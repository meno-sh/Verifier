"""Find the papers, notes, commit messages and data files in a research folder.

utils/extract.py reads the contents of each file.
"""
from __future__ import annotations

import fnmatch
import glob
import logging
import os
import re
import subprocess
from pathlib import Path

from utils import alerts, extract

# Paper discovery runs also outside the daemon (a benchmark, a test), where no alert log is
# configured, so a document left out is logged here.
LOG = logging.getLogger(__name__)

# Every ending the paper reader knows, and plain text formats it reads as Markdown.
NOTE_SUFFIXES = (*(e for endings in extract.EXTENSIONS.values() for e in endings), ".txt", ".rst")
WHOLE = "."                # a scope entry for the whole working directory
STATE_DIR_NAME = ".verifier"   # the state folder of the Verifier in a working directory
# In the state folder: the files of the daemon and OpenCode, which are never committed,
# and in it the file with the commit the daemon checks (runtime.project.paths.StatePaths).
RUNTIME_DIR = "runtime"
CHECKED_COMMIT_FILE = "checked"
SKIP_DIRS = {".git", STATE_DIR_NAME, "node_modules", "__pycache__", ".venv", "venv"}
MAX_FILES = 300            # data file groups listed; the rest are counted, never silently dropped
MAX_BYTES = 200_000_000    # larger data files are listed but not opened
# Match a folder that holds one copy of a run, one folder per seed, iteration or adapter.
REPLICA_DIR = re.compile(r"^(s|seed|iter|run|adapter|ckpt|checkpoint)[_-]?\d+$")


def read_tex(path: str | os.PathLike) -> str | None:
    """The text of a .tex file, or None when the file cannot be read.

    A file that cannot be read (no permission, or a folder named .tex) does not stop
    paper discovery. It is a `warn` in the alert log, so it is not silently lost.
    """
    try:
        with open(path, errors="ignore") as fh:
            return fh.read()
    except OSError as e:
        alerts.notify("warn", "papers.unreadable", f"{path} cannot be read, so it is "
                      "not checked as a paper", error=e, path=str(path))
        return None


def paper_candidates(root: str) -> list[tuple[str, str, int]]:
    r"""Find every .tex file under `root` with a \documentclass, largest first.

    Do not choose one file when the list has more than one. Verifying the wrong paper is
    the failure this tool exists to prevent.

    Returns:
        (path, path relative to root, size in characters) for each file.
    """
    out = []
    for dirp, _, files in os.walk(root):
        for f in sorted(files):
            if not f.endswith(".tex"):
                continue
            fp = os.path.join(dirp, f)
            if os.path.islink(fp) and not os.path.exists(fp):
                continue                # a link to nothing holds no paper
            t = read_tex(fp)
            if t is None:
                continue
            if "\\documentclass" in t:
                out.append((fp, os.path.relpath(fp, root), len(t)))
    return sorted(out, key=lambda x: -x[2])


def checked_commit(state_dir: Path) -> str:
    """The commit the daemon last moved the checkout to, or "" before the first one.

    The daemon records it, because the agent works in the checkout and can move its HEAD.
    So every reader of the commit being checked reads it here, not from the checkout.

    Raises:
        ValueError: the file is there but empty.
    """
    path = state_dir / RUNTIME_DIR / CHECKED_COMMIT_FILE
    if not path.exists():
        return ""
    sha = path.read_text().strip()
    if not sha:
        raise ValueError(f"{path} is empty")
    return sha


def _git(root: str, *args: str) -> str | None:
    """Output of a git command in `root`, or None when `root` is not in a git repository.

    In a repository with no commits yet, a command that reads commits gives no output.

    Raises:
        subprocess.CalledProcessError: git failed in a repository, for example on a
            commit that does not exist.
    """
    # git's messages in English, so the checks below work in every locale
    out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True,
                         env={**os.environ, "LC_ALL": "C"})
    if out.returncode == 0:
        return out.stdout
    if "not a git repository" in out.stderr:
        return None
    if "does not have any commits yet" in out.stderr:
        return ""                       # a new repository: no commits to list
    raise subprocess.CalledProcessError(out.returncode, out.args, out.stdout, out.stderr)


def changed_files(root: str, commit: str) -> list[str]:
    """The files under `root` that `commit` changed, relative to `root`.

    A merge commit gives the files it changed from its first parent, and the first commit
    every file it added. Outside a git repository there is none.

    Raises:
        subprocess.CalledProcessError: the commit does not exist.
    """
    out = _git(root, "diff-tree", "-r", "--name-only", "--no-commit-id", "--root", "-m",
               "--first-parent", "--relative", commit)
    return sorted(line for line in (out or "").splitlines() if line)


def note_files(root: str, exclude: tuple[str, ...] = (), since: str | None = None,
               within: tuple[str, ...] = (WHOLE,)) -> list[str]:
    """Return the note paths under `root`, relative to `root`.

    In a git repository, the list has only tracked files. With `since`, it has only later changes.
    With `within`, it has only the notes in those files and folders (`within_scope`).
    """
    if since:
        listed = (_git(root, "diff", "--name-only", f"{since}..HEAD") or "").splitlines()
    else:
        tracked = _git(root, "ls-files")
        if tracked is not None:
            listed = tracked.splitlines()
        else:
            listed = []
            for d, dirs, files in os.walk(root):
                dirs[:] = [x for x in dirs if x not in SKIP_DIRS]
                listed += [os.path.relpath(os.path.join(d, f), root) for f in files]
    out = []
    for rel in sorted(set(listed)):
        if not rel.lower().endswith(NOTE_SUFFIXES) or any(p in SKIP_DIRS for p in Path(rel).parts):
            continue
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude) or not within_scope(rel, within):
            continue
        if os.path.isfile(os.path.join(root, rel)):
            out.append(rel)
    return out


def within_scope(rel: str, within: tuple[str, ...]) -> bool:
    """True when the note `rel` is one of the files of `within`, or in one of its folders.

    Each entry of `within` is a path relative to the working directory, with `/` between
    folders. `WHOLE` is the whole working directory.
    """
    if not within:
        raise ValueError("a scope names at least one file or folder")
    return any(p == WHOLE or rel == p or rel.startswith(p + "/") for p in within)


def note_candidates(root: str, rel: str) -> list[dict]:
    """The candidates of one note, ids prefixed with its path and tagged with it as origin.

    A note in a format with no reader of its own (.txt, .rst) is read as Markdown. A whole
    table longer than extract.LONG_TABLE_CHARACTERS is a `warn` in the alert log.
    """
    path = os.path.join(root, rel)
    try:
        paper = extract.load([path])
    except extract.UnknownPaperFormat:
        paper = extract.read_markdown(path)
    found = [{**c, "id": f"{rel}#{c['id']}", "origin": rel} for c in extract.candidates(paper)]
    for c in found:
        if c["kind"] == extract.TABLE and len(c["text"]) > extract.LONG_TABLE_CHARACTERS:
            alerts.notify("warn", "papers.long_table",
                          f"{c['id']} ({c['table']}) has {len(c['text']):,} characters, more "
                          f"than {extract.LONG_TABLE_CHARACTERS:,}; it stays one whole "
                          "candidate", candidate=c["id"], characters=len(c["text"]))
    return found


def commit_candidates(root: str, since: str | None = None) -> list[dict]:
    """The sentences of every commit message (after `since` when given), oldest first.

    The subject line is its own block, so a subject with no full stop is not joined to
    the first sentence of the body.
    """
    rng = [f"{since}..HEAD"] if since else []
    log = _git(root, "log", "--reverse", "--format=%H%x00%B%x01", *rng)
    if not log:
        return []
    out = []
    for entry in log.split("\x01"):
        if "\x00" not in entry:
            continue
        sha, message = entry.strip("\n").split("\x00", 1)
        flat = " ".join(message.split())
        subject, _, body = message.strip().partition("\n")
        found = extract.sentence_candidates(extract.sentences(
            [" ".join(subject.split()), " ".join(body.split())]))
        if not found and extract.numbers(flat):       # a short message stating a number
            found = extract.sentence_candidates([flat])
        for c in found:
            out.append({**c, "id": f"commit:{sha[:10]}#{c['id']}", "kind": extract.COMMIT,
                        "commit": sha})
    return out


def included_tex(root: str, rels: list[str]) -> set[str]:
    r"""The LaTeX files in `rels` that another LaTeX file in `rels` puts in with \input or \include.

    The reader reads such a file through the file that includes it, so it is not read again
    on its own. The includes are found as the reader finds them (extract.LatexIncludes).
    """
    wanted = {os.path.abspath(os.path.join(root, rel)): rel for rel in rels}
    out = set()
    for rel in rels:
        if rel.lower().endswith(extract.EXTENSIONS[extract.PaperFormat.LATEX]):
            out.update(wanted[p] for p in extract.LatexIncludes(os.path.join(root, rel)).included
                       if p in wanted)
    return out


FULL_DOC = (re.compile(r"\\documentclass"), re.compile(r"\\begin\{document\}"))


# A full document compiled on its own beside the paper, which is part of the submission.
SUPPLEMENT_NAME = re.compile(r"(?<![a-z])supp(?:l|lement|lementary|lemental)?(?![a-z])"
                             r"|appendix|appendices", re.I)
OLD_COPY_DIR = re.compile(r"(?:^|/)(?:backup|backups|old|trash|archive|deprecated|unused|previous"
                          r"|drafts?|bak)[^/]*/", re.I)


def unused_tex(root: str, rels: list[str], included: set[str]) -> set[str]:
    r"""The .tex files that are not part of the paper a folder compiles.

    When a folder (not the repository root) holds a full LaTeX document (with \documentclass
    and \begin{document}), the paper is that document and the files it puts in. Left out are:
    another full document in the same folder (a template, an earlier version), a full
    document under a folder named like an old copy (backup/, old/, trash/, drafts/), a
    standalone figure (\documentclass{standalone}), and a .tex file that no file includes
    when it sits below the paper's folder or in a folder the paper takes files from (an
    unused table, a checklist). With several full documents in one
    folder, the one LaTeX was run on wins: a .bbl of the same name beside it, else main.tex,
    else the longest. A document named like a supplement or appendix (supplementary.tex,
    appendix.tex) is compiled on its own and is part of the paper, so it is kept beside the
    main one unless it sits under an old-copy folder. A paper at the repository root does
    not hide the repository's other notes: only its own folder's documents and old copies
    are left out. Each full document left out is logged by name.
    """
    texs = [r for r in rels if r.endswith(".tex") and r not in included]
    full, standalone = [], set()
    for rel in texs:
        text = read_tex(os.path.join(root, rel))
        if text is None:
            continue
        text = extract._strip_comments(text)
        if re.search(r"\\documentclass(?:\[[^\]]*\])?\{standalone\}", text):
            standalone.add(rel)
        elif all(p.search(text) for p in FULL_DOC):
            bbl = os.path.isfile(os.path.join(root, rel[:-4] + ".bbl"))
            full.append(((bbl, os.path.basename(rel) == "main.tex", len(text)), rel))
    kept: list[str] = []
    out = set(standalone)
    for _, rel in sorted(full, key=lambda f: (not _is_supplement(f[1]), f[0]), reverse=True):
        folder = os.path.dirname(rel)
        old_copy = OLD_COPY_DIR.search(rel) and any(_in_folder_of(rel, k) for k in kept)
        if old_copy or (not _is_supplement(rel)
                        and any(os.path.dirname(k) == folder and not _is_supplement(k)
                                for k in kept)):
            out.add(rel)
            LOG.warning("%s is a full LaTeX document but is not read: it looks like an "
                        "old copy of %s", rel, ", ".join(kept))
        else:
            kept.append(rel)
    used_dirs = {os.path.dirname(i) for i in included}
    for rel in texs:
        if rel in out or rel in kept or any(rel == f for _, f in full):
            continue
        if kept and (any(os.path.dirname(k) and _in_folder_of(rel, k) for k in kept)
                     or os.path.dirname(rel) in used_dirs - {""}):
            out.add(rel)
    return out


def _is_supplement(rel: str) -> bool:
    """True if the file name of `rel` names a supplement or an appendix."""
    return bool(SUPPLEMENT_NAME.search(os.path.basename(rel)))


def _in_folder_of(rel: str, main: str) -> bool:
    """True if `rel` sits in the folder of `main` or below it (`within_scope`)."""
    return within_scope(rel, (os.path.dirname(main) or WHOLE,))


def folder_candidates(root: str, exclude: tuple[str, ...] = (),
                      within: tuple[str, ...] = (WHOLE,)) -> list[dict]:
    """Every candidate in the folder: each note's, then each commit message's.

    With `within`, only the notes in those files and folders give candidates. Every commit
    message gives candidates in any case.

    A .tex file that another one includes is read only through that file, and a .tex file
    that is not part of the compiled paper (see `unused_tex`) is not read. A note candidate
    with the same text as one in an earlier note (an old draft that repeats the paper, say)
    is left out, so the agent answers each text one time. A repeat inside one note is kept,
    with its own id: the same row in two tables can be two claims.
    """
    rels = note_files(root, exclude, within=within)
    skip = included_tex(root, rels)
    named = {w for w in within if w != WHOLE}          # a file the scope names is always read
    skip |= unused_tex(root, rels, skip) - named
    out, seen = [], set()
    for rel in rels:
        if rel in skip:
            continue
        found = note_candidates(root, rel)
        keys = [(c["kind"], " ".join(c["text"].split())) for c in found]
        out += [c for c, key in zip(found, keys, strict=True) if key not in seen]
        seen.update(keys)
    return out + commit_candidates(root)


def path_pattern(rel: str) -> str:
    """Replace the numbers that change between copies of one file with '*'.

    Only the file name and replica folders (s1, iter3, ...) are generalised; other folder names
    keep their digits (t1-c1, llama-3.1-8b), so the agent copies a real path, not a broad glob.
    """
    parts = rel.split(os.sep)
    parts = [re.sub(r"\d+", "*", p) if REPLICA_DIR.match(p) else p for p in parts[:-1]] + \
        [re.sub(r"\d+", "*", parts[-1])]
    return os.sep.join(parts)


def data_file_paths(root: str, suffixes: tuple[str, ...]) -> list[str]:
    """The files under `root` with one of `suffixes`, relative to `root`, sorted.

    Symlinks are followed. A folder that the walk already went through by another path (for
    example a link `latest -> .` or `latest -> run_17`) is gone through once, under its real
    path. A link to a folder outside `root`, a link to a file that does not exist, the
    folders of SKIP_DIRS and the state folder of the Verifier are left out.
    """
    paths, seen = [], set()
    real_root = os.path.realpath(root)
    for dirp, dirs, files in os.walk(root, followlinks=True):
        real = os.path.realpath(dirp)
        outside = real != real_root and not real.startswith(real_root + os.sep)
        if real in seen or outside or os.sep + STATE_DIR_NAME in dirp + os.sep:
            dirs[:] = []
            continue
        seen.add(real)
        # real folders before links, so a file is listed under its real path, not under a
        # link such as `latest` that a later run moves
        dirs[:] = sorted((d for d in dirs if d not in SKIP_DIRS),
                         key=lambda d: os.path.islink(os.path.join(dirp, d)))
        paths += [os.path.relpath(os.path.join(dirp, f), root) for f in files
                  if f.endswith(suffixes) and os.path.isfile(os.path.join(dirp, f))]
    return sorted(paths)


def data_files(root: str, exclude: tuple[str, ...] = ()) -> dict:
    """Group the data files under `root` by `path_pattern`, and follow symlinks.

    Returns {"groups": {pattern: {"files": n, "examples": [...], "first": description}},
    "not_listed": groups beyond MAX_FILES, "excluded": files skipped}. One file per group is
    opened and described (extract.describe_data_file); files over MAX_BYTES are listed but not
    opened. `exclude` holds glob patterns (relative to `root`) for files the method writer must
    not see, such as a hand-made inventory that already records the paper's numbers.
    """
    groups: dict[str, list[str]] = {}
    excluded = 0
    for rel in data_file_paths(root, extract.DATA_SUFFIXES):
        if any(fnmatch.fnmatch(rel, pat) for pat in exclude):
            excluded += 1
            continue
        groups.setdefault(path_pattern(rel), []).append(rel)
    listed = {}
    for pat in sorted(groups)[:MAX_FILES]:
        rels = groups[pat]
        first = os.path.join(root, rels[0])
        desc = ({"too_large_to_open": os.path.getsize(first)} if os.path.getsize(first) > MAX_BYTES
                else extract.describe_data_file(first))
        listed[pat] = {"files": len(rels), "examples": rels[:3], "first": desc}
    return {"groups": listed, "not_listed": max(0, len(groups) - MAX_FILES), "excluded": excluded}


def matches(root: str, pattern: str) -> list[str]:
    """The files a register path (relative to `root`, may be a glob) matches."""
    return sorted(glob.glob(os.path.join(root, pattern)))


def python_files_in_folder(directory: Path) -> list[Path]:
    """The Python files directly in a directory, sorted, or none if it does not exist."""
    return sorted(p for p in directory.glob("*.py") if not p.name.startswith("_"))
