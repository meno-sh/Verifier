"""Stage 5, the links between claims: a model reads the full paper and links each claim.

For each claim with no source, a model gets the full paper (the shared paper prefix,
utils/shared_paper_prefix.py), the 37 labelled examples of the 4 pilot papers and the
numbered register. Step 1 asks for every piece of the paper that the claim rests on; step 2, in the
same conversation, maps each piece to register items, which become the claim's links. The
examples and the register have their own cache mark, because every claim of a paper sends
them. `vcheck link-claims` runs it after the agent's round and `vcheck reorder-claims`, and
saves each answer in full_paper_links.jsonl, with the checksum of the register it saw. It drops
each link to a claim before its claim in the register order (drop_links_against_order).
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import threading
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from core.registry import run_options
from utils import (
    alerts,
    checksums,
    claim_files,
    file_storage,
    models,
    shared_paper_prefix,
    spending,
)

PURPOSE = "full_paper_linker"           # the spending purpose of the linker's calls
EXAMPLES_FILE = Path(__file__).with_name("full_paper_linker_examples.json")
EXAMPLE_COUNT = 37
RESULTS_FILE = "full_paper_links.jsonl"     # in the state folder, one line per linked claim
# The most `warn` records for links against the register order in one run; the others are
# counted in the run's last line.
ORDER_ALERTS_PER_RUN = 5

MODEL = run_options.RunOption(
    key="full_paper_linker_model", kind=str, flag="--full-paper-linker-model",
    default=models.preset.HAIKU,
    help="the model `vcheck link-claims` asks (utils/models.py names)")
TABLES = run_options.RunOption(
    key="full_paper_linker_tables", kind=bool, flag="--full-paper-linker-tables", default=False,
    help="`vcheck link-claims` also links the claims of whole tables")
WORKERS = run_options.RunOption(
    key="full_paper_linker_workers", kind=int, flag="--full-paper-linker-workers", default=64,
    help="the claims `vcheck link-claims` links at once")
TIMEOUT = run_options.RunOption(
    key="full_paper_linker_timeout", kind=int, flag="--full-paper-linker-timeout", default=900,
    help="seconds `vcheck link-claims` waits for one reply")
RUN_OPTIONS = (MODEL, TABLES, WORKERS, TIMEOUT)

# The system part after the shared paper prefix.
SYSTEM = """You read a research paper and its claim register. The full text of the paper (LaTeX \
or Markdown notes) comes first. Then comes the claim register of the paper: one item per line, \
as [number] statement.

LABELLED EXAMPLES FROM OTHER PAPERS. Each example gives a claim, a register item and the correct \
label for a link from the claim to the item, with a short reason. Link an item only when it states \
a piece that the claim rests on, for the same experiment, setting and metric as the claim.

{examples}

CLAIM REGISTER:
{register}
"""

STEP1 = """Claim: {claim}

List every piece of information in the paper that this claim rests on: results, tables, \
figures, setup, definitions and equations. Include each piece that the claim needs to be true, \
also when it is far from the claim in the paper. For each piece, give a short exact quote from \
the paper and its place (section, table, figure or equation). Do not judge whether the claim is \
true. Do not list the claim itself."""

STEP2 = """For each piece that you listed, give the numbers of the register items that state it. \
Give an empty list when no register item states it. Use the labelled examples. Never give the \
number of the claim itself ({own}). Give only items with a number above the claim's own number \
({own}): the register lists the most valuable claims first, and a claim rests only on claims \
after it.

Reply with one JSON object and nothing else:
{{"points": [{{"point": "<short description>", "quote": "<exact quote>", "place": "<place>", \
"items": [<register numbers>]}}]}}"""


@dataclasses.dataclass(frozen=True)
class FullPaperLinkerSettings:
    """The settings of one `vcheck link-claims`, from the run options."""
    model: str
    tables: bool
    workers: int
    timeout: int

    @classmethod
    def from_options(cls, options: Mapping[str, object]) -> FullPaperLinkerSettings:
        """The settings that the run options give."""
        return cls(model=str(MODEL.value_in_options(options)),
                   tables=bool(TABLES.value_in_options(options)),
                   workers=int(WORKERS.value_in_options(options)),
                   timeout=int(TIMEOUT.value_in_options(options)))


def labelled_examples(path: Path = EXAMPLES_FILE) -> list[dict]:
    """The EXAMPLE_COUNT labelled examples of the 4 pilot papers, in file order.

    The eval leaves the pilot papers out, so no example comes from a paper it links.

    Raises:
        ValueError: the file does not hold EXAMPLE_COUNT examples.
    """
    examples = json.loads(path.read_text())["examples"]
    if len(examples) != EXAMPLE_COUNT:
        raise ValueError(f"{path} must hold {EXAMPLE_COUNT} examples; it holds {len(examples)}")
    return examples


def render(examples: list[dict]) -> str:
    """The labelled examples as text for the system part."""
    out = []
    for n, e in enumerate(examples, 1):
        lines = [f"Example {n}", f"Claim: {e['claim']}"]
        if e["piece"]:
            lines.append(f"Piece: {e['piece']}")
        lines += [f"Register item: {e['item']}",
                  f"Correct label: {e['label']}. Reason: {e['reason']}"]
        out.append("\n".join(lines))
    return "\n\n".join(out)


def is_table(cid: str) -> bool:
    """A claim of a whole table."""
    return "#tab-" in cid


def parse(reply: str, n_items: int) -> list[dict]:
    """The points of a step-2 reply, with numbers checked against the register.

    Raises:
        models.BadReply: the reply has no JSON object with points, or a number is not an
            item of the register.
    """
    try:
        text = reply[reply.index("{"): reply.rindex("}") + 1]
        points = json.loads(text)["points"]
    except (ValueError, KeyError, TypeError) as e:
        raise models.BadReply(f"the reply is not one JSON object with a points list ({e})") from e
    if not isinstance(points, list) or not all(
            isinstance(p, dict) and isinstance(p.get("items"), list) for p in points):
        raise models.BadReply("the points must be a list of objects, each with an items list")
    for p in points:
        bad = [i for i in p["items"] if not isinstance(i, int) or not 0 <= i < n_items]
        if bad:
            raise models.BadReply(f"register numbers out of range: {bad}")
    return points


def register_text(claims: Sequence[dict]) -> str:
    """The numbered register, one claim a line, as [number] statement.

    A table claim shows at most claim_files.TABLE_STATEMENT_CHARS characters of its
    statement: the paper is in the same prompt, so a whole table is not needed twice.
    """
    cap = claim_files.TABLE_STATEMENT_CHARS
    return "\n".join(f"[{n}] {c['statement'][:cap] if is_table(c['id']) else c['statement']}"
                     for n, c in enumerate(claims))


def claims_to_link(claims: Sequence[dict], tables: bool,
                   done: frozenset[tuple[str, str]], register_checksum: str) -> list[dict]:
    """The claims with no source, not of a table unless `tables`, not asked with this register."""
    return [c for c in claims if c.get("source") is None
            and (tables or not is_table(c["id"]))
            and (c["id"], register_checksum) not in done]


def link_one_claim(claim: dict, claims: Sequence[dict], paper_prefix: str, system: str,
                   settings: FullPaperLinkerSettings) -> dict:
    """Ask the 2 steps for one claim, and give its links, points, step-1 reply and cost.

    Raises:
        models.ProviderError, models.BadReply: the model failed after its retries.
    """
    own = next(n for n, c in enumerate(claims) if c["id"] == claim["id"])
    step1 = STEP1.format(claim=claim["statement"])
    common = {"system": system, "shared_prefix_for_cache": paper_prefix,
              "cache_system_after_shared_prefix": True}
    with spending.purpose(PURPOSE):
        r1 = models.ask(step1, settings.model, settings.timeout, **common)
        points = models.ask_and_read(STEP2.format(own=own), settings.model,
                                     lambda reply: parse(reply, len(claims)), settings.timeout,
                                     earlier_turns=((step1, r1),), **common)
    links = sorted({claims[i]["id"] for p in points for i in p["items"] if i != own})
    return {"claim": claim["id"], "links": links, "points": points, "step1": r1}


@dataclasses.dataclass(frozen=True)
class LinkerRunCounts:
    """What one run of the linker did: claims linked and failed, and links dropped."""
    linked: int
    failed: int
    links_against_order: int        # links from a claim to a claim before it, dropped


def drop_links_against_order(row: dict, claims: Sequence[dict]) -> tuple[dict, list[str]]:
    """The answer `row` without its links to claims before its own claim in `claims`.

    The register is in the order of `vcheck reorder-claims`, so a claim rests only on claims
    after it. The answer keeps the dropped links as `links_against_order`.

    Returns:
        The answer, and the ids of the dropped links.
    """
    place = {c["id"]: n for n, c in enumerate(claims)}
    own = place[row["claim"]]
    dropped = [link for link in row["links"] if place[link] < own]
    kept = [link for link in row["links"] if place[link] > own]
    return {**row, "links": kept, "links_against_order": dropped}, dropped


def link_claims(paper_text: str, claims: Sequence[dict], todo: Sequence[dict],
                settings: FullPaperLinkerSettings, save: Callable[[dict], None]
                ) -> LinkerRunCounts:
    """Link each claim of `todo` against the register `claims`, and `save` each answer.

    A claim whose model fails after its retries is a `warn` in the alert log and is not
    saved, so the next `vcheck link-claims` asks again. A link from a claim to a claim
    before it in `claims` is dropped (drop_links_against_order). Each of the first
    ORDER_ALERTS_PER_RUN dropped links is a `warn` in the alert log.

    Returns:
        The numbers of claims linked and failed, and of links dropped.

    Raises:
        shared_paper_prefix.PaperTooLongForModelContext: the paper is too long.
    """
    paper_prefix = shared_paper_prefix.shared_paper_prefix_for_model_calls(paper_text)
    checksum = register_checksum(claims)
    system = SYSTEM.format(register=register_text(claims),
                           examples=render(labelled_examples()))
    linked, failed, against_order, lock = 0, 0, 0, threading.Lock()
    with concurrent.futures.ThreadPoolExecutor(settings.workers) as pool:
        futures = {pool.submit(link_one_claim, c, claims, paper_prefix, system, settings):
                   c["id"] for c in todo}
        for f in concurrent.futures.as_completed(futures):
            try:
                row = f.result()
            except (models.ProviderError, models.BadReply) as e:
                failed += 1
                alerts.notify("warn", "full_paper_linker.failed", f"{futures[f]}: {e}",
                              error=e, claim=futures[f])
                continue
            row, dropped = drop_links_against_order(row, claims)
            with lock:
                for link in dropped:
                    against_order += 1
                    if against_order <= ORDER_ALERTS_PER_RUN:
                        alerts.notify("warn", "full_paper_linker.link_against_order",
                                      f"{row['claim']} -> {link}: the link goes to a claim "
                                      "before it in the register order, so it is dropped",
                                      claim=row["claim"], link=link,
                                      shown=f"{against_order} of at most "
                                            f"{ORDER_ALERTS_PER_RUN} in this run")
                save({**row, "register_sha256": checksum})
                linked += 1
    return LinkerRunCounts(linked=linked, failed=failed, links_against_order=against_order)


def register_checksum(claims: Sequence[dict]) -> str:
    """The checksum of the numbered register the model sees."""
    return checksums.sha256_of_json(register_text(claims))


def read_results(state_dir: Path) -> list[dict]:
    """The saved answers of the linker, oldest first; none when there are none."""
    return file_storage.AppendOnlyJsonLinesFile(state_dir / RESULTS_FILE).read_rows()


def append_result(state_dir: Path, row: dict) -> None:
    """Save one answer of the linker."""
    file_storage.AppendOnlyJsonLinesFile(state_dir / RESULTS_FILE).append_row(row)


def apply_links(register: list[dict], rows: Sequence[dict]) -> int:
    """Write the links of `rows` into `register`, and give the number of claims changed.

    Each claim in `rows` with at least one link, and with no source, gets the source
    {"kind": "claims", "claims": links} and an empty `last_updated` stamp. Every link in
    `rows` is kept. The answers of the linker hold only links to later claims, so these
    links cannot close a cycle; the check `acyclic_support` reports a cycle from another
    source of links.

    Raises:
        ValueError: a link names a claim that is not in the register, or the claim itself.
    """
    ids = {c["id"] for c in register if isinstance(c, dict) and "id" in c}
    new = {r["claim"]: r["links"] for r in rows}
    changed = 0
    for c in register:
        if not isinstance(c, dict) or "id" not in c:
            continue
        links = new.get(c["id"])
        if not links or c.get("source") is not None:
            continue
        if not all(link in ids and link != c["id"] for link in links):
            raise ValueError(f"the links of {c['id']} name a claim that is not in the register, "
                             f"or the claim itself: {links}")
        c["source"] = {"kind": "claims", "claims": links}
        c["last_updated"] = claim_files.NEW
        changed += 1
    return changed
