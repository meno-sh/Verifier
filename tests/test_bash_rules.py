#!/usr/bin/env python3
"""Check the agent's shell rules (runtime/agent/opencode.py bash_rules): few rules, same verdicts.

OpenCode echoes every shell rule in the message it gives the agent when it refuses a
command, so the rules must be few. Each case names a command and the verdict the rules
must give it, as OpenCode 1.19.0 picks a rule (`verdict`; the contract test
tests/test_opencode_contract.py checks the same against the real binary). Returns 1 if
any case fails.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from runtime.agent import opencode  # noqa: E402

FAILED: list[str] = []
MAX_RULES = 210


def pattern_matches(pattern: str, command: str) -> bool:
    """True if a permission pattern matches the command, as OpenCode 1.19.0 matches it.

    `*` matches any text, spaces too, and `?` one character. The pattern must match the
    whole command. A pattern ending in ` *` also matches without that part, so
    `git commit *` matches `git commit`.
    """
    regex = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    if regex.endswith(r"\ .*"):
        regex = regex[:-4] + "( .*)?"
    return re.fullmatch(regex, command, re.DOTALL) is not None


def verdict(rules: dict[str, str], command: str) -> str | None:
    """What OpenCode does with one command under these rules: the last matching rule's action.

    None when no rule matches. tests/test_opencode_contract.py checks real refusals.
    """
    found = None
    for pattern, action in rules.items():
        if pattern_matches(pattern, command):
            found = action
    return found


# (command, verdict). Git writes are refused alone and after global options; reads run.
CASES = [
    ("git commit -m x", "deny"),
    ("git commit", "deny"),
    ("git -C checkout commit -q --allow-empty -m sneaky", "deny"),
    ("git --no-pager push origin main", "deny"),
    ("git -P checkout main", "deny"),
    ("git -p -C . reset --hard HEAD~1", "deny"),
    ("git -c user.name=x commit -m y", "deny"),
    ("git -C a -c b=c --no-pager stash", "deny"),
    ("git bisect start", "deny"),
    ("git worktree add ../x", "deny"),
    ("git update-ref refs/heads/main HEAD~1", "deny"),
    ("git sparse-checkout set x", "deny"),
    ("git log --oneline -5", "allow"),
    ("git -C checkout log -1 --format=%h", "allow"),
    ("git --no-pager diff HEAD~1", "allow"),
    ("git show HEAD:notes.md", "allow"),
    ("git status", "allow"),
    ("git -C checkout ls-files", "allow"),
    ("git grep commit", "allow"),
    ("git log --grep=push", "allow"),
    ("git rev-parse HEAD", "allow"),
    ("git blame notes.md", "allow"),
    ("python ../run.py", "deny"),
    ("python3 /tmp/x.py", "deny"),
    ("uv run ../x.py", "deny"),
    ("bash /etc/x.sh", "deny"),
    ("python -c 'print(1)'", "deny"),
    ("python3 -c 'import os'", "deny"),
    ("node -e 'x'", "deny"),
    ("sh -c 'ls'", "deny"),
    ("bash -c 'ls'", "deny"),
    ("perl -e 1", "deny"),
    ("ruby -e 1", "deny"),
    ("../run.sh", "deny"),
    ("/bin/rm -rf x", "deny"),
    ("python methods/m.py", "allow"),
    ("python3 tmp/label.py", "allow"),
    ("vcheck check-quotes", "allow"),
    ("vcheck check-register", "allow"),
    ("vjob start sleeper -- sleep 300", "allow"),
    ("ls -la", "allow"),
    ("cat claim_candidates.json", "allow"),
    ("jq . claim_quotes.json", "allow"),
]


def expect(name: str, ok: bool, detail: str = "") -> None:
    """Record one case."""
    print(f"  {'ok  ' if ok else 'FAIL'}  {name}" + ("" if ok else f"  ({detail})"))
    if not ok:
        FAILED.append(name)


def main() -> int:
    """Run every case and report."""
    rules = opencode.bash_rules()
    expect(f"the rules are few (at most {MAX_RULES}), so a refusal's message is short",
           len(rules) <= MAX_RULES, f"{len(rules)} rules")
    expect('the first rule allows everything, so the denials after it win ("*": allow)',
           next(iter(rules.items())) == ("*", "allow"), repr(next(iter(rules.items()))))
    for command, want in CASES:
        got = verdict(rules, command)
        expect(f"`{command}` is {want}", got == want, f"got {got}")
    expect("a pattern's trailing ` *` also matches the bare command, as in OpenCode",
           verdict({"*": "allow", "git commit *": "deny"}, "git commit") == "deny")
    expect("a `*` matches spaces, as in OpenCode",
           verdict({"*": "allow", "git -* reset *": "deny"}, "git -C a b reset") == "deny")
    print("all passed" if not FAILED else f"{len(FAILED)} FAILED")
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
