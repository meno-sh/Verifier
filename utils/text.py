"""Text utilities that hold no rule of the Verifier: one line, cuts, Markdown and Typst."""
from __future__ import annotations

import re


def on_one_line(text: str) -> str:
    """`text` on one line, so it cannot break a list item or a table row."""
    return " ".join(text.split())


def paragraphs_and_bullets(text: str) -> list[tuple[bool, str]]:
    """The parts of `text` as (is a bullet, text on one line), in order.

    A line that starts with "- " is one bullet, and an indented line after it continues it.
    The other lines between bullets join into one paragraph. A model writes its lists this
    way, and the reports show them as lists.
    """
    parts: list[tuple[bool, str]] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("- "):
            parts.append((True, on_one_line(stripped[2:])))
        elif parts and (not parts[-1][0] or line[:1].isspace()):
            parts[-1] = (parts[-1][0], parts[-1][1] + " " + on_one_line(stripped))
        else:
            parts.append((False, on_one_line(stripped)))
    return parts


def comparable(text: str) -> str:
    """Text for finding identical items: lower case, no list marker, markdown or extra space."""
    t = re.sub(r"^\s*[-•]\s+", "", text.lower())
    t = re.sub(r"[*`]|(?<!\w)_|_(?!\w)", "", t)
    return re.sub(r"\s+", " ", t).strip(" .;")


def first_part(text: str, limit: int) -> str:
    """The first `limit` characters of `text`, and a marker that says how many were cut."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} [... {len(text) - limit} more characters cut]"


def last_part(text: str, limit: int) -> str:
    """The last `limit` characters of `text`, after a marker that says how many were cut."""
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} earlier characters cut] {text[-limit:]}"


def markdown_table_cell(text: str) -> str:
    """`text` for a cell of a Markdown table."""
    return on_one_line(text).replace("|", "\\|")


def markdown_code_span(text: str) -> str:
    """`text` as a Markdown code span, whatever backticks it holds."""
    text = on_one_line(text)
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def typst_literal(value: object) -> str:
    """`value` as a Typst literal: none, a bool, a number, a string, an array or a dictionary.

    Raises:
        TypeError: `value` is of another type.
    """
    match value:
        case None:
            return "none"
        case bool():
            return "true" if value else "false"
        case int() | float():
            return repr(value)
        case str():
            return '"' + "".join(typst_escaped_character(c) for c in value) + '"'
        case list() | tuple():
            # a one-item array needs a trailing comma, as in Python
            items = [typst_literal(v) for v in value]
            return "(" + ", ".join(items) + ("," if len(items) == 1 else "") + ")"
        case dict():
            if not value:
                return "(:)"
            return "(" + ", ".join(f"{typst_literal(str(k))}: {typst_literal(v)}"
                                   for k, v in value.items()) + ")"
    raise TypeError(f"no Typst literal for {type(value).__name__}: {value!r}")


def typst_escaped_character(c: str) -> str:
    """One character of a Typst string, escaped when it must be."""
    if c in '\\"':
        return "\\" + c
    if c == "\n":
        return "\\n"
    if ord(c) < 0x20 or ord(c) == 0x7f:
        return f"\\u{{{ord(c):x}}}"
    return c
