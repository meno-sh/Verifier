"""Read LaTeX and Markdown files into a `Paper` and make its claim candidates.

`coverage` checks the agent's answers. `describe_data_file` describes one data file.
"""
import csv
import hashlib
import itertools
import json
import os
import re
from collections.abc import Collection, Iterable, Iterator, Mapping, Sequence
from enum import Enum

from schema.primitives import record
from utils import model_names
from utils.text import first_part

# A name may be written with spaces between its parts (see models_in).
TOKEN_PAT = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?")
MAX_WORDS_IN_NAME = 4        # "Llama 3.1 8B Instruct"


def models_in(text: str) -> list[str]:
    """The known model names a piece of text mentions, as the list spells them.

    A name may be written with spaces ("Llama 3.1 8B"), and a single word may carry
    extra parts after a known name ("Qwen3-8B-sft"). The longest known name at each
    place is taken.
    """
    tokens = list(TOKEN_PAT.finditer(text))
    found, i = set(), 0
    while i < len(tokens):
        for n in range(min(MAX_WORDS_IN_NAME, len(tokens) - i), 0, -1):
            group = tokens[i:i + n]
            if any(text[a.end():b.start()] != " " for a, b in zip(group, group[1:], strict=False)):
                continue
            key = model_names.model_name_key(" ".join(t.group(0) for t in group))
            hit = model_names.KNOWN_NAMES.get(key) if n > 1 else _longest_known(key)
            if hit:
                found.add(hit)
                i += n
                break
        else:
            i += 1
    return sorted(found)


def _longest_known(key: str) -> str | None:
    """The known name that is `key`, or the longest one `key` starts with, part by part."""
    parts = key.split("-")
    for end in range(len(parts), 0, -1):
        name = model_names.KNOWN_NAMES.get("-".join(parts[:end]))
        if name:
            return name
    return None



# Three or four decimals: metrics in this literature are reported at that precision,
# while two decimals is usually a hyperparameter or a figure width.
NUM_PAT = re.compile(r"(?<![\w.])(\d\.\d{3,4})(?![\w])")
# Any printed number, for the candidates, which err towards too much: optional sign
# (ASCII or unicode minus, but not the second dash of a range "0.2--0.3"), digits,
# optional decimals, and a sentence's closing period allowed after it. A comma is a
# thousands separator only when 1 to 3 digits come before it, exactly 3 after each comma,
# and no other ",digit" follows: "1,024" is one number, while in "256,512,1024" the last
# group is not 3 digits, so it is a list and each item is a number of its own.
# A unit printed right after a claim's value ("0.19s", "12ms", "3x faster", "8GB"); the value
# is the number. A model size ("7B", "70M") or a count ("1.2k") is often a name, or its value
# is not the printed number, so B, M and k are not units here.
CLAIM_UNIT = r"(?:ms|s|min|h|x|[KMGT]B)(?![A-Za-z0-9])"


def number_pattern(unit: str | None = None) -> re.Pattern[str]:
    """A number as printed, with a letter or a digit directly before or after it refused.

    A number of a scientific notation ("5e-4") is refused too. With `unit`, a number with
    that unit directly after it is read, without the unit.
    """
    word_after = rf"(?!{unit})\w" if unit else r"\w"
    return re.compile(
        r"(?<![\w.])(?<!\d[eE][+\-−])((?:(?<!-)[+\-−])?"
        rf"(?:\d{{1,3}}(?:,\d{{3}})+(?:\.\d+)?(?!{word_after}|\.\d|,\d)"
        rf"|(?:\d+(?:\.\d+)?|\.\d+)(?!{word_after}|\.\d)))")


NUMBER = number_pattern()
# The numbers that can be a claim's value: as NUMBER, and also a number with a unit.
CLAIM_NUMBER = number_pattern(CLAIM_UNIT)
MIN_SENTENCE = 25          # shorter fragments (headings, list labels) are not sentences
# How `value_marked_text` hides values from a reader that must not see them (the method
# writer): the entry's own value, and the values of its sister entries.
OWN_VALUE_HIDDEN = "{{VALUE}}"
SISTER_VALUE_HIDDEN = "*redacted*"

# Lengths, graphics options and float placement are formatting, not claims.
LATEX_NOISE = re.compile(r"\\includegraphics(\[[^\]]*\])?\{[^}]*\}|"
                         r"width\s*=\s*[^,\]\s]+|\\linewidth|\\textwidth|\[[thbp!]+\]")


def numbers(text: str) -> list[str]:
    """Every number printed in `text`, as written (a unicode minus becomes '-')."""
    return [n.replace("−", "-") for n in NUMBER.findall(text)]


def _strip_comments(t: str) -> str:
    r"""Remove LaTeX line comments. Keep each escaped \% sign."""
    return "\n".join(re.sub(r'(?<!\\)%.*', '', line) for line in t.split("\n"))


def _denoise(t: str) -> str:
    """Drop formatting constructs whose numbers would otherwise look like claims."""
    return LATEX_NOISE.sub(" ", t)


SIGNS = {
    "times": "×", "cdot": "·", "div": "÷", "leq": "≤", "le": "≤", "leqslant": "≤",
    "geq": "≥", "ge": "≥", "geqslant": "≥", "lt": "<", "gt": ">", "approx": "≈",
    "lesssim": "≲", "gtrsim": "≳", "propto": "∝", "neq": "≠", "ne": "≠", "sim": "~",
    "ll": "≪", "gg": "≫", "infty": "∞", "uparrow": "↑", "downarrow": "↓", "Uparrow": "↑",
    "Downarrow": "↓", "nabla": "∇", "partial": "∂", "sum": "∑", "prod": "∏", "int": "∫",
    "cup": "∪", "cap": "∩", "in": "∈", "notin": "∉", "subset": "⊂", "subseteq": "⊆",
    "forall": "∀", "exists": "∃", "mid": "|", "cdots": "⋯", "ldots": "…", "dots": "…",
    "circ": "∘", "odot": "⊙", "otimes": "⊗", "oplus": "⊕", "perp": "⊥", "top": "⊤",
    "emptyset": "∅", "varnothing": "∅",
}
# Named operators stay as words: "\\exp" is "exp", so "1 - 2.5 exp[...]" keeps its exponent.
OPERATORS = re.compile(r"\\(exp|log|ln|max|min|sup|inf|lim|det|arg|argmin|argmax|sin|cos|tan"
                       r"|tanh|dim|ker|Pr|softmax|sigmoid|diag|tr|rank|sgn|sign)(?![A-Za-z])")


def _argument(s: str, i: int) -> tuple[str, int]:
    r"""One argument of a command at s[i]: a brace group, or one token (\frac12, \sqrt\pi).

    Without braces, LaTeX takes one character or one command name as the argument.
    Returns the argument's text and the index after it.
    """
    if i < len(s) and s[i] == "{":
        return _braced(s, i)
    token = re.match(r"\\[A-Za-z]+|\\.|[^\s{}\\]", s[i:])
    if not token:
        raise ValueError("no argument at this index")
    return token.group(0), i + token.end()


def _fracs(s: str) -> str:
    r"""\frac{a}{b} as (a)/(b), and \sqrt{x} as √(x), inner ones first.

    An argument without braces is one token, so \frac12 is (1)/(2) and \sqrt2 is √(2).
    """
    pat = re.compile(r"\\(?:d|t)?frac(?![A-Za-z])|\\sqrt(?![A-Za-z])")
    for _ in range(200):
        found = list(pat.finditer(s))
        if not found:
            return s
        m = found[-1]
        try:
            if m.group(0).startswith("\\sqrt"):
                x, end = _argument(s, _skip_options(s, m.end()))
                s = s[:m.start()] + f"√({x})" + s[end:]
            else:
                a, i = _argument(s, _skip_options(s, m.end()))
                b, end = _argument(s, _skip_options(s, i))
                s = s[:m.start()] + f"({a})/({b})" + s[end:]
        except ValueError:
            s = s[:m.start()] + " " + s[m.end():]
    return s
SPACED_SIGNS = {"pm": "±", "mp": "∓", "to": "→"}
GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε", "varepsilon": "ε",
    "zeta": "ζ", "eta": "η", "theta": "θ", "vartheta": "ϑ", "iota": "ι", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "varpi": "ϖ", "rho": "ρ",
    "varrho": "ϱ", "sigma": "σ", "varsigma": "ς", "tau": "τ", "upsilon": "υ", "phi": "φ",
    "varphi": "φ", "chi": "χ", "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ",
    "Theta": "Θ", "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Upsilon": "Υ",
    "Phi": "Φ", "Psi": "Ψ", "Omega": "Ω",
}
# A sign or a Greek letter carries the claim ("≤ 0.1", "64×64", "λ = 0.5"), so it becomes
# its symbol, and `_plain` deletes every other command. A Greek letter is a word character,
# so it gets a space before it: "3σ" would hide the 3 from `numbers`.
MATH_SYMBOLS = (SIGNS | {k: f" {v} " for k, v in SPACED_SIGNS.items()}
                | {k: f" {v}" for k, v in GREEK.items()})
# Not after a backslash: in `\\\\ge 5` the `\\\\` is a line break, not part of a command.
MATH_SYMBOL = re.compile(r"(?<!\\)\\(" + "|".join(sorted(MATH_SYMBOLS, key=len, reverse=True))
                         + r")(?![A-Za-z])")
# The signs `_plain` writes that the comparison text keeps, written with no space around
# them there, so "3 × faster" and "3× faster" compare equal. ≈, → and ± were kept before.
KEPT_SIGNS = "".join(sorted(set("".join(SIGNS.values()) + "".join(SPACED_SIGNS.values()))
                            - set("≈→±<>")))
NORM_DROP = re.compile(r"[^\w.%+\-'\"<>=≈→±/()" + re.escape(KEPT_SIGNS) + r"]")
NORM_SIGN_SPACE = re.compile(r"\s*([" + re.escape(KEPT_SIGNS) + r"])\s*")


def _plain(text: str) -> str:
    """LaTeX reduced to readable text, with signs, Greek letters and arrows kept."""
    s = re.sub(r"\\(?:begin|end)\{[^}]*\}", " ", LAYOUT.sub(" ", text))
    s = s.replace("\\$", "\x00").replace("$", " ").replace("\x00", "$")
    s = re.sub(r"\\(?:mathbf|textbf|emph|textit|underline|text|mathrm|texttt)\{([^{}]*)\}",
               r"\1", s)
    s = s.replace("~", " ")
    s = OPERATORS.sub(r" \1 ", _fracs(s)).replace("\\|", "‖")
    s = s.replace("\\{", " ").replace("\\}", " ")        # set braces: B ∪ {x} reads B ∪ x
    for esc, ch in (("\\%", "%"), ("\\&", "&"), ("\\_", "_"), ("\\#", "#")):
        s = s.replace(esc, ch)
    s = MATH_SYMBOL.sub(lambda m: MATH_SYMBOLS[m.group(1)], s)
    s = s.replace("\\!", "").replace("\\,", " ").replace("\\ ", " ").replace("\\quad", " ")
    s = re.sub(r"\\[a-zA-Z]+", " ", s)
    s = s.replace("{", "").replace("}", "")
    return re.sub(r" ([,;:.)])", r"\1", " ".join(s.split()))


class PaperFormat(Enum):
    """The formats a paper file can be written in. Each has its own reader."""
    LATEX = "latex"
    MARKDOWN = "markdown"


# The extensions each format is recognised by, compared without regard to case. R
# Markdown and Quarto files are Markdown with code chunks, which are not read.
EXTENSIONS = {
    PaperFormat.LATEX: (".tex", ".ltx", ".latex"),
    PaperFormat.MARKDOWN: (".md", ".markdown", ".mdown", ".mkd", ".mkdn", ".rmd", ".qmd"),
}


class UnknownPaperFormat(ValueError):
    """A paper file whose extension names no format the Verifier reads."""


def paper_format(path: str) -> PaperFormat:
    """A file's format, from its extension.

    Raises:
        UnknownPaperFormat: when the extension is neither a LaTeX nor a Markdown one.
            Reading such a file with a guessed reader would check the wrong text.
    """
    for fmt, extensions in EXTENSIONS.items():
        if path.lower().endswith(extensions):
            return fmt
    known = ", ".join(e for extensions in EXTENSIONS.values() for e in extensions)
    raise UnknownPaperFormat(f"{os.path.basename(path)} is not a paper file this tool "
                             f"reads; a paper file ends in one of {known}")



@record
class PaperStatement:
    """A sentence of the paper that carries a number or a model name."""
    text: str
    numbers: tuple[str, ...]
    models: tuple[str, ...]


@record
class PaperTable:
    """One table of the paper.

    `row_models` is kept separate from `caption_models` on purpose. A caption saying "all
    rows are Llama" is a claim *about* the table; the row cells are evidence *from* it.
    Conflating the two is how a block mixing two model families came to be labelled with
    one model's name. `unnamed_rows` counts the data rows (every row after the header)
    whose cells name no model: for those, the caption is the only place the table says
    which model they are.
    """
    name: str | None
    caption: str
    rows: tuple[tuple[str, ...], ...]           # the header row first
    sections: tuple[str, ...]                   # the section heading of each row, "" for none
    ncol: int | None                            # the declared column count
    numbers: tuple[str, ...]                    # from the cells and the caption
    row_models: tuple[str, ...]
    caption_models: tuple[str, ...]
    unnamed_rows: int
    carried: tuple[int, ...] = ()               # the rows whose label was carried down


@record
class PaperCaption:
    """The caption of one figure or table."""
    kind: str                                   # "figure" or "table"
    float_name: str | None                      # the name of the figure or the table
    text: str


@record
class PaperEquation:
    """One line of a display equation."""
    label: str | None                           # its \label, if it has one
    numbered: bool                              # LaTeX gives it a number
    number: str | None                          # the number it prints as, when known
    address: str | None                         # "printed" (from the .aux) or "counted"
    latex: str
    text: str                                   # as plain text


@record
class PaperAddress:
    r"""What one \label prints as: its kind, its number, and where the number comes from."""
    label: str
    kind: str                                   # "table", "figure", "section", "equation-line"
    number: str
    source: str                                 # "printed" (from the .aux) or "counted"


@record
class Paper:
    """What the paper checks read, the same whichever format the paper was written in.

    The tables, statements and captions are PaperTable, PaperStatement and PaperCaption.
    """
    formats: frozenset[PaperFormat]
    text: str                         # all readable text: no comments or code, \inputs inlined
    prose: tuple[str, ...]            # body text with tables and figures removed, by block
    sentences: tuple[str, ...]        # every body sentence, from the abstract on, as plain text
    statements: tuple[PaperStatement, ...]  # the sentences with a number or a model name
    tables: tuple[PaperTable, ...]
    captions: tuple[PaperCaption, ...]      # every figure and table caption, in document order
    labels: tuple[str, ...]           # every label or explicit anchor, in document order
    targets: frozenset[str]           # everything a reference can resolve to
    refs: frozenset[str]              # everything the text refers to
    abstract: str                     # "" when the paper has none
    after_abstract: str               # the text after the abstract; all of it without one
    unreadable: tuple[str, ...]       # \input files that could not be read
    equations: tuple[PaperEquation, ...] = ()   # the lines of the display equations
    addresses: tuple[PaperAddress, ...] = ()     # what each \label prints as
    sentence_sections: tuple[str, ...] = ()  # the section heading each sentence sits under


def load(paths: Sequence[str]) -> Paper:
    """Read a paper from one or more files, each with the reader for its format.

    The unreadable list is load-bearing. If a table file is missing then every number
    in it is missing too, and the "is this number in a table" checks would fire across
    the whole paper. The caller must not run those checks on an incomplete source.
    """
    if isinstance(paths, str) or not paths:
        raise TypeError("load takes a non-empty sequence of paths, not one string")
    papers = [READERS[paper_format(p)](p) for p in paths]
    if len(papers) == 1:
        return papers[0]
    return Paper(
        formats=frozenset().union(*(p.formats for p in papers)),
        text="\n\n".join(p.text for p in papers),
        prose=tuple(b for p in papers for b in p.prose),
        sentences=tuple(s for p in papers for s in p.sentences),
        statements=tuple(s for p in papers for s in p.statements),
        tables=tuple(t for p in papers for t in p.tables),
        captions=tuple(c for p in papers for c in p.captions),
        labels=tuple(label for p in papers for label in p.labels),
        targets=frozenset().union(*(p.targets for p in papers)),
        refs=frozenset().union(*(p.refs for p in papers)),
        abstract=" ".join(p.abstract for p in papers if p.abstract),
        after_abstract="\n\n".join(p.after_abstract for p in papers),
        unreadable=tuple(u for p in papers for u in p.unreadable),
        equations=tuple(e for p in papers for e in p.equations),
        addresses=tuple(a for p in papers for a in p.addresses),
        sentence_sections=tuple(s for p in papers for s in p.sentence_sections))


# A sentence ends after . ! or ? and white space, but not after an abbreviation that
# scientific text puts mid-sentence ("et al.", "e.g.", "Fig.", "Eq.", "vs."). "etc." and a
# single capital ("Appendix A.") can end a sentence, so they are not on the list.
ABBREVIATIONS = ("al", "e.g", "i.e", "eg", "ie", "cf", "vs", "approx", "resp", "Fig", "Figs",
                 "Eq", "Eqs", "Sec", "Secs", "Tab", "Tabs", "Appx", "App", "Alg", "Thm", "Def",
                 "Prop", "Lem", "Cor", "No", "Nos", "Ref", "Refs", "Dr", "Prof", "Mr", "Ms", "St",
                 "et", "w.r.t", "a.k.a", "i.i.d", "Ch", "Vol", "pp", "Corp", "Inc", "Ltd")
NO_END = re.compile(r"(?:^|[\s(\[])(?:" + "|".join(re.escape(a) for a in ABBREVIATIONS)
                    + r")\.$")


def _split_sentences(block: str) -> list[str]:
    """The block cut after each . ! or ? plus white space, except after an abbreviation."""
    out, start = [], 0
    for m in re.finditer(r"(?<=[.!?])\s+", block):
        if NO_END.search(block[max(start, m.start() - 12):m.start()]):
            continue
        out.append(block[start:m.start()])
        start = m.end()
    out.append(block[start:])
    return out


def _short_claim(s: str) -> bool:
    """A short sentence that is still a claim: a number, three words or more, a full stop.

    "We reach 91.2% accuracy." is shorter than MIN_SENTENCE but is the paper's result.
    """
    return bool(re.search(r"\d", s)) and len(s.split()) >= 3 and s.endswith((".", "!", "?"))


def sentences(blocks: Iterable[str]) -> list[str]:
    """Every sentence of body text, as plain text, numbers or not.

    `blocks` are pieces of body text with tables and figures already removed; a
    sentence never runs across two blocks. Fragments shorter than MIN_SENTENCE
    characters (headings, list labels) are left out, unless they are a short claim.
    """
    out = []
    for block in blocks:
        for raw in _split_sentences(block):
            s = _plain(_denoise(raw)).lstrip(". ").strip()
            if len(s) >= MIN_SENTENCE or _short_claim(s):
                out.append(s)
    return out


def statements(sents: Iterable[str]) -> list[PaperStatement]:
    """The sentences that carry a number or a model name.

    This is co-occurrence, not comprehension: it records that a sentence contains
    a number and a model name, not what the sentence claims about them. That gap
    is the main thing a language model would close.
    """
    out = []
    for s in sents:
        nums = sorted(set(NUM_PAT.findall(s)))
        mods = models_in(s)
        if nums or mods:
            out.append(PaperStatement(text=s, numbers=tuple(nums), models=tuple(mods)))
    return out


def _table(name: str | None, caption: str, rows: list[list[str]], ncol: int | None,
           sections: list[str], carried: set[int] | frozenset[int] = frozenset()
           ) -> PaperTable:
    """One table. Numbers count from the caption too; models from each separately."""
    cells_only = " ".join(" ".join(r) for r in rows)
    return PaperTable(name=name, caption=caption, rows=tuple(map(tuple, rows)),
                      sections=tuple(sections), ncol=ncol,
                      numbers=tuple(sorted(set(NUM_PAT.findall(caption + " " + cells_only)))),
                      row_models=tuple(models_in(cells_only)),
                      caption_models=tuple(models_in(caption)),
                      unnamed_rows=sum(1 for r in rows[1:] if not models_in(" ".join(r))),
                      carried=tuple(sorted(carried)))


def _caption(kind: str, name: str | None, text: str) -> PaperCaption:
    """One caption."""
    return PaperCaption(kind=kind, float_name=name, text=text)


# --------------------------------------------------------------------- LaTeX

# The float environments, by family; each may have a star. Every regex that names a
# float is built from these, so a reader that knows one float knows it everywhere.
TABLE_FLOATS = ("table", "wraptable", "sidewaystable")
FIGURE_FLOATS = ("figure", "wrapfigure", "sidewaysfigure")
# The tabular environments; a longtable is a table without a float around it.
TABULARS = ("tabular", "tabular*", "tabularx", "longtable")


def _names(names: Iterable[str], star: bool = True) -> str:
    """A regex alternation of environment names, each with an optional star."""
    return "(?:" + "|".join(re.escape(n) for n in names) + ")" + (r"\*?" if star else "")


TABLE_ENVS = _names(TABLE_FLOATS)
FIGURE_ENVS = _names(FIGURE_FLOATS)
LATEX_FLOATS = re.compile(r"\\begin\{(" + TABLE_ENVS + "|" + FIGURE_ENVS + "|"
                          + _names(TABULARS, star=False) + r")\}.*?\\end\{\1\}", re.S)
# The reader does not read tables that a macro makes.
LATEX_TABLE = re.compile(r"\\begin\{(" + TABLE_ENVS + r")\}(.*?)\\end\{\1\}", re.S)
LATEX_FIGURE = re.compile(r"\\begin\{(" + FIGURE_ENVS + r")\}(.*?)\\end\{\1\}", re.S)
# The rule commands of a table, each with the arguments that follow its name.
TABLE_RULES = {
    "toprule": "", "midrule": "", "bottomrule": "", "hline": "",
    "cmidrule": r"(?:\([^)]*\))?\{[^}]*\}", "cline": r"\{[^}]*\}",
    "specialrule": r"\{[^}]*\}\{[^}]*\}\{[^}]*\}", "Xhline": r"\{[^}]*\}", "hhline": r"\{[^}]*\}",
    "cdashline": r"\{[^}]*\}(?:\s*\[[^\]]*\])?", "hdashline": r"(?:\s*\[[^\]]*\])?",
}
# A full rule ends the header or a group of rows; a partial rule underlines part of a
# row. The rules that take a size option, such as \toprule[1.2pt].
FULL_RULES = ("midrule", "hline", "hdashline", "specialrule", "Xhline", "hhline")
PARTIAL_RULES = ("cmidrule", "cline", "cdashline")
SIZED_RULES = ("toprule", "midrule", "bottomrule", "specialrule", "cmidrule")
LATEX_RULES = re.compile(r"\\(?:" + "|".join(name + args for name, args in TABLE_RULES.items())
                         + ")")
LATEX_LABEL = re.compile(r"\\label\{([^}]+)\}")
# A label given as an environment option, as in \begin{lstlisting}[label={lst:add}].
LATEX_OPTION_LABEL = re.compile(r"\\begin\{[A-Za-z]+\*?\}\[[^\]]*?\blabel\s*=\s*\{?([^,}\]]+)")
# A command that refers to labels: \ref and every command whose name ends in "ref" but
# \href and \hyperref (\cref, \autoref, \eqref, or a paper's own \figref or \pref), with an
# optional argument first; and \hyperref[label]. The braces hold one label or a comma list.
LATEX_REF = re.compile(r"\\(?!h(?:yper)?ref\b)[A-Za-z]*ref\*?(?:\[[^\]]*\])?\{([^}]+)\}"
                       r"|\\hyperref\[([^\]]+)\]")
LATEX_INCLUDE = re.compile(r"\\(?:input|include)\{([^}]+)\}")
# Citations and cross-references carry numbers that are not claims; a sectioning
# command, list item or algorithm step ends the sentence before it, and its title is a
# fragment of its own. Algorithm floats stay in the text, so each step is a sentence.
LATEX_NOT_CLAIMS = re.compile(r"\\(?:cite[pt]?|ref|eqref|label|url|href)\{[^}]*\}")
LATEX_BREAKS = re.compile(
    r"\\(?:item|section|subsection|subsubsection|paragraph"
    r"|State|STATE|Statex|Require|REQUIRE|Ensure|ENSURE|Return|RETURN)(?![A-Za-z])\*?"
    r"(?:\{([^}]*)\})?")


def _as_prose(text: str) -> str:
    """Text without citations and references, with a sentence break at each item or heading.

    A heading's title becomes a sentence of its own.
    """
    return LATEX_BREAKS.sub(lambda m: f"\n. {(m.group(1) or '').rstrip('.')}. ",
                            LATEX_NOT_CLAIMS.sub(" ", text))


MACRO_DEF = re.compile(r"\\(?:re)?newcommand\*?\s*\{?\\([A-Za-z]+)\}?\s*(\[\d\])?\s*(?=\{)"
                       r"|\\def\s*\\([A-Za-z]+)\s*(?=\{)"
                       r"|\\DeclareMathOperator\*?\s*\{?\\([A-Za-z]+)\}?\s*(?=\{)")
MAX_MACRO_TEXT = 60


def _latex_refs(text: str) -> frozenset[str]:
    """Every label the text refers to (LATEX_REF).

    A key with #1 is the parameter of a macro definition, not a reference.
    """
    return frozenset(_label_key(k) for m in LATEX_REF.finditer(text)
                     for k in (m.group(1) or m.group(2)).split(",") if k.strip() and "#" not in k)


def _label_key(key: str) -> str:
    """A label or reference key as LaTeX compares it: a line break inside it is one space."""
    return " ".join(key.split())


def _expand_macros(text: str) -> str:
    r"""Replace each short text macro with its text, so that a method name stays in the sentence.

    Only a macro with no arguments whose body is short text counts, for example
    \newcommand{\method}{Vector-ICL\xspace}. Other macros stay as they are.
    """
    macros = {}
    for m in MACRO_DEF.finditer(text):
        name = m.group(1) or m.group(3) or m.group(4)
        if m.group(2) and m.group(2) != "[0]":      # [0] declares no arguments
            continue
        try:
            body, _ = _braced(text, m.end())
        except ValueError:
            continue
        body = body.replace("\\xspace", "").strip()
        if m.group(4):                              # an operator name such as arg\,min
            body = re.sub(r"\\[,;:!]|\s", "", body)
        if 0 < len(body) <= MAX_MACRO_TEXT and "#" not in body and re.search(r"[A-Za-z]", body):
            macros[name] = body
    for name, body in sorted(macros.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(r"\\" + name + r"(?![A-Za-z])(\{\})?", lambda _m, b=body: b, text)
    return text


# The endings of the files that a paper \inputs as graphics, not as text.
GRAPHIC_INPUT_ENDINGS = (".pgf", ".tikz", ".pdf_tex", ".pstex_t", ".eps_tex")


class LatexIncludes:
    r"""The text of a LaTeX file with each file it \input s or \include s inserted.

    Included files are read at any depth, with comments removed first. A path is resolved
    as LaTeX does, from the main file's folder, with ".tex" added when it has no ending; a
    path found only next to the including file is also read. Note discovery
    (utils/structure.py) uses `included` to skip these files, so a file is read once.
    """

    def __init__(self, path: str) -> None:
        """Read `path` and every file it includes."""
        self.root = os.path.dirname(os.path.abspath(path))
        self.missing: list[str] = []            # a missing file, or one that includes itself
        self.included: list[str] = []           # the absolute path of each included file
        with open(path, errors="ignore") as fh:
            main = _strip_comments(fh.read())
        self.text = self._expand(main, self.root, (os.path.normpath(os.path.abspath(path)),))

    def _expand(self, text: str, folder: str, chain: tuple[str, ...]) -> str:
        r"""`text` with each \input{} and \include{} replaced by the included file's text."""
        def sub(m: re.Match[str]) -> str:
            r"""Replace one command with the file's text, or record the file as unreadable."""
            rel = m.group(1).strip()
            if rel.lower().endswith(GRAPHIC_INPUT_ENDINGS):
                return m.group(0)           # a graphic is not text: it is left as it is
            if not rel.endswith(".tex"):
                rel += ".tex"
            p = next((os.path.normpath(os.path.join(base, rel)) for base in (self.root, folder)
                      if os.path.isfile(os.path.join(base, rel))), None)
            if p is None:
                self.missing.append(rel)
                return m.group(0)
            if p in chain:
                self.missing.append(f"{rel} (includes itself)")
                return m.group(0)
            self.included.append(os.path.abspath(p))
            with open(p, errors="ignore") as fh:
                inner = _strip_comments(fh.read())
            return self._expand(inner, os.path.dirname(p), chain + (p,))

        return LATEX_INCLUDE.sub(sub, text)


def read_latex(path: str) -> Paper:
    r"""Read a LaTeX file and insert the text of each file that it \input s or \include s.

    Included files are read at any depth, as `LatexIncludes` finds them. A file that is
    missing, or that includes itself through other files, is listed in `unreadable` and
    its command is left in the text.
    """
    includes = LatexIncludes(path)
    full = _expand_macros(includes.text)
    addresses = _numbering(full, os.path.splitext(path)[0] + ".aux")
    shown = _resolve_refs(full, addresses, _bib_labels(os.path.splitext(path)[0] + ".bbl"))
    body = LATEX_FLOATS.sub(" ", shown)
    # The sentences start at the abstract, past the title and the preamble; with no
    # abstract, at the start of the document.
    start = next((m for m in ("\\begin{abstract}", "\\begin{document}") if m in body), None)
    lead = body[body.find(start):] if start else body
    lead, eqs = _display_equations(lead, addresses)
    lead, notes = _footnotes(lead)
    sents, secs = [], []
    lead = re.sub(r"\\\\(?:\[[^\]]*\])?", "\n. ", lead)   # a forced line break ends a line of text
    for heading, part in _by_section(lead):
        found = sentences((_as_prose(part),))
        sents += found
        secs += [heading] * len(found)
    found = sentences(notes)
    sents += found
    secs += ["footnote"] * len(found)
    abstract = re.search(r"\\begin\{abstract\}(.*?)\\end\{abstract\}", full, re.S)
    # a key with #1 is the parameter of a macro definition, not a label or a reference
    labels = [_label_key(k) for k in LATEX_LABEL.findall(full) + LATEX_OPTION_LABEL.findall(full)
              if "#" not in k]
    refs = _latex_refs(full)
    return Paper(
        formats=frozenset({PaperFormat.LATEX}), text=full, prose=(body,),
        sentences=tuple(sents), statements=tuple(statements(sents)),
        tables=tuple(_latex_tables(shown)), captions=tuple(_latex_captions(shown)),
        labels=tuple(labels), targets=frozenset(labels),
        refs=refs,
        abstract=" ".join(_denoise(abstract.group(1)).split()) if abstract else "",
        after_abstract=(full[full.find("\\end{abstract}"):] if "\\end{abstract}" in full
                        else full),
        unreadable=tuple(includes.missing), equations=tuple(eqs), sentence_sections=tuple(secs),
        addresses=tuple(PaperAddress(label=k, kind=v[0], number=v[1], source=v[2])
                        for k, v in addresses.items()))


REF_CMD = re.compile(r"\\(ref|eqref|cref|Cref|autoref|pageref|nameref|vref)\*?\{([^}]*)\}")
CITE_CMD = re.compile(r"\\(cite|citep|citet|citealp|citealt|citeauthor|citeyear|parencite|textcite"
                      r"|autocite)\*?(?:\[[^\]]*\])*\{([^}]*)\}")
KIND_NAME = {"table": "Table", "figure": "Figure", "equation": "Eq.", "equation-line": "Eq.",
             "section": "Section"}
NUMBERING_EVENT = re.compile(
    r"\\(section|subsection|subsubsection)(\*?)\s*(?:\[[^\]]*\])?\{"
    r"|\\(appendix)(?![A-Za-z])"
    r"|\\begin\{(" + "|".join((*TABLE_FLOATS, *FIGURE_FLOATS, "longtable")) + r")\*?\}"
    r"|\\end\{(" + "|".join((*TABLE_FLOATS, *FIGURE_FLOATS, "longtable")) + r")\*?\}"
    r"|\\begin\{(subfigure|subtable)\}"
    r"|\\label\{([^}]+)\}"
    r"|\\end\{(subfigure|subtable)\}|\\begin\{([A-Za-z]+\*?)\}"
    r"|\\caption(?:of\s*\{(table|figure)\})?(?![A-Za-z*])")
# Environments a label inside of does not name the section it sits in.
NOT_SECTION_ENVS = re.compile(r"theorem|lemma|proposition|corollary|definition|example|remark"
                              r"|assumption|claim|conjecture|exa|thm|lem|prop|cor|defn?|rem"
                              r"|algorithm|enumerate|itemize|description|proof|problem|hypothesis")


def _numbering(full: str, aux: str) -> dict[str, tuple[str, str, str]]:
    r"""What each \label prints as: {label: (kind, number, source)}.

    The number LaTeX printed comes from the paper's .aux file when one sits beside it
    (source "printed"). Otherwise it is counted in document order as LaTeX does by default
    (source "counted"): sections 1, 1.1, ..., appendix sections A, A.1; one table or figure
    number per \caption, a letter per subfigure caption; numbered equation lines in one
    sequence (per section with \numberwithin{equation}{section}). Labels this cannot place
    (a theorem, a list item) are left out. Unlabelled numbered equation lines are listed
    too, keyed by "#eq " and their text, so an equation without a label still gets its number.
    """
    out: dict[str, tuple[str, str, str]] = {}
    start = full.find("\\begin{document}")
    doc = full[start:] if start >= 0 else full
    sec = [0, 0, 0]
    appendix = False
    floats = {"table": 0, "figure": 0}
    in_float, in_sub, sub, last, captioned = None, False, 0, None, False
    eq, by_section = 0, bool(re.search(r"\\numberwithin\{equation\}\{section\}", full))
    spans = [(m.start(), m.end(), m) for m in DISPLAY_MATH.finditer(doc)]
    events = [(m.start(), "event", m) for m in NUMBERING_EVENT.finditer(doc)
              if not any(s <= m.start() < e for s, e, _ in spans)]
    events += [(s, "math", m) for s, _, m in spans]
    for _, what, m in sorted(events, key=lambda x: x[0]):
        if what == "math":
            env, star = m.group(1), m.group(2)
            if not env or star:
                continue
            for _line, _label, numbered, key in _equation_lines(env, star, m.group(3)):
                if not numbered:
                    continue
                eq += 1
                number = f"{_section_number(sec, appendix, 1)}.{eq}" if by_section else str(eq)
                out.setdefault(key, ("equation-line", number, "counted"))
            continue
        level, star, app, fbeg, fend, subenv, label, subend, other, capof = m.groups()
        if m.group(0).startswith("\\caption"):
            # LaTeX numbers a float at its \caption: two captions in one float are two numbers
            kind = capof or in_float
            if kind and in_sub:
                sub += 1        # a subfigure takes the number its float's caption will have
                n = floats[kind] + (0 if captioned else 1)
                last = (kind, f"{n}{'abcdefghijklmnopqrstuvwxyz'[(sub - 1) % 26]}")
            elif kind:
                floats[kind] += 1
                captioned = True
                last = (kind, str(floats[kind]))
            continue
        if subend:
            in_sub = False
            if in_float:
                last = (in_float, str(floats[in_float]))
            continue
        if other:
            if NOT_SECTION_ENVS.match(other.lower()) and not in_float:
                last = None
            continue
        if level:
            if star:
                last = None
                continue
            i = ["section", "subsection", "subsubsection"].index(level)
            sec[i] += 1
            for j in range(i + 1, 3):
                sec[j] = 0
            if i == 0 and by_section:
                eq = 0
            last = ("section", _section_number(sec, appendix, i + 1))
        elif app:
            appendix, sec = True, [0, 0, 0]
        elif fbeg:
            in_float, sub, last = ("table" if "table" in fbeg else "figure"), 0, None
            captioned = False
        elif fend:
            in_float, in_sub, last = None, False, None
        elif subenv:
            in_sub = bool(in_float)
        elif label and last and label not in out:
            out[label] = (last[0], last[1], "counted")
    if os.path.isfile(aux):
        with open(aux, errors="ignore") as fh:
            for key, number in re.findall(r"\\newlabel\{([^}]+)\}\{\{([^{}]*)\}", fh.read()):
                kind = out[key][0] if key in out else "other"
                out[key] = (kind, number, "printed")
    return out


def _section_number(sec: list[int], appendix: bool, depth: int) -> str:
    """A section number such as 3.2, or A.1 in the appendix."""
    head = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"[(sec[0] - 1) % 26] if appendix else str(sec[0])
    return ".".join([head] + [str(x) for x in sec[1:depth]])


def _bib_labels(bbl: str) -> dict[str, str]:
    r"""{citation key: "Zhang et al. (2020)"} from a natbib .bbl beside the paper, or {}."""
    if not os.path.isfile(bbl):
        return {}
    with open(bbl, errors="ignore") as fh:
        text = fh.read()
    out = {}
    for label, key in re.findall(r"\\bibitem\[\{?(.*?)\}?\]\s*\{([^}]+)\}", text):
        m = re.match(r"\s*(.*?)\s*\((\d{4}[a-z]?|n\.d\.)\)", label)
        if m:
            name = " ".join(_plain(m.group(1).replace("~", " ")).split())
            out[key.strip()] = f"{name} ({m.group(2)})"
    return out


def _resolve_refs(text: str, addresses: Mapping[str, tuple[str, str, str]],
                  cites: Mapping[str, str]) -> str:
    r"""The text with each \ref written as LaTeX prints it, and each citation kept.

    \ref{tab:x} becomes 3, \eqref becomes (5), and \cref or \autoref becomes "Table 3" or
    "Eq. (5)". A label with no known number stays as [label]. A citation becomes the author
    and year from the .bbl, "(Zhang et al., 2020)" or for \citet "Zhang et al. (2020)", and
    its key when there is no .bbl, so a claim about another paper keeps who it is about.
    """
    def ref(m: re.Match[str]) -> str:
        cmd, keys = m.group(1), [k.strip() for k in m.group(2).split(",") if k.strip()]
        if cmd == "pageref":
            return ""
        parts = []
        for k in keys:
            kind, number, _ = addresses.get(k, ("", "", ""))
            if not number:
                parts.append(f"[{k}]")
            elif cmd == "eqref" or (cmd != "ref" and kind.startswith("equation")):
                parts.append(f"Eq. ({number})" if cmd != "eqref" else f"({number})")
            elif cmd == "ref":
                parts.append(number)
            else:
                parts.append(f"{KIND_NAME.get(kind, '')} {number}".strip())
        return ", ".join(parts)

    def cite(m: re.Match[str]) -> str:
        cmd, keys = m.group(1), [k.strip() for k in m.group(2).split(",") if k.strip()]
        names = [cites.get(k, k) for k in keys]
        if cmd in ("citet", "textcite"):
            return "; ".join(names)
        if cmd == "citeauthor":
            return "; ".join(re.sub(r"\s*\([^)]*\)$", "", n) for n in names)
        if cmd == "citeyear":
            return "; ".join(re.sub(r"^.*\(([^)]*)\)$", r"\1", n) for n in names)
        short = [re.sub(r"\s*\(([^)]*)\)$", r", \1", n) for n in names]
        return "(" + "; ".join(short) + ")"

    return CITE_CMD.sub(cite, REF_CMD.sub(ref, text))


SECTION_CMD = re.compile(r"\\(section|subsection|subsubsection|paragraph)\*?\s*(?:\[[^\]]*\])?\{")


def _by_section(text: str) -> list[tuple[str, str]]:
    r"""The text cut at each sectioning command, as (heading path, text) pairs.

    The heading path is "Results > Ablations". The part before the first command (the
    abstract, say) has the heading "".
    """
    out, path, pos, heading = [], ["", "", "", ""], 0, ""
    for m in SECTION_CMD.finditer(text):
        try:
            title, end = _braced(text, m.end() - 1)
        except ValueError:
            continue
        out.append((heading, text[pos:m.start()]))
        level = ["section", "subsection", "subsubsection", "paragraph"].index(m.group(1))
        path[level] = _plain(title).rstrip(".")
        for j in range(level + 1, 4):
            path[j] = ""
        heading = " > ".join(p for p in path if p)
        pos = end
    out.append((heading, text[pos:]))
    return out


def _footnotes(text: str) -> tuple[str, list[str]]:
    r"""The text without its \footnote{...}s, and the footnotes, each read as its own block.

    A footnote left in place glues its text into the middle of the sentence it marks.
    """
    notes, out, pos = [], [], 0
    for m in re.finditer(r"\\footnote(?:text)?(?![A-Za-z])", text):
        if m.start() < pos:
            continue
        try:
            note, end = _braced(text, _skip_options(text, m.end()))
        except ValueError:
            continue
        out.append(text[pos:m.start()])
        notes.append(_as_prose(note))
        pos = end
    out.append(text[pos:])
    return "".join(out), notes


DISPLAY_MATH = re.compile(
    r"\\begin\{(equation|align|gather|multline|eqnarray|flalign|alignat)(\*?)\}(.*?)\\end\{\1\2\}"
    r"|(?<!\\)\\\[(.*?)\\\]|\$\$(.*?)\$\$", re.S)
MULTI_LINE_MATH = {"align", "gather", "eqnarray", "flalign", "alignat"}
NO_NUMBER = re.compile(r"\\(?:nonumber|notag)")
# What an equation line carries that is not its text: its number switch and its label.
EQUATION_MARKS = re.compile(r"\\(?:nonumber|notag|label\{[^}]*\})")


def _equation_text(line: str) -> str:
    """One equation line as plain text, without its label, number switch and & marks."""
    return _plain(EQUATION_MARKS.sub(" ", line).replace("&", " "))


def _equation_lines(env: str | None, star: str, body: str
                    ) -> Iterator[tuple[str, str | None, bool, str]]:
    r"""Each non-empty line of one display equation: (line, label, numbered, key).

    An align (or gather, eqnarray) gives one line per \\; any other equation is one
    line. A line is numbered unless its environment is starred, it is \[...\] or $$...$$
    (no `env`), or it has \nonumber or \notag. The key is the line's label, or for a
    line with none "#eq " and its plain text, which `_numbering` and `_display_equations`
    both use, so an unlabelled line finds its number.
    """
    for line in _rows(body) if env in MULTI_LINE_MATH else [body]:
        if not line.strip():
            continue
        found = LATEX_LABEL.search(line)
        label = found.group(1) if found else None
        numbered = bool(env) and not star and not NO_NUMBER.search(line)
        yield line, label, numbered, label or "#eq " + " ".join(_equation_text(line).split())


def _display_equations(text: str, addresses: Mapping[str, tuple[str, str, str]] | None = None
                       ) -> tuple[str, list[PaperEquation]]:
    r"""The text with each display equation taken out, and the equations in order.

    Each equation stays at the end of the sentence that leads into it ("... can be rewritten
    as <equation>"), followed by a sentence break, so it is never glued to the sentence after
    it. It is also its own record, which later gets its number.
    An align (or gather, eqnarray) gives one equation per line. An equation is numbered
    unless its environment is starred, it is \[...\] or $$...$$, or its line has \nonumber
    or \notag. Its \label stays with it.
    """
    eqs = []
    addresses = addresses or {}

    def take(m: re.Match[str]) -> str:
        """Record the equation; leave its text at the end of the sentence that leads into it."""
        env, star = m.group(1), m.group(2)
        body = m.group(3) if env else (m.group(4) or m.group(5) or "")
        for line, label, numbered, key in _equation_lines(env, star, body):
            text_ = _equation_text(line).strip(" ,.;")
            number, source = None, None
            if numbered and key in addresses:
                _, number, source = addresses[key]
            if text_:
                eqs.append(PaperEquation(label=label, numbered=numbered, number=number,
                                         address=source, latex=" ".join(line.split()),
                                         text=text_))
        inline = EQUATION_MARKS.sub(" ", body)
        return " " + inline.replace("\\\\", " ").replace("&", " ") + " .\n. "

    return DISPLAY_MATH.sub(take, text), eqs


# The start of a caption: \caption, \caption* or \captionof{table}, with an optional
# [short caption] that the list of tables prints; the caption text follows in braces.
LATEX_CAPTION_START = re.compile(r"\\caption(?:of\*?\s*\{[^}]*\}|\*)?\s*(?:\[[^\]]*\])?\s*(?=\{)")


def _latex_caption(body: str) -> str:
    r"""Return the text of a float's first caption with balanced braces, or "".

    It reads `\caption{...}`, `\caption[short]{...}`, `\caption*{...}`,
    `\captionof{table}{...}` and `\captionof*{table}{...}`, and gives the long text.
    """
    m = LATEX_CAPTION_START.search(body)
    return "" if m is None else _braced(body, m.end())[0]


def _latex_name(body: str) -> str | None:
    r"""Return a float's \label, or None."""
    lab = LATEX_LABEL.search(body)
    return lab.group(1) if lab else None


def _braced(text: str, i: int) -> tuple[str, int]:
    r"""The text inside the brace group that starts at text[i], and the index after it.

    An escaped brace (\{ or \}) is a printed brace, so it does not open or close a group.
    """
    if i >= len(text) or text[i] != "{":
        raise ValueError("no brace group at this index")
    depth, k = 0, i
    while k < len(text):
        if text[k] == "\\":
            k += 2                      # \{, \} and \\ are one character each
            continue
        depth += {"{": 1, "}": -1}.get(text[k], 0)
        if depth == 0:
            return text[i + 1:k], k + 1
        k += 1
    return text[i + 1:], len(text)


def _skip_options(text: str, i: int) -> int:
    """The index after any [...] options and white space (newlines too) that start at text[i]."""
    while i < len(text) and (text[i] == "[" or text[i].isspace()):
        if text[i] == "[":
            close = text.find("]", i)
            i = len(text) if close < 0 else close + 1
        else:
            i += 1
    return i


# Commands that wrap a cell's content. The value is the number of brace arguments before
# the content. The content stays, and the command and its other arguments go.
CELL_WRAPPERS = {"multicolumn": 2, "multirow": 2, "makecell": 0, "shortstack": 0,
                 "textcolor": 1, "colorbox": 1}
CELL_FORMAT = re.compile(r"\\(?:rowcolor|cellcolor|columncolor|color)(?:\[[^\]]*\])?\{[^{}]*\}")
# Layout commands whose arguments are sizes, not content: \scalebox{0.78}{0.368} is 0.368.
# The size arguments go; the content (the next brace group) stays.
LAYOUT = re.compile(r"\\(?:scalebox\{[^{}]*\}|resizebox\*?\{[^{}]*\}\{[^{}]*\}"
                    r"|rotatebox(?:\[[^\]]*\])?\{[^{}]*\}|raisebox\{[^{}]*\}(?:\[[^\]]*\])*"
                    r"|(?:hspace|vspace|phantom|hphantom|vphantom)\*?\{[^{}]*\})")
TABULAR_ENVS = _names(TABULARS, star=False)
TABULAR_START = re.compile(r"\\begin\{" + TABULAR_ENVS + r"\}")
# The begin and end of a tabular inside a cell, with its options and column spec.
TABULAR_EDGE = re.compile(
    r"\\(?:begin|end)\{" + TABULAR_ENVS + r"\}(?:\[[^\]]*\])?(?:\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})?")
# Marks that carry a claim in a table cell: the best result in bold, the second underlined,
# and ticks and crosses.
# The emphasis of a cell: commands that take the text as an argument, and switches.
EMPHASIS_COMMANDS = {"textbf": "bold", "mathbf": "bold", "boldsymbol": "bold", "bm": "bold",
                     "textit": "italic", "textsl": "italic", "emph": "italic", "mathit": "italic",
                     "underline": "underline", "uline": "underline"}
EMPHASIS_SWITCHES = {"bfseries": "bold", "bf": "bold", "itshape": "italic", "it": "italic",
                     "em": "italic", "slshape": "italic", "sl": "italic"}


def _emphasis(style: str | None = None, commands: bool = True, switches: bool = True) -> str:
    """A regex alternation of the emphasis commands and switches of one style, or of all."""
    names = [n for table, on in ((EMPHASIS_COMMANDS, commands), (EMPHASIS_SWITCHES, switches))
             if on for n, s in table.items() if style in (None, s)]
    return "(?:" + "|".join(names) + ")"


BOLD_CELL = re.compile(r"\\" + _emphasis("bold") + r"(?![A-Za-z])")
UNDERLINE_CELL = re.compile(r"\\" + _emphasis("underline") + r"(?![A-Za-z])")
TICKS = tuple((re.compile(pat), sym) for pat, sym in (
    (r"\\ding\{51\}", " ✓ "), (r"\\ding\{52\}", " ✓ "), (r"\\ding\{55\}", " ✗ "),
    (r"\\ding\{56\}", " ✗ "), (r"\\(?:checkmark|cmark)(?![A-Za-z])", " ✓ "),
    (r"\\xmark(?![A-Za-z])", " ✗ ")))
CELL_WRAPPER = re.compile(r"\\(" + "|".join(CELL_WRAPPERS) + r")\b")


def _ticks(text: str) -> str:
    """Ticks and crosses as ✓ and ✗."""
    for pat, sym in TICKS:
        text = pat.sub(sym, text)
    return text


def _unwrap(text: str) -> str:
    r"""A table cell or row without its cell wrappers (inner ones first) and colour commands.

    For example, \multicolumn{1}{l}{\multirow{2}{*}{Algorithm}} becomes Algorithm.
    """
    text = CELL_FORMAT.sub(" ", LAYOUT.sub(" ", _ticks(text)))
    while True:
        found = list(CELL_WRAPPER.finditer(text))
        if not found:
            return text
        m = found[-1]
        i = _skip_options(text, m.end())
        try:
            for _ in range(CELL_WRAPPERS[m.group(1)]):
                _, i = _braced(text, _skip_options(text, i))
            content, end = _braced(text, _skip_options(text, i))
        except ValueError:
            text = text[:m.start()] + text[m.end():]
            continue
        text = text[:m.start()] + content + text[end:]


def _tabulars(text: str) -> list[tuple[str, str]]:
    r"""Every outermost tabular, tabularx or longtable in the text, as (column spec, body).

    A tabular inside another one (a cell with two lines, say) stays in the body of the
    outer one. Each tabular's \end is found by counting the nesting.
    """
    out, pos = [], 0
    while True:
        m = TABULAR_START.search(text, pos)
        if not m:
            return out
        i = m.end()
        try:
            if re.match(r"\\begin\{(?:tabular\*|tabularx)\}", text[m.start():m.end()]):
                _, i = _braced(text, _skip_options(text, i))   # the width argument
            spec, i = _braced(text, _skip_options(text, i))
        except ValueError:
            pos = m.end()
            continue
        depth, k = 1, i
        for t in re.finditer(r"\\(begin|end)\{" + TABULAR_ENVS + r"\}", text[i:]):
            depth += 1 if t.group(1) == "begin" else -1
            if depth == 0:
                k = i + t.start()
                break
        else:
            k = len(text)
        out.append((spec, text[i:k]))
        pos = k + 1


def _inner_tabulars_flat(body: str) -> str:
    """The body with each tabular inside a cell turned into its text on one line."""
    for _spec, inner in _tabulars(body):
        flat = " ".join(x.strip() for x in inner.split("\\\\"))
        body = body.replace(inner, flat, 1)
    return TABULAR_EDGE.sub(" ", body)


def _spec_columns(spec: str) -> int:
    r"""The number of columns a column spec declares; *{3}{c} counts as three."""
    spec = re.sub(r"\[[^\]]*\]", "", spec)                 # S[table-format=1.3] is one column
    spec = re.sub(r"\*\s*\{(\d+)\}\s*\{([^{}]*)\}", lambda m: m.group(2) * int(m.group(1)), spec)
    return len([c for c in re.sub(r"\{[^{}]*\}", "", spec) if c in "lcrpXmbSL"])


def _split_top(text: str, sep: str) -> list[str]:
    r"""`text` split at each `sep` outside braces.

    An escaped character (\&, \{) is neither a separator nor a brace. With `sep` the
    line break \\, a \\* is a line break too.
    """
    out, depth, start, i = [], 0, 0, 0
    while i < len(text):
        if depth <= 0 and text.startswith(sep, i):
            out.append(text[start:i])
            i += len(sep)
            if sep == "\\\\" and text.startswith("*", i):    # \\* keeps the page
                i += 1
            start = i
            continue
        if text[i] == "\\":
            i += 2
            continue
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        i += 1
    out.append(text[start:])
    return out


def _split_cells(raw: str) -> list[str]:
    r"""A row split at each & outside braces; an escaped \& stays in its cell."""
    return _split_top(raw, "&")


def _span(cell: str) -> tuple[str, int, int]:
    r"""(content, columns spanned, rows spanned) of one cell.

    \multicolumn{n}{..}{x} spans n columns. \multirow{k}{..}{x} spans k rows; a negative k
    puts the content on the last row of the block, as in \multirow{-8}{*}{name}.
    """
    cols, rows = 1, 1
    while True:
        s = CELL_FORMAT.sub(" ", cell).strip()
        m = re.match(r"\\(multicolumn|multirow)\b", s)
        if not m:
            return cell, cols, rows
        try:
            n, i = _braced(s, _skip_options(s, m.end()))
            _, i = _braced(s, _skip_options(s, i))
            content, _ = _braced(s, _skip_options(s, i))
            k = int(n.strip())
        except ValueError:
            return cell, cols, rows
        if m.group(1) == "multicolumn":
            cols = max(1, k)
        else:
            rows = k
        cell = content


def _cell_text(cell: str) -> str:
    """One raw cell of a table record as plain text, with bold and underline kept as marks."""
    bold, under = bool(BOLD_CELL.search(cell)), bool(UNDERLINE_CELL.search(cell))
    text = _plain(_unwrap(cell)).strip()
    if text and bold:
        text += " [bold]"
    if text and under:
        text += " [underlined]"
    return text


# A full rule ends the header or a group of rows; \cline, \cmidrule and \cdashline only
# underline part of a row.
HEADER_RULE = re.compile(r"\\(?:" + "|".join(FULL_RULES) + ")")
PARTIAL_RULE = re.compile(r"\\(?:" + "|".join(PARTIAL_RULES) + ")")


# Size options on rules and line breaks (\toprule[1.2pt], \\[2pt], \\*[0.5\baselineskip]) are
# layout. Left in, they glue onto the next cell.
SIZE_OPTION = re.compile(r"(\\\\\*?)\s*\[[^\]]*\]")
RULE_OPTION = re.compile(r"(\\(?:" + "|".join(SIZED_RULES) + r")\s*)\[[^\]]*\]")
PLACEHOLDER = re.compile(r"^(?:[✓✗–—-]+|n/?a|N/?A)?$")
# Emphasis on a cell (bold, italic, underline), as commands with an argument or as switches.
EMPHASIS = re.compile(r"\\" + _emphasis(switches=False) + r"\s*\{((?:[^{}]|\{[^{}]*\})*)\}")
EMPHASIS_SWITCH = re.compile(r"\\" + _emphasis(commands=False) + r"(?![A-Za-z])\s*")


def _cell_plain(cell: str) -> str:
    """The plain text of one raw cell, its span wrappers removed."""
    return _plain(_unwrap(_span(cell)[0])).strip()


def _is_value(text: str) -> bool:
    """Whether a cell's plain text is a number (24.1, 1e-4, 85%), not a name such as Qwen2.5."""
    text = re.sub(r"(?<=\d)[eE][+-]?(?=\d)", "", text)
    return bool(re.search(r"\d", text)) and not re.search(r"[A-Za-z]", text)


def _first_data_row(grid: list[list[str]], header_end: int) -> int:
    r"""Where the header ends when rows of numbers sit above the first full rule.

    The header runs to the first full rule, but some tables put that rule under a few data
    rows ("Model | Acc", "BERT | 81.2", \hline, "Ours | 85.0"). A row above the rule is a
    data row, and ends the header, when a cell after its first holds a number. It stays a
    header row when a \multicolumn cell of the row above, past the first column, sits over
    one of its numbers, since then it names the columns of a header group ("Shots" over
    "1 | 5 | 10"). A \multicolumn over the first columns ("Metric") names the label
    columns, not a group, so it does not count.
    """
    for k in range(1, header_end):
        values = {col for col, _, text in _placed(grid[k]) if col > 0 and _is_value(text)}
        group = {c for col, span, _ in _placed(grid[k - 1]) if span > 1 and col > 0
                 for c in range(col, col + span)}
        if values and not values & group:
            return k
    return header_end


def _placed(cells: list[str]) -> list[tuple[int, int, str]]:
    """Each cell of a raw row as (first column, columns spanned, plain text)."""
    out, col = [], 0
    for cell in cells:
        span = _span(cell)[1]
        out.append((col, span, _cell_plain(cell)))
        col += span
    return out


def _second_header_end(grid: list[list[str]], full_rules: list[bool], partial_rules: list[bool],
                       rules: list[bool], header_end: int) -> int:
    r"""Where the header ends, with a second header row taken in.

    Some tables put each header level between full rules: "Agent | Metrics", \midrule,
    "Model | SR | CR", \midrule, data. The second row joins the header when it sits between
    two real full rules right under a one-row header (a section row in between is not a
    rule), has at least two filled cells, and every filled cell is a word: no digit, no
    tick or cross, no placeholder. Later rows must hold number cells, so a text-only table
    keeps its rows. A rule under a row of words is not enough on its own, since many tables
    draw a rule after every row and start with a row of words (Optimizer | AdamW). The
    table must also show a header level: the first row spans columns with \multicolumn, a
    partial rule (\cmidrule, \cline, \cdashline) sits under it, or the rows below are not
    each set off by a rule.
    """
    if header_end != 1 or len(grid) < 3 or not (full_rules[1] and full_rules[2]):
        return header_end
    texts = [_cell_plain(c) for c in grid[1]]
    filled = [x for x in texts if x]
    if len(filled) < 2 or any(re.search(r"\d", x) or PLACEHOLDER.match(x) for x in filled):
        return header_end
    if not any(_is_value(_cell_plain(c)) for cells in grid[2:] for c in cells):
        return header_end
    spans = any(_span(c)[1] > 1 for c in grid[0])
    unruled = any(not rules[k] for k in range(3, len(grid)))
    return 2 if spans or partial_rules[1] or unruled else header_end


def _without_emphasis(cell: str) -> str:
    """A cell with its bold, italic and underline removed, so a carried label stays plain."""
    while True:
        plain = EMPHASIS_SWITCH.sub("", EMPHASIS.sub(r"\1", cell))
        if plain == cell:
            return cell
        cell = plain


def _carry_labels(table: list[list[str]], grid: list[list[str]], rules: list[bool], header_end: int
                  ) -> set[int]:
    r"""Fill a blank first cell with the label written once above it, as a reader does.

    Authors often write a group's label on its first row only and leave the rows below
    blank, with no \multirow. The label is carried without its bold, italic or underline,
    which belong to the row that wrote it. A blank is filled only in that clean pattern: a
    row with a label, then rows up to the next full rule (or section row) that all have a
    blank first cell, the same number of cells as the label row, a number in them, and no
    \multicolumn in front. Anything else stays blank, since a wrong label is worse than
    none. Returns the indices of the filled rows.
    """
    filled = set()
    r = header_end
    while r < len(table):
        end = r + 1
        while end < len(table) and not rules[end]:
            end += 1
        first, rest = r, range(r + 1, end)
        label = table[first][0] if table[first] else ""
        n = len(grid[first])
        if label and rest and not re.match(r"\s*\\multicolumn", grid[first][0]) and all(
                len(grid[k]) == n and table[k] and not table[k][0]
                and not re.match(r"\s*\\multicolumn", grid[k][0])
                and any(re.search(r"\d", _cell_plain(c)) for c in grid[k]) for k in rest):
            for k in rest:
                table[k][0] = _without_emphasis(label)
                filled.add(k)
        r = end
    return filled


def _table_rows(spec: str, body: str) -> tuple[list[list[str]], list[str], int, set[int]]:
    r"""One tabular's rows, section headings, column count, and rows given a carried label.

    Cells stay LaTeX, with their wrappers removed, as the checks read them. The first row
    returned is the header: for each column, the path of all header rows
    above the first \midrule (or \hline), joined with " / ", with \multicolumn headers spread
    over the columns they cover. Data cells under a \multirow get its text, whether the
    \multirow sits on the first row of its block or (with a negative count) on the last. A
    \multicolumn data cell sits in its first column and leaves the others empty. A row with
    no `&` is a section heading. It goes in `sections`, not in rows. Size options on rules
    and line breaks are dropped first. Two readings of layout follow, on the grid:
    `_first_data_row` (a row of numbers above the first rule is data, not header),
    `_second_header_end` (a second header row between two full rules joins the header) and
    `_carry_labels` (a label written once above a group of rows reaches the group).
    """
    ncol = _spec_columns(spec)
    body = SIZE_OPTION.sub(r"\\\\", RULE_OPTION.sub(r"\1", body))   # \\*[2pt] is a line break
    raws = _rows(_inner_tabulars_flat(body))
    grid, sections, section, header_end = [], [], "", None
    rules, pending = [], False         # rules[i]: a full rule (or a section row) before grid row i
    full_rules, partial_rules = [], []  # a real full rule / a partial rule before grid row i
    full, partial, section_row = False, False, False
    for raw in raws:
        rule = bool(HEADER_RULE.search(raw.split("&")[0]))
        pending = pending or rule
        full = full or rule
        partial = partial or bool(PARTIAL_RULE.search(raw.split("&")[0]))
        raw = LATEX_RULES.sub(" ", raw).replace("\\\\", " ").strip()
        if not raw:
            if rule and grid and header_end is None:
                header_end = len(grid)
            continue
        if rule and grid and header_end is None:
            header_end = len(grid)
        cells = _split_cells(raw)
        if len(cells) == 1 and not re.match(r"\s*\\multirow", cells[0]):
            section = _plain(_unwrap(raw))
            pending = section_row = True
            if not grid:
                header_end = None             # a title row above the header is not the header
            continue
        grid.append(cells)
        sections.append(section)
        rules.append(pending)
        full_rules.append(full and not section_row)
        partial_rules.append(partial)
        pending = full = partial = section_row = False
    if not grid:
        return [], [], ncol, set()
    if header_end is None or header_end == 0 or header_end >= len(grid):
        header_end = 1
    header_end = _first_data_row(grid, header_end)
    header_end = _second_header_end(grid, full_rules, partial_rules, rules, header_end)
    # place cells, spreading \multicolumn and recording \multirow blocks
    width = max(ncol, max(sum(_span(c)[1] for c in cells) for cells in grid[:header_end]))
    table, vspans = [], []
    for r, cells in enumerate(grid):
        row, col = [], 0
        for cell in cells:
            content, cs, rs = _span(cell)
            text = _unwrap(content).strip()
            if r < header_end:
                row += [text] * cs
            else:
                row += [text] + [""] * (cs - 1)
            if rs != 1 and text:
                vspans.append((r, col, rs, text))
            col += cs
        table.append(row)
    for r, col, rs, text in vspans:
        span = range(r + 1, r + rs) if rs > 0 else range(r + rs + 1, r)
        for rr in span:
            if 0 <= rr < len(table) and col < len(table[rr]) and not table[rr][col]:
                table[rr][col] = text
    carried = _carry_labels(table, grid, rules, header_end)
    header_rows = table[:header_end]
    header = []
    for j in range(width):
        parts = []
        for h in header_rows:
            v = h[j] if j < len(h) else ""
            if v and (not parts or parts[-1] != v):
                parts.append(v)
        header.append(" / ".join(parts))
    rows = [header] + table[header_end:]
    secs = [""] + sections[header_end:]
    return rows, secs, ncol, {k - header_end + 1 for k in carried}


def _rows(body: str) -> list[str]:
    r"""A tabular body split at each \\ or \\* outside braces; a cell's own line break stays."""
    return _split_top(body, "\\\\")


def _latex_tables(full: str) -> list[PaperTable]:
    r"""One record per tabular in each table float (named by its \label), then per bare tabular.

    A float with two tabulars side by side gives two records with the float's name and
    caption. A bare tabular is one outside any table float, as in a file that holds only
    the tabular and that a float elsewhere puts in with \input, or a longtable. It has no
    name and no caption, unless it is a longtable with its own caption.
    """
    out = []
    for m in LATEX_TABLE.finditer(full):
        body = m.group(2)
        tabs = _tabulars(body) or [("", "")]
        for spec, inner in tabs:
            rows, sections, ncol, carried = (_table_rows(spec, inner) if inner
                                             else ([], [], None, set()))
            out.append(_table(_latex_name(body), _latex_caption(body), rows, ncol, sections,
                              carried))
    rest = LATEX_TABLE.sub(" ", full)
    for spec, body in _tabulars(rest):
        rows, sections, ncol, carried = _table_rows(spec, body)
        out.append(_table(_latex_name(body), _latex_caption(body), rows, ncol, sections,
                          carried))
    return out


def _latex_captions(full: str) -> list[PaperCaption]:
    r"""Every figure caption, then every table caption, as plain text.

    A float with two \caption s (two minipages side by side) gives two captions, each named
    by the \label that follows it.
    """
    out = []
    for kind, pattern in (("figure", LATEX_FIGURE), ("table", LATEX_TABLE)):
        for m in pattern.finditer(full):
            body = m.group(m.lastindex)
            starts = [c.start() for c in re.finditer(r"\\caption(?![A-Za-z])", body)]
            for i, s in enumerate(starts):
                part = body[s:starts[i + 1] if i + 1 < len(starts) else len(body)]
                text = _plain(_latex_caption(part))
                if text:
                    out.append(_caption(kind, _latex_name(part) or (_latex_name(body)
                                                                     if len(starts) == 1 else None),
                                        text))
    return out


# ------------------------------------------------------------------ Markdown
#
# Markdown has no \label or \ref. Its references are links to anchors: `[see](#results)`
# points at a heading whose anchor is "results", or at an explicit id such as
# `<a id="results">` or a heading ending in `{#results}`. Its tables carry no label,
# so nothing can refer to one. A caption is a one-line paragraph starting "Table"
# directly above or below the table, as Markdown writers usually do it.

MD_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")
MD_HEADING = re.compile(r" {0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
MD_HEADING_ID = re.compile(r"\s*\{#([^\s}]+)[^}]*\}\s*$")
MD_LIST_ITEM = re.compile(r"\s*(?:[-*+]|\d+[.)])\s+")
MD_TABLE_DELIMITER = re.compile(r"\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$")
MD_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
MD_HTML_ID = re.compile(r"<[A-Za-z][^>]*?\b(?:id|name)\s*=\s*[\"']([^\"']+)[\"']")
MD_LINK_TO_ANCHOR = re.compile(r"\]\(\s*#([^)\s]+)|href\s*=\s*[\"']#([^\"']+)"
                               r"|^\s*\[[^\]]+\]:\s*#(\S+)", re.M)
MD_CAPTION = re.compile(r"[*_]*Table\b", re.I)
ABSTRACT_HEADINGS = re.compile(r"(abstract|summary|tl;?\s*dr)\s*:?", re.I)


def _md_cells(row: str) -> list[str]:
    """The cells of one pipe-table row, without the outer pipes."""
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|") and not row.endswith("\\|"):
        row = row[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def _md_anchor(heading: str) -> str:
    """The anchor a heading gets on GitHub: lower case, punctuation dropped, spaces as -."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)        # a link keeps its text
    text = re.sub(r"[^\w\- ]", "", text.strip().lower())
    return text.replace(" ", "-")


def read_markdown(path: str) -> Paper:
    """Read a Markdown file: its prose blocks, pipe tables, headings and anchors."""
    with open(path, errors="ignore") as fh:
        raw = fh.read()
    # Comments and code are not claims; blank them line for line so line numbers hold.
    raw = MD_HTML_COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), raw)
    lines = raw.split("\n")
    if lines and lines[0].strip() == "---":            # front matter
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), 0)
        lines[:end + 1] = [""] * (end + 1)
    blocks = []          # (kind, first line, last line + 1, payload)
    para, i = [], 0

    def flush() -> None:
        """End the paragraph being collected."""
        if para:
            text = " ".join(re.sub(r"^\s*>\s?", "", lines[j]).strip() for j in para)
            blocks.append(("prose", para[0], para[-1] + 1, text))
            para.clear()

    while i < len(lines):
        line = lines[i]
        fence = MD_FENCE.match(line)
        if fence:
            flush()
            mark, j = fence.group(1), i + 1
            while j < len(lines) and not lines[j].lstrip().startswith(mark):
                j += 1
            for k in range(i, min(j + 1, len(lines))):
                lines[k] = ""
            i = j + 1
            continue
        heading = MD_HEADING.match(line)
        if heading:
            flush()
            blocks.append(("heading", i, i + 1, (len(heading.group(1)), heading.group(2))))
        elif (i + 1 < len(lines) and "|" in line and MD_TABLE_DELIMITER.match(lines[i + 1])
              and len(_md_cells(line)) == len(_md_cells(lines[i + 1]))):
            flush()
            j = i + 2
            while j < len(lines) and lines[j].strip() and "|" in lines[j]:
                j += 1
            rows = [_md_cells(line)] + [_md_cells(r) for r in lines[i + 2:j]]
            blocks.append(("table", i, j, (rows, len(_md_cells(lines[i + 1])))))
            i = j
            continue
        elif not line.strip():
            flush()
        else:
            if MD_LIST_ITEM.match(line) or line.lstrip().startswith(">"):
                flush()
            para.append(i)
        i += 1
    flush()

    name = os.path.basename(path)
    tables, table_captions, caption_blocks = [], [], set()
    for n, (kind, first, last, payload) in enumerate(blocks):
        if kind != "table":
            continue
        caption = ""
        for k in (n - 1, n + 1):          # a one-line "Table ..." paragraph touching it
            if 0 <= k < len(blocks) and blocks[k][0] == "prose" and k not in caption_blocks:
                _kind, cfirst, clast, ctext = blocks[k]
                if clast - cfirst == 1 and (clast == first or cfirst == last) \
                        and MD_CAPTION.match(ctext):
                    caption = ctext
                    caption_blocks.add(k)
                    break
        rows, ncol = payload
        table_name = f"{name} table {len(tables) + 1}"
        tables.append(_table(table_name, caption, rows, ncol, [""] * len(rows)))
        if caption:
            table_captions.append(_caption("table", table_name, caption))

    labels, anchors, seen = [], set(), {}
    for kind, _first, _last, payload in blocks:
        if kind != "heading":
            continue
        explicit = MD_HEADING_ID.search(payload[1])
        if explicit:
            labels.append(explicit.group(1))
        base = _md_anchor(MD_HEADING_ID.sub("", payload[1]))
        anchors.add(base if base not in seen else f"{base}-{seen[base]}")
        seen[base] = seen.get(base, 0) + 1
    text = "\n".join(lines)
    labels += MD_HTML_ID.findall(text)
    refs = {next(g for g in m.groups() if g) for m in MD_LINK_TO_ANCHOR.finditer(text)}

    def heading_text(payload: tuple[int, str]) -> str:
        """A heading as prose, without its explicit anchor."""
        return MD_HEADING_ID.sub("", payload[1])

    prose = tuple(payload if kind == "prose" else heading_text(payload)
                  for n, (kind, _f, _l, payload) in enumerate(blocks)
                  if kind != "table" and n not in caption_blocks)
    abstract, after = "", text
    for n, (kind, _first, _last, payload) in enumerate(blocks):
        if kind == "heading" and ABSTRACT_HEADINGS.fullmatch(heading_text(payload).strip()):
            level = payload[0]
            end = next((b for b in blocks[n + 1:]
                        if b[0] == "heading" and b[3][0] <= level), None)
            stop = end[1] if end else len(lines)
            abstract = " ".join(_denoise(b[3]).strip() for b in blocks[n + 1:]
                                if b[0] == "prose" and b[1] < stop)
            abstract = " ".join(abstract.split())
            after = "\n".join(lines[stop:])
            break
    sents = sentences(prose)
    return Paper(
        formats=frozenset({PaperFormat.MARKDOWN}), text=text, prose=prose,
        sentences=tuple(sents), statements=tuple(statements(sents)), tables=tuple(tables),
        captions=tuple(table_captions), labels=tuple(labels),
        targets=frozenset(labels) | anchors, refs=frozenset(refs),
        abstract=abstract, after_abstract=after, unreadable=(),
        sentence_sections=("",) * len(sents))     # one per sentence, so `load` lines them up


READERS = {PaperFormat.LATEX: read_latex, PaperFormat.MARKDOWN: read_markdown}


# ---------------------------------------------------------------- candidates
#
# A candidate is one place in a note that may make a claim: a whole table, a caption, an
# equation or a body sentence. The auditing agent answers every one, as a claim or as a drop
# with a reason, so a claim made in words or resting on a small number is never skipped
# unseen. An id comes from the text, not from its place: `s-4f1a09c2` for a sentence,
# `tab-...` for a whole table and `cap-...` for a caption. The hex is the start of the
# SHA-256 of the text after `_norm`. A repeat of the same text in one file gets `-2`, `-3`,
# and so on. So a sentence keeps its id when the worker adds or moves text around it.

type Candidate = dict[str, str | list[str] | None]

ID_HEX = 8                  # hex characters of the SHA-256 in a candidate id
# The kinds of candidate. A caption's kind names its float: "figure caption", "table caption".
SENTENCE = "sentence"
TABLE = "table"
EQUATION = "equation"
COMMIT = "commit message"          # a sentence of a commit message (utils/structure.py)
CAPTION_END = " caption"


def caption_kind(float_kind: str) -> str:
    """The candidate kind of a caption of this float kind ("figure" or "table")."""
    return float_kind + CAPTION_END


def is_caption(kind: object) -> bool:
    """True for the kind of a caption candidate."""
    return str(kind).endswith(CAPTION_END)


def cell_span(header: str, value: str) -> str:
    """One cell as a row of a whole-table candidate writes it: "header: value"."""
    return f"{header}: {value}"


def free_id(base: str, taken: Collection[str]) -> str:
    """`base`, or the first of base-2, base-3, ... that is not in `taken`."""
    out, n = base, 1
    while out in taken:
        n += 1
        out = f"{base}-{n}"
    return out


def text_id(kind: str, text: str, seen: dict[str, int]) -> str:
    """The id of a candidate of this kind (`s`, `tab`, `cap` or `eq`) from its text.

    `seen` counts the ids given so far in the same file. A repeat gets `-2`, `-3`, ...
    """
    base = f"{kind}-{hashlib.sha256(_norm(text).encode()).hexdigest()[:ID_HEX]}"
    seen[base] = seen.get(base, 0) + 1
    return base if seen[base] == 1 else f"{base}-{seen[base]}"


def candidates(paper: Paper) -> list[Candidate]:
    """Every whole table, caption, equation and body sentence of a paper, with ids and numbers.

    A table is one candidate: its name, caption and columns, then its rows, each row naming
    each cell by its column. Its rows are not candidates of their own, and its text is never
    cut (utils/structure.py warns on one longer than LONG_TABLE_CHARACTERS). A sentence carries
    its section heading and the caption of each table or figure it names ("referenced"), as
    fields outside its text, so its id does not change when they do. Every candidate lists
    its numbers as written ("values"). A sentence or caption whose text was cut at MAX_PIECE
    has "cut": true, and lists only the numbers of the text kept.
    """
    out = []
    seen: dict[str, int] = {}
    printed = {a.label: f"{KIND_NAME.get(a.kind, '')} {a.number}".strip()
               for a in paper.addresses}
    for ti, t in enumerate(paper.tables, start=1):
        if not t.rows:
            continue
        header = [_plain(c) for c in t.rows[0]]
        name = printed.get(t.name or "", t.name or f"table {ti}")
        caption = _plain(t.caption)
        lines = []
        for row in t.rows[1:]:
            cells = [_cell_text(c) for c in row]
            lines.append("; ".join(cell_span(h, c) for h, c in
                                   itertools.zip_longest(header, cells, fillvalue="")))
        head = f"{name}: " + (f"{caption} " if caption else "") + \
            f"Columns: {' | '.join(h for h in header if h)}.{TABLE_ROWS_MARK}"
        whole = head + " || ".join(lines)
        out.append({"id": text_id("tab", whole, seen), "kind": TABLE, "table": name,
                    "text": whole, "values": numbers(" ".join(lines))})
    kinds = {}
    for c in paper.captions:
        kinds[c.kind] = kinds.get(c.kind, 0) + 1
        address = printed.get(c.float_name or "")
        text = (f"{address}: " if address else "") + c.text[:MAX_PIECE]
        out.append({"id": text_id("cap", text, seen), "kind": caption_kind(c.kind),
                    "float": address or c.float_name or f"{c.kind} {kinds[c.kind]}",
                    "text": text, "values": claim_numbers(c.text[:MAX_PIECE]),
                    **({"cut": True} if len(c.text) > MAX_PIECE else {})})
    for e in paper.equations:
        text = (f"Eq. ({e.number}): " if e.number else "") + e.text
        out.append({"id": text_id("eq", text, seen), "kind": EQUATION,
                    "label": e.label, "address": e.address,
                    "text": text, "values": claim_numbers(e.text)})
    found = sentence_candidates(paper.sentences, seen)
    captions = {c["text"].split(":", 1)[0]: c["text"] for c in out
                if is_caption(c["kind"]) and ":" in c["text"]}
    for n, c in enumerate(found):
        c["section"] = paper.sentence_sections[n] if n < len(paper.sentence_sections) else ""
        c["referenced"] = [captions[k] for k in dict.fromkeys(
            re.findall(r"\b(?:Table|Figure) [A-Z]?\d+[a-z]?", c["text"])) if k in captions]
    out += found
    return out


# Numbers that a reference or a citation put into the text: "Table 3", "Eq. (5)",
# "Section 4.2", "(Zhang et al., 2020)". They locate a claim; they are not one.
NOT_CLAIM_NUMBERS = re.compile(
    r"\b(?:Tables?|Figures?|Figs?\.|Sections?|Secs?\.|Eqs?\.|Equations?|Appendix|Algorithm|Alg\."
    r"|Theorem|Lemma)\s*\(?[A-Z]?\d+(?:\.\d+)*[a-z]?\)?"
    r"|\((?:[^()]*?,\s*(?:19|20)\d{2}[a-z]?;?\s*)+\)|\((?:19|20)\d{2}[a-z]?\)")
# A sentence or caption candidate's text is cut at this length, with no marker in the text.
# A cut candidate has the context field "cut": true (not part of its id or text), and its
# "values" are only the numbers of the text kept. A whole table is never cut.
MAX_PIECE = 4000            # longer is a failed split; cut so the candidate stays readable
# A whole-table candidate longer than this is kept whole, with a warning
# (utils/structure.py): every model that labels or links it reads all of it.
LONG_TABLE_CHARACTERS = 5000
TABLE_ROWS_MARK = " Rows: "     # where a whole-table candidate's rows start (table_header)


def claim_numbers(text: str) -> list[str]:
    """The numbers of a candidate's text, without those that a reference or citation put there.

    A number with a unit (CLAIM_UNIT) directly after it is read without the unit.
    """
    return [n.replace("−", "-") for n in CLAIM_NUMBER.findall(NOT_CLAIM_NUMBERS.sub(" ", text))]


def value_marked_text(text: str, value: str | None, sister_values: Collection[str] = (),
                      anonymized: bool = False) -> str:
    """`text` with its own value marked, for a reader of one quote or claim among its sisters.

    A quote with N values is N quotes with the same text, each with one value; the other
    values are its sister values. The own value is written as {{value}} as printed, and
    as {{VALUE}} when `anonymized`. A sister value stays as printed, and is *redacted*
    when `anonymized`. Values are compared as `claim_numbers` gives them (a unicode minus
    is "-"), and a number of a reference or a citation is never marked.

    Args:
        text: the words of the quote or the statement of the claim.
        value: the own value, or None for an entry with no value.
        sister_values: the values of the sister entries.
        anonymized: hide every value, as for the method writer.
    """
    skip = [m.span() for m in NOT_CLAIM_NUMBERS.finditer(text)]
    sisters = set(sister_values) - {value}

    def mark(m: re.Match[str]) -> str:
        printed = m.group(1)
        if any(start <= m.start() < end for start, end in skip):
            return printed
        number = printed.replace("−", "-")
        if number == value:
            return OWN_VALUE_HIDDEN if anonymized else "{{" + printed + "}}"
        if number in sisters and anonymized:
            return SISTER_VALUE_HIDDEN
        return printed
    return CLAIM_NUMBER.sub(mark, text)


def sentence_candidates(sents: Iterable[str], seen: dict[str, int] | None = None
                        ) -> list[Candidate]:
    """One candidate per sentence, each with the sentence before it as context.

    The context lets a reader understand a sentence such as "Results show it improves accuracy"
    without a rewrite of the sentence. `seen` counts the ids already given in the same file
    (see `text_id`); None when these sentences are the whole file.
    """
    sents = list(sents)
    seen = {} if seen is None else seen
    return [{"id": text_id("s", s[:MAX_PIECE], seen), "kind": SENTENCE,
             "text": s[:MAX_PIECE], "values": claim_numbers(s[:MAX_PIECE]),
             "previous": sents[n - 2][:300] if n > 1 else "",
             **({"cut": True} if len(s) > MAX_PIECE else {})}
            for n, s in enumerate(sents, start=1)]


def _norm(text: object) -> str:
    """Text for comparing a quote with a note: lower case, one space, plain minus and quotes."""
    s = str(text).lower().replace("−", "-").replace("’", "'").replace("“", '"').replace("”", '"')
    return NORM_SIGN_SPACE.sub(r"\1", " ".join(NORM_DROP.sub(" ", s).split()))


def quote_in(quote: str, texts: Iterable[str]) -> bool:
    """True if the quote occurs word for word in the texts joined, after `_norm`.

    A quote that starts or ends with a digit must not start or end inside a longer number:
    "1.2% accuracy" does not occur in "91.2% accuracy", "accuracy of 9" does not occur in
    "accuracy of 91.2", and "accuracy of 1" does not occur in "accuracy of 1,300".
    """
    if not quote.strip():
        return False
    words, text = _norm(quote), _norm(" ".join(texts))
    start = text.find(words)
    while start != -1:
        end = start + len(words)
        if not (_number_goes_on_before(text, start, words)
                or _number_goes_on_after(text, end, words)):
            return True
        start = text.find(words, start + 1)
    return False


# After `_norm`, a thousands separator is a space: "1,300" is "1 300".
_THOUSANDS_BEFORE = re.compile(r"\d{1,3} $")
_THOUSANDS_AFTER = re.compile(r"^ \d{3}(?![\d.])")
_LAST_WHOLE_NUMBER = re.compile(r"(?<![\d.])\d{1,3}$")
_FIRST_THREE_DIGITS = re.compile(r"^\d{3}(?!\d)")


def _number_goes_on_before(text: str, start: int, words: str) -> bool:
    """True if `words` starts with a digit or a point, and a number in `text` goes on before it."""
    if not (words[0].isdigit() or words[0] == "."):
        return False
    before = text[:start]
    return bool(before) and (before[-1].isdigit() or before[-1] == "."
                             or (_FIRST_THREE_DIGITS.match(words) is not None
                                 and _THOUSANDS_BEFORE.search(before) is not None))


def _number_goes_on_after(text: str, end: int, words: str) -> bool:
    """True if `words` ends with a digit, and a number in `text` goes on after it."""
    if not words[-1].isdigit():
        return False
    after = text[end:]
    return bool(after) and (after[0].isdigit()
                            or (after[0] == "." and after[1:2].isdigit())
                            or (_LAST_WHOLE_NUMBER.search(words) is not None
                                and _THOUSANDS_AFTER.match(after) is not None))


def table_header(text: str) -> str:
    """The name, caption and columns of a whole-table candidate's text: its part before the rows."""
    return text.split(TABLE_ROWS_MARK, 1)[0]


def coverage(cands: Mapping[str, Candidate], quotes: Mapping[str, Mapping[str, object]],
             dropped: Mapping[str, object]) -> tuple[list[str], list[str]]:
    """(candidate ids no quote or drop answers, problems with the quotes and drops).

    `cands` maps a candidate id to its candidate, in file order. `quotes` maps a quote id to
    {"candidates": [ids], "quote": words copied exactly from them}. `dropped` maps a
    candidate id to its drop. A problem names the entry and what is wrong, in one sentence.
    """
    answered = set(dropped)
    problems = [f'Drop "{d}": no candidate has this id.' for d in dropped if d not in cands]
    for qid, q in quotes.items():
        names = q.get("candidates")
        if not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names):
            problems.append(f'Entry "{qid}": "candidates" must list one or more candidate ids.')
            continue
        answered.update(names)
        if unknown := [n for n in names if n not in cands]:
            problems.append(f'Entry "{qid}": no candidate has the id {unknown[0]}.')
            continue
        words = q.get("quote")
        if not isinstance(words, str) or not quote_in(words, (cands[n]["text"] for n in names)):
            problems.append(f'Entry "{qid}": the quote does not occur word for word in '
                            f"candidate {' '.join(names)}.")
        if both := sorted(set(names) & set(dropped)):
            problems.append(f'Entry "{qid}": candidate {both[0]} also has a drop. '
                            "Remove the drop or the quote.")
    return [c for c in cands if c not in answered], problems


# ---------------------------------------------------------------- data files

DATA_SUFFIXES = (".json", ".jsonl", ".csv")
SAMPLE_RECORDS = 2


def _records(data: object) -> list[object]:
    """The records of a loaded results file: its `records` or `rows` list, or the file itself."""
    if isinstance(data, dict):
        for key in ("records", "rows"):
            if isinstance(data.get(key), list):
                return data[key]
        return [data]
    return data if isinstance(data, list) else [data]


def _short(value: object, limit: int = 300) -> object:
    """A sample record cut down so the listing stays small: long lists and strings shortened."""
    if isinstance(value, dict):
        more = {"...": f"and {len(value) - 25} more keys"} if len(value) > 25 else {}
        return {k: _short(v, 80) for k, v in list(value.items())[:25]} | more
    if isinstance(value, list):
        return [_short(v, 40) for v in value[:5]] + (["..."] if len(value) > 5 else [])
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


def describe_data_file(path: str) -> dict[str, object]:
    """A data file's record count, field names and first records, or why it could not be read.

    The count is None for JSON Lines and CSV, which are not read to the end.
    """
    try:
        if path.endswith(".jsonl"):
            with open(path) as fh:
                lines = zip(range(SAMPLE_RECORDS + 1), fh, strict=False)
                recs = [json.loads(line) for _, line in lines if line.strip()]
            n = None
        elif path.endswith(".csv"):
            with open(path, newline="") as fh:
                recs = [r for _, r in zip(range(SAMPLE_RECORDS), csv.DictReader(fh), strict=False)]
            n = None
        else:
            with open(path) as fh:
                recs = _records(json.load(fh))
            n = len(recs)
    except (OSError, ValueError, csv.Error) as e:
        # csv.Error: for example a cell longer than the field size limit of the csv module
        return {"unreadable": first_part(f"{type(e).__name__}: {e}", 200)}
    fields = sorted({k for r in recs[:50] if isinstance(r, dict) for k in r})
    more = [f"... and {len(fields) - 60} more"] if len(fields) > 60 else []
    scanned = {"fields_from_first_records": 50} if len(recs) > 50 else {}
    return {"records": n, "fields": fields[:60] + more, **scanned,
            "sample": [_short(r) for r in recs[:SAMPLE_RECORDS]]}
