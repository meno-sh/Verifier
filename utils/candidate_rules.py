"""Code rules that drop candidates which are clearly not claims, before the labeller reads them.

`vcheck drop-non-claims` runs them on the candidates with no answer yet, and writes a drop
for each match with a reason "auto:Rn <name>", so drops can be counted per rule. The rules
R1 to R14 were built from PR #56's `not_a_claim`. Each drops only kinds of text that the
labeller also drops (core/claim_steps/labeller.py, KEEP_RULE).
"""
from __future__ import annotations

import re
from collections.abc import Collection, Mapping

from utils import extract

type Candidates = Mapping[str, Mapping[str, object]]

# PR #56's word list for "has a verb or a pronoun": a piece with none of them may be a
# heading or a name.
VERB = re.compile(r"\b(is|are|was|were|be|been|has|have|had|can|could|will|would|may|might|"
                  r"should|must|does|do|did|we|our|it|they|this|these|that|which|shows?|"
                  r"showed|use[sd]?|achieves?|improves?|outperforms?|leads?|yields?|gives?|"
                  r"makes?|takes?|holds?|provides?|requires?|remains?)\b", re.I)
# A compute or data fact, which is setup even in the acknowledgements.
COMPUTE = re.compile(r"\b(gpus?|tpus?\w*|cpus?|compute|hours|chips|cluster|a100s?|h100s?|"
                     r"v100s?|nodes)\b", re.I)

# R1: a comment line that reached the text.
COMMENT = re.compile(r"^\s*%")
# R2: the title block: an e-mail address, an affiliation, or a note on the authors.
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
AFFILIATION = re.compile(r"\b(university|institute|department|laboratory|college|school of|"
                         r"inc\.|ltd\.?|corporation)\b", re.I)
AUTHOR_NOTE = re.compile(r"\b(equal contribution|corresponding author|work done (while|during))\b",
                         re.I)
# R3: an acknowledgement, funding or contribution section, by its heading.
ACK_HEADING = re.compile(r"(acknowledge?ments?( and disclosure of funding)?|funding|"
                         r"author contributions?|contributions of the authors)", re.I)
# R4: PR #56's acknowledgement phrases, with `\bthank` narrowed so "thanks to its design"
# stays.
ACK_PHRASE = re.compile(
    r"\b(we thank|would like to thank|we are grateful|are grateful to|thanks go to|"
    r"(this|the|our) (work|research|project|study) (was|is) (partially |partly |in part )?"
    r"(supported|funded)|funded by|supported (in part )?by (the |a |an )?(grant|nsf|nih|darpa|"
    r"erc|award)|views and conclusions contained|author contributions)", re.I)
# R5: sentences of the venues' templates and checklists. Written from the NeurIPS checklist
# and the ICLR and NeurIPS style files, not copied from them, so they are matched by word
# overlap, not exactly.
TEMPLATE_SENTENCES = (
    "The answer NA means that the paper does not include experiments.",
    "The answer NA means that the abstract and introduction do not include the claims made "
    "in the paper.",
    "The answer NA means that the paper has no limitation while the answer No means that the "
    "paper has limitations, but those are not discussed in the paper.",
    "The answer NA means that the paper does not include theoretical results.",
    "The answer NA means that the paper does not involve crowdsourcing nor research with "
    "human subjects.",
    "The answer NA means that the paper poses no such risks.",
    "The answer NA means that the paper does not use existing assets.",
    "The answer NA means that the paper does not release new assets.",
    "The answer NA means that there is no societal impact of the work performed.",
    "Do the main claims made in the abstract and introduction accurately reflect the paper's "
    "contributions and scope?",
    "Does the paper discuss the limitations of the work performed by the authors?",
    "For each theoretical result, does the paper provide the full set of assumptions and a "
    "complete (and correct) proof?",
    "Does the paper fully disclose all the information needed to reproduce the main "
    "experimental results of the paper to the extent that it affects the main claims and/or "
    "conclusions of the paper (regardless of whether the code and data are provided or not)?",
    "Does the paper provide open access to the data and code, with sufficient instructions to "
    "faithfully reproduce the main experimental results, as described in supplemental "
    "material?",
    "Does the paper specify all the training and test details (e.g., data splits, "
    "hyperparameters, how they were chosen, type of optimizer, etc.) necessary to understand "
    "the results?",
    "Does the paper report error bars suitably and correctly defined or other appropriate "
    "information about the statistical significance of the experiments?",
    "For each experiment, does the paper provide sufficient information on the computer "
    "resources (type of compute workers, memory, time of execution) needed to reproduce the "
    "experiments?",
    "Does the research conducted in the paper conform, in every respect, with the NeurIPS "
    "Code of Ethics?",
    "Does the paper discuss both potential positive societal impacts and negative societal "
    "impacts of the work performed?",
    "Does the paper describe safeguards that have been put in place for responsible release "
    "of data or models that have a high risk for misuse (e.g., pretrained language models, "
    "image generators, or scraped datasets)?",
    "Are the creators or original owners of assets (e.g., code, data, models), used in the "
    "paper, properly credited and are the license and terms of use explicitly mentioned and "
    "properly respected?",
    "Are new assets introduced in the paper well documented and is the documentation "
    "provided alongside the assets?",
    "For crowdsourcing experiments and research with human subjects, does the paper include "
    "the full text of instructions given to participants and screenshots, if applicable, as "
    "well as details about compensation (if any)?",
    "Does the paper describe potential risks incurred by study participants, whether such "
    "risks were disclosed to the subjects, and whether Institutional Review Board (IRB) "
    "approvals (or an equivalent approval/review based on the requirements of your country "
    "or institution) were obtained?",
    "Please do not modify the questions and only use the provided macros for your answers.",
    "The checklist answers are an integral part of your paper submission.",
    "Delete this instruction block, but keep the section heading NeurIPS paper checklist.",
    "Papers not including the checklist will be desk rejected.",
    "All headings should be lower case (except for first word and proper nouns), flush left, "
    "and bold.",
    "Use unnumbered first level headings for the acknowledgments.",
    "Do not include acknowledgments in the anonymized submission, only in the final paper.",
)
TEMPLATE_MIN_WORDS = 5          # shorter pieces share too many words with any sentence
TEMPLATE_SHARE = 0.9            # of the piece's words that are in one template sentence
TEMPLATE_COVER = 0.9            # of that template sentence's words that are in the piece
# R5 reads only the checklist's guideline bullets: a piece in a section whose path names
# the checklist, which is not an answer or its justification. "The paper does not include
# theoretical results." is a justification there, and a claim anywhere else.
CHECKLIST = re.compile(r"checklist", re.I)
ANSWER_TEXT = re.compile(r"^\s*(answer|justification)\b|\[\s*(yes|no|n/?a|todo)\s*\]|"
                         r"\\answer(yes|no|na|todo)", re.I)
# R6: phrases that only a style file's instructions to authors use. "camera-ready" and
# "paper should not" are left out: authors write them in claims ("we will release the
# code with the camera-ready version"). A piece in the first person is never caught.
LAYOUT_THING = r"(tables?|figures?|captions?|headings?|titles?|equations?|footnotes?|pages?|" \
               r"references?|citations?|abstracts?)"
STYLE = re.compile(rf"(\b{LAYOUT_THING}( and \w+)? (should|must) be (numbered|centered|"
                   r"flush|lower case|single-spaced|double-spaced)|\bpoint type\b|"
                   r"\brunning (head|title)\b|\bstyle file\b|\bflush (left|right)\b|"
                   r"\bpt\b (bold|type)|\bsubmissions? (must|should)\b)", re.I)
FIRST_PERSON = re.compile(r"\b(we|our|ours|us|i|my)\b", re.I)
# R7: a checklist answer stub with nothing after it.
STUB = re.compile(r"^\s*(((answer|justification|guidelines)\s*:?\s*)|"
                  r"(\[\s*(yes|no|n/?a|todo)\s*\]\s*[.:]?\s*))+$", re.I)
# R9 and R10: what is left of a piece without LaTeX commands, links and citations.
COMMAND = re.compile(r"\\[a-zA-Z]+\*?(\[[^\]]*\])?")
# R9 drops no piece with math, an "=" or a digit ("$\lambda = 0.5$." is setup), except
# inside a graphics command's options, such as [width=0.5\linewidth].
GRAPHICS_OPTIONS = re.compile(r"\[\s*(width|height|totalheight|scale|angle|trim|clip|"
                              r"keepaspectratio|page|viewport|origin)\b[^\]]*\]", re.I)
MATH = re.compile(r"[$=<>^_\d]|\\[(\[]|\\(frac|sum|prod|int|log|exp|sqrt|math\w+|cdot|times|"
                  r"leq?|geq?|neq|approx|sim|infty|partial|nabla|hat|bar|tilde|vec|"
                  r"var\w+|alpha|beta|gamma|delta|epsilon|zeta|eta|theta|iota|kappa|lambda|mu|"
                  r"nu|xi|pi|rho|sigma|tau|upsilon|phi|chi|psi|omega)\b", re.I)
ADDRESS = re.compile(r"^[A-Z][a-z]+\.? [A-Z]?\d+[a-z]?\s*:\s*")      # "Figure 3: " at the start
LINK = re.compile(r"\\(url|href|cite[a-z]*|ref|eqref|autoref|cref|Cref)\*?(\[[^\]]*\])?"
                  r"(\{[^}]*\})+|https?://\S+|www\.\S+", re.I)
# R11: PR #56's pointers, and "Table N shows the ...".
PLACE = r"(the )?(appendix|fig\.?|figure|table|section|sec)\b"
# The words of a pure pointer's subject: "Further details are provided in Appendix D."
POINTED_AT = r"(is|are) ((given|provided|shown|reported|listed|deferred|available|presented|" \
             r"included|described|discussed|detailed|documented) )?"
POINTER = re.compile(
    rf"(^(detailed|more|additional|further|full|a full|complete)\b[^,;]*\b{POINTED_AT}"
    rf"(in|at) {PLACE}[^,;]{{0,40}}$"
    rf"|\b(can be found|(is|are) (shown|given|provided|presented|reported|visuali[sz]ed|listed|"
    rf"detailed)) in {PLACE}[^,;]{{0,40}}$"
    rf"|^(we|i) (show|visuali[sz]e|plot|provide|present|report|list)\b[^,;]*\b(in|at) "
    rf"{PLACE}[^,;]{{0,30}}$"
    rf"|^the rest of (the|this) paper"
    rf"|^(table|figure|fig\.)\s*[A-Z]?\d+[a-z]?\s+(shows|presents|summari[sz]es|reports|lists)"
    rf"\s+the\b)", re.I)
# A statement of the paper's aim is a claim, not a pointer (it dropped 37 pieces before).
AIM = re.compile(r"\b((in|throughout) (this|our) (work|paper|study)|we (propose|introduce))\b",
                 re.I)
# A verb that states a result: a sentence with one says what is there, not only where.
RESULT_VERB = re.compile(r"\b(confirm\w*|demonstrat\w*|show(s|n|ed)? that|outperform\w*|"
                         r"improv\w*|reach\w*|achiev\w*|finds?|findings|(?<!be )found|"
                         r"observ\w*|indicat\w*|suggest\w*|reveal\w*|verif\w*|validat\w*|"
                         r"prov(e|es|ed)|hold|holds|same trend|consistent\w*|robust\w*|"
                         r"generaliz\w*|match\w*|exceed\w*|surpass\w*)\b", re.I)
COMPARATIVE = re.compile(r"\b(that|than|better|worse|higher|lower|best|worst|outperforms?\w*|"
                         r"improv\w*|increas\w*|decreas\w*|reduc\w*|gains?|drops?|beats?|fails?|"
                         r"superior|inferior|significant\w*)\b", re.I)
POINTER_MAX = 160               # characters; a longer sentence says more than where to look
# A checklist answer's justification is a claim about the paper, even when it only points.
CHECKLIST_ANSWER = re.compile(r"\[\s*(yes|no|n/?a)\s*\]", re.I)
# R12: a heading has at most this many words, and is compared without its number.
HEADING_WORDS = 6
NUMBERING = re.compile(r"^([A-Z]\.)?\d+(\.\d+)*\.?\s+")
# R13: what does not change an equation's meaning.
EQ_PREFIX = re.compile(r"^Eq\. \([^)]*\):\s*")
EQ_NOISE = re.compile(r"\\(left|right|[,;!:]|quad|qquad)|\s")
EQ_MIN = 6                      # characters left; a shorter one ("x=1") is in many sentences
REPEAT_MARK = " repeat of "     # in a repeat's reason, before the twin's id


def non_claim_reasons(cands: Candidates, quoted: Mapping[str, Collection[str]] | None = None,
                      dropped: Collection[str] = ()) -> dict[str, str]:
    """Why each unanswered candidate is not a claim, for those the rules catch.

    No rule drops a commit message. The repeat rules come last: R13 (an equation whose
    text is inside a sentence) and R14 (a later copy of the same words) drop a repeat only
    when its twin is not dropped, and name the twin; `claim_files.quote_problems` refuses
    the drop if the twin ends with no quote, or (for R13) with no quote that holds the
    equation (`holds`). Some rules would need LaTeX structure that
    candidates do not carry (the enclosing environment, the title block). The parser
    already removes comments and starts at the abstract, so R1 and R2 catch only what
    leaks through, from the text and the section path.

    Args:
        cands: the candidates, by id, in file order.
        quoted: the words of the quotes that already name each candidate, by candidate id.
        dropped: ids of candidates that already have a drop.

    Returns:
        {candidate id: "auto:Rn <name>"} for each candidate with no answer that a rule
        catches. A repeat's reason ends with its twin's id.
    """
    quoted = quoted or {}
    answered = set(quoted) | set(dropped)
    titles = _titles(cands)
    sectioned: set[object] = set()          # the notes in which a section has begun
    out: dict[str, str] = {}
    for cid, c in cands.items():
        title_block = c.get("origin") not in sectioned
        if c.get("kind") == extract.SENTENCE and c.get("section"):
            sectioned.add(c.get("origin"))
        if cid in answered or c.get("kind") == extract.COMMIT:
            continue
        why = _first_rule(c, titles.get(c.get("origin"), set()), title_block)
        if why:
            out[cid] = why
    gone = set(dropped) | set(out)
    out.update(_equation_repeats(cands, answered | gone, gone, quoted))
    gone = set(dropped) | set(out)
    out.update(_exact_repeats(cands, answered | gone, gone))
    return out


def _first_rule(c: Mapping[str, object], titles: set[str], title_block: bool = True
                ) -> str | None:
    """The reason of the first of R1 to R12 that catches the candidate, or None.

    `title_block` is False once a sentence of the same note had a section: R2 reads only
    what comes before the first section.
    """
    kind, text = c.get("kind"), str(c.get("text", "")).strip()
    section = str(c.get("section") or "")
    prose = kind == extract.SENTENCE or extract.is_caption(kind)
    if not prose:
        return None
    if COMMENT.match(text):
        return "auto:R1 comment"
    if kind == extract.SENTENCE and not section and title_block \
            and str(c.get("origin") or "").endswith(".tex") and _author_block(text):
        return "auto:R2 author block"
    if any(ACK_HEADING.fullmatch(_heading(h)) for h in section.split(" > ") if h) \
            and not COMPUTE.search(text):
        return "auto:R3 acknowledgement section"
    if kind == extract.SENTENCE and ACK_PHRASE.search(text) and not COMPUTE.search(text):
        return "auto:R4 acknowledgement"
    if CHECKLIST.search(section) and not ANSWER_TEXT.search(text) and _template(text):
        return "auto:R5 template"
    if STYLE.search(text) and not FIRST_PERSON.search(text):
        return "auto:R6 template"
    if kind == extract.SENTENCE and STUB.match(text):
        return "auto:R7 checklist stub"
    if _residue(text):
        return "auto:R9 layout residue"
    if _link_only(text):
        return "auto:R10 link only"
    if kind == extract.SENTENCE and _pointer(text):
        return "auto:R11 pointer"
    if kind == extract.SENTENCE and _heading(text) in titles and _heading_shape(text):
        return "auto:R12 heading"
    return None


def _author_block(text: str) -> bool:
    """R2: an e-mail address, an affiliation, or a note on the authors, with no digit or verb.

    The caller makes sure the piece is in a .tex file, before its first section.
    """
    if re.search(r"\d", text) or VERB.search(text):
        return False
    words = len(text.split())
    if EMAIL.search(text) and words <= 25:
        return True
    if AFFILIATION.search(text) and words <= 15:
        return True
    return bool(AUTHOR_NOTE.search(text)) and words <= 15


def _words(text: str) -> set[str]:
    """The lower-case words of a text."""
    return set(re.findall(r"[a-z]+", text.lower()))


TEMPLATE_WORDS = tuple(_words(t) for t in TEMPLATE_SENTENCES)


def _template(text: str) -> bool:
    """R5: almost every word of the piece is in one template sentence, and the reverse."""
    words = _words(text)
    if len(words) < TEMPLATE_MIN_WORDS:
        return False
    return any(len(words & t) >= TEMPLATE_SHARE * len(words)
               and len(words & t) >= TEMPLATE_COVER * len(t) for t in TEMPLATE_WORDS)


def _residue(text: str) -> bool:
    """R9: without LaTeX commands and a caption's address, no letter or only one token is left.

    A piece with math, an "=" or a digit (out of a caption's address and a graphics
    command's options) is never residue.
    """
    if MATH.search(GRAPHICS_OPTIONS.sub(" ", ADDRESS.sub("", text.strip()))):
        return False
    rest = ADDRESS.sub("", COMMAND.sub(" ", text)).strip()
    tokens = [t for t in rest.split() if re.search(r"\w", t)]
    return not re.search(r"[A-Za-z]", rest) or len(tokens) <= 1


def _link_only(text: str) -> bool:
    """R10: a link or citation, and fewer than two words beside it."""
    if not LINK.search(text):
        return False
    rest = re.sub(r"[^\w\s]", " ", COMMAND.sub(" ", LINK.sub(" ", text)))
    return len(re.findall(r"[A-Za-z]{2,}", rest)) < 2


def _pointer(text: str) -> bool:
    """R11: a short sentence that only says where to look, not what is there."""
    if len(text) >= POINTER_MAX or AIM.search(text) or COMPARATIVE.search(text) \
            or RESULT_VERB.search(text) or CHECKLIST_ANSWER.search(text):
        return False
    if re.search(r"\d", extract.NOT_CLAIM_NUMBERS.sub(" ", text)):
        return False                       # a number other than a table or figure's
    return bool(POINTER.search(text))


def _heading(text: str) -> str:
    """A title as compared: lower case, no numbering, no end punctuation, one space."""
    t = NUMBERING.sub("", text.strip())
    return " ".join(t.lower().rstrip(".:").split())


def _heading_shape(text: str) -> bool:
    """R12: PR #56's shape of a heading: a few words, no digit, no verb or pronoun."""
    words = re.findall(r"[A-Za-z][\w-]*", text)
    return 0 < len(words) <= HEADING_WORDS and not re.search(r"\d", text) \
        and not VERB.search(text)


def _titles(cands: Candidates) -> dict[object, set[str]]:
    """Every section title of each note, from the section paths of its candidates."""
    out: dict[object, set[str]] = {}
    for c in cands.values():
        for h in str(c.get("section") or "").split(" > "):
            if h.strip():
                out.setdefault(c.get("origin"), set()).add(_heading(h))
    return out


def _equation_key(text: str) -> str:
    """An equation's text as R13 compares it: no "Eq. (n):" label, no spacing commands."""
    return EQ_NOISE.sub("", EQ_PREFIX.sub("", text)).rstrip(".,;:")


def holds(cand: Mapping[str, object], twin_quotes: Collection[str]) -> bool:
    """Whether the quotes of a repeat's twin answer for the repeat, so its drop can stand.

    A copy of the same words (R14) is answered by any quote of its twin. An equation is
    answered only by a quote that holds its whole text: a quote of the start of its
    lead-in sentence leaves the equation in no quote.
    """
    if not twin_quotes:
        return False
    if cand.get("kind") != extract.EQUATION:
        return True
    eq = _equation_key(str(cand.get("text", "")))
    return any(eq in EQ_NOISE.sub("", q) for q in twin_quotes)


def _equation_repeats(cands: Candidates, skip: set[str], gone: set[str],
                      quoted: Mapping[str, Collection[str]]) -> dict[str, str]:
    """R13: each equation whose text is inside a sentence that is not dropped.

    A sentence that already has quotes is a twin only if one of them holds the equation.
    """
    sentences = [(cid, EQ_NOISE.sub("", str(c.get("text", ""))))
                 for cid, c in cands.items()
                 if c.get("kind") == extract.SENTENCE and cid not in gone]
    out = {}
    for cid, c in cands.items():
        if cid in skip or c.get("kind") != extract.EQUATION:
            continue
        eq = _equation_key(str(c.get("text", "")))
        if len(eq) < EQ_MIN:
            continue
        twin = next((sid for sid, s in sentences
                     if eq in s and cands[sid].get("origin") == c.get("origin")
                     and (sid not in quoted or holds(c, quoted[sid]))), None)
        if twin:
            out[cid] = f"auto:R13 equation{REPEAT_MARK}{twin}"
    return out


def _exact_repeats(cands: Candidates, skip: set[str], gone: set[str]) -> dict[str, str]:
    """R14: each later copy of a sentence, caption or equation whose first copy is not dropped.

    Whole tables are left.
    """
    first: dict[tuple, str] = {}
    out = {}
    for cid, c in cands.items():
        kind = c.get("kind")
        if kind in (extract.TABLE, extract.COMMIT):
            continue
        key = (kind, extract._norm(c.get("text", "")))
        if key not in first:
            first[key] = cid
            continue
        twin = first[key]
        if cid not in skip and twin not in gone:
            out[cid] = f"auto:R14{REPEAT_MARK}{twin}"
    return out


def twin(reason: str) -> str | None:
    """The candidate a repeat drop (R13 or R14) names, or None for another reason."""
    if reason.startswith(("auto:R13 ", "auto:R14 ")) and REPEAT_MARK in reason:
        return reason.split(REPEAT_MARK, 1)[1]     # a candidate id can hold spaces
    return None
