#!/usr/bin/env python3
"""Check the method writer of core/methods/method_writer.py in batches, with an offline model.

The cases check that:
  - the batches keep the needs of one data path pattern together, and split a pattern
    over the limit of needs or of prompt tokens;
  - no prompt holds a claimed value, also a value of another batch;
  - two batches give two modules, and no method name is used two times;
  - a retry asks only for the failed needs, and the methods that worked stay.
Returns 1 if any case fails.
"""
import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core.methods import method_writer  # noqa: E402
from utils import extract, structure  # noqa: E402

FAILED: list[str] = []
METHOD = ("@method('{name}')\n"
        "def {name}(path):\n"
        "    \"\"\"The mean of the scores.\n\n    Definition: the mean.\n"
        "    Assumptions: every record.\"\"\"\n"
        "    rows = [r for f in glob.glob(path) for r in json.load(open(f))['records']]\n"
        "    return Computed(value=sum(r['{key}'] for r in rows) / len(rows), n=len(rows))\n")
HEADER = "import glob\nimport json\nfrom checks.interface import Computed, method\n"


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case and print it."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def project(tmp: Path) -> Path:
    """A project with two kinds of data file: runs/seed<n>.json and eval/results.json."""
    root = tmp / "project"
    for seed in (1, 2):
        (root / "runs").mkdir(parents=True, exist_ok=True)
        (root / "runs" / f"seed{seed}.json").write_text(json.dumps(
            {"records": [{"score": 0.5 + seed / 10}, {"score": 0.25}]}))
    (root / "eval").mkdir()
    (root / "eval" / "results.json").write_text(json.dumps({"records": [{"acc": 0.75}]}))
    return root


def need(k: int, path: str, value: str) -> method_writer.MethodWriterNeed:
    """One need for the claim c<k>, whose passage states its value."""
    return method_writer.MethodWriterNeed(
        need_id=f"c{k}/n1", claim_id=f"c{k}", quantity=f"the mean score {k}", path=path,
        value=value, sister_values=(), statement=f"The score {k} is {{{{VALUE}}}}.",
        passages=(f"In run {k} the mean score was {value} over all seeds.",))


class OfflineModel:
    """The method writer's model, offline: a method for each need it is asked for.

    A need whose id is in `broken_once` gets a method that crashes in its first reply.
    """

    def __init__(self, broken_once: set[str] = frozenset()) -> None:
        self.prompts: list[str] = []
        self.broken = set(broken_once)

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        shown = json.loads(prompt.split("NEEDS:\n", 1)[1].split("\n\nDATA FILES:", 1)[0])
        module, methods = HEADER, []
        for n in shown:
            name = re.sub(r"\W", "_", n["id"]) + f"_r{len(self.prompts)}"
            key = "no_such_key" if n["id"] in self.broken else (
                "acc" if n["path"].startswith("eval") else "score")
            self.broken.discard(n["id"])
            module += METHOD.format(name=name, key=key)
            methods.append({"need": n["id"], "method": name})
        return json.dumps({"module": module, "methods": methods})


def test_batches(tmp: Path) -> None:
    """The batches group the needs by data path pattern, within both limits."""
    root = project(tmp)
    needs = [need(1, "runs/seed1.json", "0.61"), need(2, "eval/results.json", "0.74"),
             need(3, "runs/seed2.json", "0.71"), need(4, "runs/seed*.json", "0.65")]
    writer = method_writer.MethodWriter(structure.data_files(str(root)), str(root), tmp / "state",
                                    OfflineModel(), needs_per_batch=2)
    batches = [[n.claim_id for n in b] for b in writer.batches(needs)]
    expect("batches: one pattern stays together, and splits over the limit of needs",
           batches == [["c2"], ["c1", "c3"], ["c4"]], repr(batches))
    loose = method_writer.MethodWriter(structure.data_files(str(root)), str(root), tmp / "state",
                                   OfflineModel(), needs_per_batch=3)
    batches = [[n.claim_id for n in b] for b in loose.batches(needs[:3])]
    expect("batches: whole groups of two patterns share a batch when they fit",
           batches == [["c2", "c1", "c3"]], repr(batches))
    one = writer.prompt_for(needs[:1], {"0.61"})
    small = method_writer.MethodWriter(structure.data_files(str(root)), str(root), tmp / "state",
                                   OfflineModel(), prompt_tokens_per_batch=len(one) // 4)
    sizes = [len(b) for b in small.batches(needs)]
    expect("batches: a batch over the token limit splits, one need at least",
           sizes == [1, 1, 1, 1], repr(sizes))


def test_write_methods(tmp: Path) -> None:
    """Two batches give two modules; a retry asks only for the failed need."""
    root = project(tmp)
    state = tmp / "state"
    needs = [need(1, "runs/seed1.json", "0.61"), need(2, "eval/results.json", "0.74"),
             need(3, "runs/seed2.json", "0.71")]
    model = OfflineModel(broken_once={"c3/n1"})
    writer = method_writer.MethodWriter(structure.data_files(str(root)), str(root), state, model,
                                    retries=1, needs_per_batch=2)
    report = writer.write_methods(needs)
    expect("write: every need gets a working method",
           report.linked == 3 and not report.unresolved and report.batches == 2, repr(report))
    expect("write: three calls, one for each batch and one retry", len(model.prompts) == 3,
           str(len(model.prompts)))
    retry_needs = json.loads(model.prompts[2].split("NEEDS:\n", 1)[1]
                             .split("\n\nDATA FILES:", 1)[0])
    expect("write: the retry asks only for the failed need",
           [n["id"] for n in retry_needs] == ["c3/n1"] and "crashed" in model.prompts[2],
           repr(retry_needs))
    expect("write: the retry is told the names of the earlier modules",
           "c1_n1_r2" in model.prompts[2].split("NEEDS:")[0])
    modules = sorted(p.name for p in (state / "methods").glob("*.py"))
    expect("write: one module for each reply that has a working method",
           modules == ["written_methods_1.py", "written_methods_2.py", "written_methods_3.py"],
           repr(modules))
    names = list(report.methods.values())
    expect("write: no method name is used two times", len(set(names)) == len(names), repr(names))
    expect("write: no prompt holds a claimed value, also of another batch",
           all(v not in p for p in model.prompts for v in ("0.61", "0.74", "0.71")),
           next((p for p in model.prompts if "0.71" in p), "")[-300:])
    expect("write: each prompt holds only the passages of its own needs",
           "In run 2" in model.prompts[0] and "In run 1" not in model.prompts[0],
           model.prompts[0][-300:])


def test_mask_hides_each_print_of_a_value(tmp: Path) -> None:
    """The mask hides a value in each form that a paper can print it."""
    hidden = extract.SISTER_VALUE_HIDDEN
    for value, text in (("0.12", "acc 0.120 here"), ("0.120", "acc 0.12 here"),
                        ("1300", "n = 1,300 runs"), ("1e-05", "lr 0.00001 here"),
                        ("0.00001", "lr 1e-05 here"), ("1e-05", "lr 1E-5 here"),
                        ("0.5", "delta \u22120.5 here"), ("12.5", "12.5% here")):
        masked = method_writer._mask(text, {value})
        expect(f"mask: the value {value} is hidden in {text!r}",
               hidden in masked and not any(c.isdigit() for c in masked), masked)
    try:
        method_writer.masked_values([need(1, "runs/seed1.json", "n/a")])
        said = ""
    except ValueError as e:
        said = str(e)
    expect("mask: a value that is not a number is refused, with its claim", "c1" in said, said)
    kept = method_writer._mask("acc 0.121 and 10.12 and v0.12", {"0.12"})
    expect("mask: another number, and a number in a longer number or a name, stays",
           kept == "acc 0.121 and 10.12 and v0.12", kept)


def test_data_files_with_links(tmp: Path) -> None:
    """Broken links, link loops, tool folders and very long CSV cells give no crash."""
    root = project(tmp)
    os.symlink("does-not-exist.json", root / "runs" / "broken.json")
    os.symlink(".", root / "latest")
    os.symlink("..", root / "up")
    (tmp / "outside.json").write_text("{}")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "package.json").write_text("{}")
    (root / "wide.csv").write_text("a\n" + "x" * 200_000 + "\n")
    found = structure.data_files(str(root))
    examples = [e for g in found["groups"].values() for e in g["examples"]]
    expect("data files: a broken link, a link loop, a link out of the project and "
           "node_modules are left out",
           not any(e.startswith(("latest", "node_modules", "up")) or "broken" in e
                   for e in examples),
           repr(examples))
    wide = [g["first"] for g in found["groups"].values() if g["examples"][0] == "wide.csv"]
    expect("data files: a CSV cell over the csv field limit is unreadable, with no crash",
           len(wide) == 1 and "unreadable" in wide[0], repr(wide))


def test_reply_of_the_wrong_form(tmp: Path) -> None:
    """A reply whose methods list has a wrong entry is unreadable, not a crash."""
    for entry in ('{"method": "m"}', '{"need": "c1/n1", "method": ["m"]}', '"c1/n1"'):
        reply = json.dumps({"module": "x = 1"})[:-1] + f', "methods": [{entry}]}}'
        try:
            method_writer._parse(reply)
            refused = False
        except ValueError:
            refused = True
        expect(f"reply: the methods entry {entry} is refused as unreadable", refused)


def main() -> int:
    """Run every case in a new temporary folder."""
    for case in (test_batches, test_write_methods, test_mask_hides_each_print_of_a_value,
                 test_reply_of_the_wrong_form,
                 test_data_files_with_links):
        print(case.__name__)
        with tempfile.TemporaryDirectory() as tmp:
            case(Path(tmp))
    if FAILED:
        print(f"\n{len(FAILED)} case(s) failed: {FAILED}")
        return 1
    print("\nAll method writer cases passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
