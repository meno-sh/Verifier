#!/usr/bin/env python3
"""Write the class signatures of SPEC.md from the code, or check that they match it.

SPEC.md holds one block for each layer, between the markers `<!-- spec:classes LAYER -->`
and `<!-- /spec:classes -->`. This script writes the blocks; the text around them is
written by hand. tests/test_spec.py runs it with --check.

Usage: spec_signatures.py [--check]
"""
from __future__ import annotations

import ast
import collections
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "docs" / "SPEC.md"
LAYERS = ("schema", "utils", "core", "checks", "runtime", "review", "diagnosis")
MIN_USERS = 3               # a class that 3 or more other classes use or inherit is major
# Classes with fewer users that have an important role in a run, by (file, name).
IMPORTANT = (
    ("runtime/daemon.py", "Daemon"),
    ("runtime/agent/opencode.py", "Client"),
    ("core/analysis/claim_checker.py", "ClaimChecker"),
    ("core/analysis/document_checker.py", "DocumentChecker"),
    ("core/report_model/report_builder.py", "VerificationReportBuilder"),
    ("core/project/check_runs.py", "StaticCheckRun"),
    ("core/project/check_runs.py", "WatchedProjectCheckRun"),
    ("checks/extension.py", "BuiltinChecks"),
    ("core/methods/computational_methods.py", "MethodFolderLoader"),
    ("core/registry/plugin_checks.py", "PluginCheckFolderLoader"),
    ("core/project/agent_session.py", "AgentSessionPolicy"),
    ("core/methods/method_writer.py", "MethodWriter"),
    ("core/methods/source_index.py", "SourceIndex"),
    ("core/analysis/check_result_cache.py", "CheckResultCache"),
)
START = re.compile(r"<!-- spec:classes (\w+) -->\n")
END = "<!-- /spec:classes -->"
WIDTH = 96


def class_definitions(root: Path) -> dict[tuple[str, str], ast.ClassDef]:
    """Every class of the layers, by (file relative to `root`, class name)."""
    out = {}
    for layer in LAYERS:
        for path in sorted((root / layer).rglob("*.py")):
            tree = ast.parse(path.read_text())
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    out[(str(path.relative_to(root)), node.name)] = node
    return out


def _name_of(node: ast.AST) -> str | None:
    """The class name that a name or an attribute (module.Class) refers to."""
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


def users_by_class(classes: dict[tuple[str, str], ast.ClassDef]) -> dict[str, set[str]]:
    """For each class name, the other classes that use or inherit it, as "file:name"."""
    names = {name for _, name in classes}
    users: dict[str, set[str]] = collections.defaultdict(set)
    for (path, name), node in classes.items():
        for sub in ast.walk(node):
            ref = _name_of(sub)
            if ref in names and ref != name:
                users[ref].add(f"{path}:{name}")
    return users


def major_classes(root: Path = ROOT) -> list[tuple[str, str]]:
    """The classes SPEC.md shows, by (file, name), in file order and then source order.

    A class is major when 3 or more other classes use it or inherit from it, or when
    IMPORTANT names it.
    """
    classes = class_definitions(root)
    users = users_by_class(classes)
    missing = [key for key in IMPORTANT if key not in classes]
    assert not missing, f"IMPORTANT names classes that do not exist: {missing}"
    chosen = {key for key in classes if len(users[key[1]]) >= MIN_USERS} | set(IMPORTANT)
    return sorted(chosen, key=lambda key: (key[0], classes[key].lineno))


def _first_line(node: ast.AST) -> str | None:
    """The first line of the docstring of a class or a function, or None."""
    doc = ast.get_docstring(node) if isinstance(
        node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) else None
    return doc.strip().splitlines()[0] if doc else None


def _signature(node: ast.FunctionDef | ast.AsyncFunctionDef, indent: str) -> list[str]:
    """The `def` line of a method, wrapped after each argument when it is too long."""
    head = f"{indent}{'async ' if isinstance(node, ast.AsyncFunctionDef) else ''}def {node.name}("
    tail = ")" + (f" -> {ast.unparse(node.returns)}" if node.returns else "") + ": ..."
    args = ast.unparse(node.args)
    if len(head) + len(args) + len(tail) <= WIDTH:
        return [head + args + tail]
    parts = [p.strip() for p in _split_arguments(args)]
    if len(parts) == 1:
        return [head + args + tail]
    pad = " " * len(head)
    return ([head + parts[0] + ","] + [pad + p + "," for p in parts[1:-1]]
            + [pad + parts[-1] + tail])


def _split_arguments(args: str) -> list[str]:
    """The arguments of an argument list, split at the commas outside brackets."""
    out, depth, current = [], 0, ""
    for ch in args:
        depth += ch in "([{"
        depth -= ch in ")]}"
        if ch == "," and depth == 0:
            out.append(current)
            current = ""
        else:
            current += ch
    return [*out, current]


def class_block(path: str, node: ast.ClassDef) -> list[str]:
    """The pseudocode of one class: its fields with their types and its public methods."""
    bases = ", ".join(ast.unparse(b) for b in node.bases)
    lines = [f"class {node.name}({bases}):" if bases else f"class {node.name}:"]
    doc = _first_line(node)
    if doc:
        lines.append(f'    """{doc}"""')
    body = 0
    for item in node.body:
        if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            lines.append(f"    {item.target.id}: {ast.unparse(item.annotation)}")
            body += 1
        elif (isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef)
              and (not item.name.startswith("_") or item.name == "__init__")):
            decorators = [d for d in item.decorator_list
                          if _name_of(d) in ("property", "classmethod", "staticmethod")]
            first = _first_line(item)
            if first:
                lines.append(f"    # {first}")
            lines += [f"    @{_name_of(d)}" for d in decorators]
            lines += _signature(item, "    ")
            body += 1
    if not body and not doc:
        lines.append("    ...")
    return lines


def layer_blocks(root: Path = ROOT) -> dict[str, str]:
    """The text of the block of each layer: the major classes of its files, file by file."""
    classes = class_definitions(root)
    by_layer: dict[str, list[str]] = {layer: [] for layer in LAYERS}
    current: dict[str, str] = {}
    for path, name in major_classes(root):
        layer = path.split("/")[0]
        lines = by_layer[layer]
        if current.get(layer) != path:
            current[layer] = path
            lines += ([""] if lines else []) + [f"# {path}"]
        lines += ["", *class_block(path, classes[(path, name)])]
    return {layer: "```python\n" + "\n".join(lines) + "\n```\n"
            for layer, lines in by_layer.items() if lines}


def spec_with_blocks(text: str, blocks: dict[str, str]) -> str:
    """SPEC.md with the text between each pair of markers replaced by its block.

    Raises:
        ValueError: a layer with major classes has no markers, or a marker names no layer.
    """
    out, pos, seen = [], 0, set()
    for start in START.finditer(text):
        layer = start.group(1)
        if layer not in blocks:
            raise ValueError(f"SPEC.md has markers for {layer!r}, which has no major class")
        end = text.index(END, start.end())
        out += [text[pos:start.end()], blocks[layer]]
        pos = end
        seen.add(layer)
    missing = sorted(set(blocks) - seen)
    if missing:
        raise ValueError(f"SPEC.md has no markers for the layers {missing}")
    return "".join(out) + text[pos:]


def changed_classes(old: str, new: str) -> list[str]:
    """The names of the classes whose lines differ between two texts of SPEC.md."""
    def classes(text: str) -> dict[str, str]:
        """Each class block of a text, by its class name."""
        found = re.findall(r"^class (\w+)[^\n]*\n((?:(?!class |# |```).*\n)*)", text, re.M)
        return dict(found)
    a, b = classes(old), classes(new)
    return sorted(name for name in a.keys() | b.keys() if a.get(name) != b.get(name))


def main(argv: list[str]) -> int:
    """Write the blocks into SPEC.md, or with --check, say which classes differ."""
    text = SPEC.read_text()
    new = spec_with_blocks(text, layer_blocks())
    if "--check" not in argv:
        SPEC.write_text(new)
        print(f"wrote the class blocks of {SPEC.name}")
        return 0
    if new == text:
        print(f"the class blocks of {SPEC.name} match the code")
        return 0
    names = changed_classes(text, new) or ["(the order or the file headers)"]
    print(f"{SPEC.name} does not match the code for: {', '.join(names)}. "
          f"Run `python docs/spec_signatures.py` and review the change.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
