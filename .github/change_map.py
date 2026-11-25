"""Write a pull request's change map as Markdown: `python .github/change_map.py BASE HEAD`.

It shows the verifier's Python files the PR changed (an edit to docstrings or comments
alone does not count), what each gained or lost, the unchanged files that import them,
and the import links the PR adds. CI posts it on the PR and updates it on every push.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

PACKAGES = {p.parent.name for p in Path().glob("*/__init__.py")}  # run from the repo root
MARKER = "<!-- change-map -->"  # how CI finds the comment to update
STATUS = {"A": "added", "D": "deleted"}  # anything else counts as changed
CLASSES = ["  classDef added fill:#e3f4e8,stroke:#1f7a3f,color:#1d2330",
           "  classDef changed fill:#fff3dc,stroke:#9a5b00,color:#1d2330",
           "  classDef deleted fill:#fbe6e8,stroke:#b42332,color:#1d2330",
           "  classDef user fill:#eef1f5,stroke:#b7bfca,color:#5d6676"]
DEFINITIONS = ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef


def git(*args: str) -> str:
    """Run git in the current repository and return what it printed."""
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def source(sha: str, path: str) -> str | None:
    """A file's text at a commit, or None if it does not exist there."""
    out = subprocess.run(["git", "show", f"{sha}:{path}"], capture_output=True, text=True)
    return out.stdout if out.returncode == 0 else None


def is_code(path: str) -> bool:
    """True for a Python file in one of the verifier's packages."""
    return path.endswith(".py") and path.split("/")[0] in PACKAGES and "/examples/" not in path


def module(path: str) -> str:
    """The import name of a file, such as core.run for core/run.py."""
    return path.removesuffix(".py").removesuffix("/__init__").replace("/", ".")


def node(name: str) -> str:
    """A Mermaid node id for a module name."""
    return name.replace(".", "_")


def code_only(text: str | None) -> str:
    """The file's syntax tree with docstrings removed (comments are never in it)."""
    if text is None:
        return ""
    tree = ast.parse(text)
    for n in ast.walk(tree):
        if isinstance(n, DEFINITIONS) and ast.get_docstring(n) is not None:
            n.body = n.body[1:] or [ast.Pass()]
    return ast.dump(tree)


def names(text: str | None) -> set[str]:
    """The functions and classes a file defines at its top level."""
    kinds = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
    return {n.name for n in ast.parse(text).body if isinstance(n, kinds)} if text else set()


def links(sha: str, known: set[str]) -> set[tuple[str, str]]:
    """Every (importer, imported) pair of the verifier's modules at a commit."""
    found = set()
    for path in filter(is_code, git("ls-tree", "-r", "--name-only", sha).splitlines()):
        here = module(path)
        for n in ast.walk(ast.parse(source(sha, path) or "")):
            if isinstance(n, ast.Import):
                targets = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module:
                # `from core import report` imports core.report, not the core package
                targets = [f"{n.module}.{a.name}" if f"{n.module}.{a.name}" in known
                           else n.module for a in n.names]
            else:
                continue
            found |= {(here, t) for t in targets if t in known and t != here}
    return found


def main(base: str, head: str) -> None:
    """Print the comment for the changes from the merge base of BASE and HEAD to HEAD."""
    base = git("merge-base", base, head).strip()
    status = {line.split("\t")[1]: STATUS.get(line[0], "changed")
              for line in git("diff", "--name-status", "--no-renames", base, head).splitlines()}
    changed = {p: k for p, k in status.items()
               if is_code(p) and code_only(source(base, p)) != code_only(source(head, p))}
    paths = {module(p): p for p in git("ls-tree", "-r", "--name-only", head).splitlines()
             if is_code(p)} | {module(p): p for p in changed}
    now, before = links(head, set(paths)), links(base, set(paths))
    mods = {module(p) for p in changed}
    shown = sorted((a, b) for a, b in now if b in mods)
    importers = {m: [a for a, b in shown if b == m and a not in mods] for m in sorted(mods)}
    users = sorted({a for found in importers.values() for a in found})
    out = [MARKER, "### Change map", ""]
    if not changed:
        out.append("No code in the verifier's packages changed (docstring and comment edits "
                   "are not counted).")
    review = [f"- New import: `{paths[a]}` → `{paths[b]}`" for a, b in sorted(now - before)]
    review += [f"- `{paths[m]}` changed, and these unchanged files import it: "
               + ", ".join(f"`{paths[a]}`" for a in found)
               for m, found in importers.items() if found]
    if review:
        out += ["**Worth a reviewer's look**", "", *review, ""]
    if changed:
        out += ["Green: added file. Amber: changed. Red: deleted. Grey: unchanged, but imports "
                "a changed file. Green arrow: an import this PR adds. `+`/`−`: functions and "
                "classes added or removed.", "", "```mermaid", "graph LR", *CLASSES]
        for path, kind in sorted(changed.items()):
            old, new = names(source(base, path)), names(source(head, path))
            gained = [f"+ {n}" for n in sorted(new - old)] + [f"− {n}" for n in sorted(old - new)]
            label = "<br/>".join([path, *gained[:6], *(["…"] if len(gained) > 6 else [])])
            out.append(f'  {node(module(path))}["{label}"]:::{kind}')
        out += [f'  {node(m)}["{paths[m]}"]:::user' for m in users]
        for i, (a, b) in enumerate(shown):
            out.append(f"  {node(a)} --> {node(b)}")
            if (a, b) not in before:
                out.append(f"  linkStyle {i} stroke:#2da44e,stroke-width:3px")
        out += ["```", "", "<details><summary>Changed code files</summary>", "",
                *(f"- {k}: `{p}`" for p, k in sorted(changed.items())), "", "</details>", ""]
    others = sorted((k, p) for p, k in status.items() if p not in changed)
    if others:
        out += ["<details><summary>Other files</summary>", "",
                *(f"- {k}: `{p}`" for k, p in others), "", "</details>"]
    print("\n".join(out))


if __name__ == "__main__":
    main(*sys.argv[1:])
