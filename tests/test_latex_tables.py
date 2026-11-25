#!/usr/bin/env python3
"""Check the LaTeX table reader and the folder reader on table shapes that real papers use.

The cases come from ICLR papers whose wrong numbers were missing from the candidates:
  - a `wraptable` float is read as a table;
  - a tabular with a tabular inside a header cell is read in full, not cut at the inner end;
  - a cell with \\multicolumn around \\multirow keeps only its text;
  - a bare tabular in a file that another file puts in with \\input is read, one time;
  - a sentence that two draft files repeat is listed one time.
  - each step of an algorithm float is a candidate of its own.
  - a \\multicolumn header spreads over its columns, so no data cell is dropped, and two
    header rows give each column its full path (dmzM5UdAq6);
  - a \\multirow label reaches every row of its block, also with a negative count;
  - bold cells, ticks and crosses stay readable, and each table is also one whole piece;
  - a \\scalebox size is not glued to the value it scales (9,002 cells on the ZH papers);
  - only the compiled paper is read: not a backup copy, an old draft or a table no file uses;
    a paper at the repository root does not hide a separate note in another folder;
    a supplement compiled on its own beside the paper is read, and each dropped full
    document is logged by name;
  - a \\cline under a header group does not end the header;
  - a label written once above a group of rows reaches the blank first cells below it, up
    to the next rule, and not past it (QhxjQOMdDF); a second header row between two rules
    joins the header (qqKJjwibsp); a rule thickness such as \\toprule[1.2pt] leaves no text;
  - code-review cases: a \\multirow label does not leak past its rule, a tick-only data row
    is not taken as a header, a nested-tabular label is copied whole, \\\\*[2pt] leaves no
    "*", a \\specialrule ends a group, and \\hline\\hline still lets a second header join;
  - second code-review cases: a rule after every row does not make the first data row a
    header, and digits inside model names are not numbers; a section row is not a rule; a
    dash pattern on \\cdashline or \\hdashline leaves no text, and \\hdashline is a full
    rule; a carried label does not carry bold; a bare \\\\* leaves no "*".
  - full-review case: rows of numbers above the first rule stay data rows, unless a
    spanning header or a partial rule above them marks them as a header level.
Returns 1 if any case fails.
"""
import logging
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from utils import extract, structure  # noqa: E402

MAIN = r"""\documentclass{article}
\begin{document}
\begin{abstract}We fine-tune GPT-2 with LoRA and report BLEU on E2E.\end{abstract}
\begin{wraptable}{r}{8cm}\caption{E2E results.}
\begin{tabular}{c|c|cc} \toprule
Method & Param & BLEU & NIST \\ \midrule
LoRA & $0.39$M & $63.7$ & $8.19$ \\
\end{tabular}\end{wraptable}
\begin{table*}\caption{Nested header cells.}\resizebox{\textwidth}{!}{
\begin{tabular}{@{}l@{}c|cc}
\multicolumn{4}{c}{\textbf{Low-level}}\\
& \textbf{\begin{tabular}[c]{@{}c@{}} Tools \end{tabular}} &
\textbf{\begin{tabular}[c]{@{}c@{}} AC \end{tabular}} &
\textbf{\begin{tabular}[c]{@{}c@{}} LC\\2Obj \end{tabular}} \\
\rowcolor{gray!20}
GPT-4V & - & 0.58 & 0.64 \\
\multicolumn{1}{l}{\multirow{2}{*}{SOBA}} & x & 90.00 & 0.37 \\
\end{tabular}}\end{table*}
\begin{table}\caption{Ablation.}\input{tab/ablation}\end{table}
\section{Results}
The ablation removes the source sample step and the score falls to 43.7 on Rouge.
\begin{algorithm}\caption{Addax}\begin{algorithmic}[1]
\STATE Randomly draw mini-batches $B^0$, $B^1$ uniformly from $D^0$, $D^1$
\STATE Estimate the first-order gradient on $B^1$
\end{algorithmic}\end{algorithm}
\end{document}
"""
ABLATION = r"""\begin{tabular}{@{}lll@{}}
\textbf{Method} & \textbf{EM} & \textbf{Rouge} \\
\quad - Source Sample & $41.7 (-2.3)$ & $43.7 (-3.5)$ \\
\end{tabular}
"""
SPANS = r"""\documentclass{article}
\begin{document}
\begin{table}\caption{GFLOPs.}\label{tab:spans}
\begin{tabular}{llcc} \toprule
& & \multicolumn{2}{c}{GFLOPs} \\ \cmidrule(lr){3-4}
Model & Attack & Total & Encoder \\ \midrule
\multirow{2}{*}{Ours} & SF & 171.70 & \textbf{56.18} \\
 & EAD & 172.00 & 57.00 \\
 & PGD & \scalebox{0.78}{0.368} & 2.0 \\
\multirow{-2}{*}{Base} & CW & 3.0 & \ding{55} \\
\bottomrule
\end{tabular}\end{table}
\end{document}
"""
GROUPS = r"""\documentclass{article}
\begin{document}
\begin{table}\caption{Settings.}
\begin{tabular}{lll}\toprule[1.2pt]
Method & Hyper-parameter & Value \\ \midrule
MeZO & Batch size & 64 \\
     & Learning rate & 1e-7 \\
     & Epsilon & 1e-3 \\ \midrule
Adam & Batch size & 8 \\
     & Learning rate & 3e-5 \\ \midrule
     & note & see text \\
\bottomrule\end{tabular}\end{table}
\begin{table}\caption{Results.}
\begin{tabular}{ll}\toprule
\multicolumn{1}{c}{Agent} & \multicolumn{1}{c}{Metrics} \\ \midrule
Model & SR \\ \midrule
GPT-4o & 24.1 \\
Claude & 30.2 \\
\bottomrule\end{tabular}\end{table}
\end{document}
"""
DRAFT = r"""\documentclass{article}
\begin{document}
\section{Results}
The ablation removes the source sample step and the score falls to 43.7 on Rouge.
\end{document}
"""



def table_rows(cands: list[dict]) -> list[str]:
    """The rows of every whole-table candidate, as the candidate's text writes them."""
    return [row for c in cands if c["kind"] == "table"
            for row in c["text"].split(" Rows: ", 1)[1].split(" || ")]

def main():
    """Run every case and report."""
    ok = True

    def check(name, cond, detail=""):
        """Print one case and keep the overall result."""
        nonlocal ok
        print(f"{'ok  ' if cond else 'FAIL'} {name}"
              + (f": {detail}" if detail and not cond else ""))
        ok &= bool(cond)

    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "tab"))
        files = (("main.tex", MAIN), ("tab/ablation.tex", ABLATION), ("old_draft.tex", DRAFT))
        for rel, text in files:
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(text)
        texts = table_rows(extract.candidates(extract.read_latex(os.path.join(root, "main.tex"))))
        check("a wraptable is read", any("BLEU: 63.7" in t for t in texts), str(texts))
        check("a tabular with tabulars in its header cells is read in full",
              any("GPT-4V" in t and "LC 2Obj: 0.64" in t for t in texts), str(texts))
        check("a wrapped cell keeps only its text",
              any(t.startswith(": SOBA;") for t in texts) and not any("2*" in t for t in texts),
              str(texts))
        check("a tabular that a table puts in with \\input is read",
              any("Rouge: 43.7 (-3.5)" in t for t in texts), str(texts))
        paper = extract.read_latex(os.path.join(root, "main.tex"))
        sents = [c["text"] for c in extract.candidates(paper) if c["kind"] == "sentence"]
        check("each algorithm step is a candidate of its own",
              any(t.startswith("Randomly draw mini-batches") and "gradient" not in t for t in sents)
              and any(t.startswith("Estimate the first-order gradient") for t in sents), str(sents))
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        found = structure.folder_candidates(root)
        rouge_rows = [c for c in found if c["kind"] == "table" and "43.7 (-3.5)" in c["text"]]
        check("the included file is read only through the file that includes it",
              len(rouge_rows) == 1 and not any(c["id"].startswith("tab/") for c in found),
              str([c["id"] for c in rouge_rows]))
        repeated = [c for c in found if "score falls to 43.7" in c["text"]]
        check("a sentence that a draft repeats is listed one time", len(repeated) == 1,
              str([c["id"] for c in repeated]))
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "spans.tex"), "w") as fh:
            fh.write(SPANS)
        found = extract.candidates(extract.read_latex(os.path.join(root, "spans.tex")))
        texts = table_rows(found)
        check("a multicolumn header gives every column its full path, so no cell is dropped",
              "Model: Ours; Attack: SF; GFLOPs / Total: 171.70; GFLOPs / Encoder: 56.18 [bold]"
              in texts, str(texts))
        check("a multirow label reaches the next row of its block",
              any(t.startswith("Model: Ours; Attack: EAD") for t in texts), str(texts))
        check("a negative multirow label reaches the rows above it, and a cross is a cross",
              any(t.startswith("Model: Base; Attack: PGD") for t in texts)
              and any("Attack: CW" in t and "✗" in t for t in texts), str(texts))
        check("a size argument is not part of the value: \\scalebox{0.78}{0.368} is 0.368",
              any("Attack: PGD; GFLOPs / Total: 0.368;" in t for t in texts), str(texts))
        check("each table is also one whole piece",
              any(c["kind"] == "table" and "171.70" in c["text"] and "3.0" in c["text"]
                  for c in found), str([c["kind"] for c in found]))
    with tempfile.TemporaryDirectory() as root:
        files = {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                             "The final score is 71.3 on the test set of the task.\n"
                             "\\input{tables/used}\n\\end{document}\n",
                 "main.bbl": "",
                 "tables/used.tex": "\\begin{tabular}{lc}\nModel & Acc \\\\\nOurs & 71.3 \\\\\n"
                                    "\\end{tabular}\n",
                 "tables/unused.tex": "\\begin{tabular}{lc}\nModel & Acc \\\\\nOld & 64.0 \\\\\n"
                                      "\\end{tabular}\n",
                 "backup/main_old.tex": "\\documentclass{article}\n\\begin{document}\n"
                                        "The final score is 69.9 on the test set of the task.\n"
                                        "\\end{document}\n"}
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(text)
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        texts = [c["text"] for c in structure.folder_candidates(root)]
        check("only the compiled paper is read, not a backup copy or an unused table",
              any("71.3" in x for x in texts)
              and not any("69.9" in x or "64.0" in x for x in texts),
              str(texts))
    with tempfile.TemporaryDirectory() as root:
        files = {"main.tex": "\\documentclass{article}\n\\begin{document}\n"
                             "The final score is 71.3 on the test set of the task.\n"
                             "\\begin{tabular}{lcc}\n& \\multicolumn{2}{c}{Acc} \\\\ \\cline{2-3}\n"
                             "Method & A & B \\\\ \\hline\nOurs & 0.1 & 0.2 \\\\\n\\end{tabular}\n"
                             "\\end{document}\n",
                 "notes/derivation.tex": "\\documentclass{article}\n\\begin{document}\n"
                                         "The derivation gives a bound of 4.2 on the error.\n"
                                         "\\end{document}\n",
                 "backup/main_old.tex": "\\documentclass{article}\n\\begin{document}\n"
                                        "The final score is 69.9 on the test set of the task.\n"
                                        "\\end{document}\n"}
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)) or root, exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(text)
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        found = structure.folder_candidates(root)
        texts = [c["text"] for c in found]
        check("a paper at the root does not hide a separate note, but drops its backup copy",
              any("4.2" in x for x in texts) and not any("69.9" in x for x in texts), str(texts))
        check("a cline under a header group does not end the header",
              any("Rows: Method: Ours; Acc / A: 0.1; Acc / B: 0.2" in x for x in texts),
              str(texts))
    with tempfile.TemporaryDirectory() as root:
        doc = "\\documentclass{article}\n\\begin{document}\n%s\n\\end{document}\n"
        files = {"paper/main.tex": doc % "The final score is 71.3 on the test set of the task.",
                 "paper/main.bbl": "",
                 "paper/supplementary.tex": doc % "\\input{sections/extra}",
                 "paper/sections/extra.tex": "The extra run reaches 88.8 on the held out set.\n",
                 "paper/main_old.tex": doc % "The final score is 69.9 on the test set of the task."}
        for rel, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(root, rel)), exist_ok=True)
            with open(os.path.join(root, rel), "w") as fh:
                fh.write(text)
        subprocess.run(["git", "-C", root, "init", "-q"], check=True)
        subprocess.run(["git", "-C", root, "add", "-A"], check=True)
        dropped = []
        handler = logging.Handler()
        handler.emit = lambda record: dropped.append(record.getMessage())
        logging.getLogger(structure.__name__).addHandler(handler)
        try:
            texts = [c["text"] for c in structure.folder_candidates(root)]
        finally:
            logging.getLogger(structure.__name__).removeHandler(handler)
        check("a supplement compiled on its own is read, with the files it puts in",
              any("71.3" in x for x in texts) and any("88.8" in x for x in texts), str(texts))
        check("an old draft beside the paper is still dropped, and the drop is logged",
              not any("69.9" in x for x in texts)
              and any("paper/main_old.tex" in m for m in dropped)
              and not any("supplementary" in m for m in dropped), str((texts, dropped)))
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "groups.tex"), "w") as fh:
            fh.write(GROUPS)
        paper = extract.read_latex(os.path.join(root, "groups.tex"))
        rows = table_rows(extract.candidates(paper))
        check("a label written once reaches the blank first cells of its group",
              "Method: MeZO; Hyper-parameter: Learning rate; Value: 1e-7" in rows, str(rows))
        check("a label does not cross a rule into the next group",
              "Method: Adam; Hyper-parameter: Learning rate; Value: 3e-5" in rows, str(rows))
        check("a blank cell that is not in a clean group stays blank",
              any(r.startswith("Method: ; Hyper-parameter: note") for r in rows), str(rows))
        check("a second header row between two rules joins the header, and [1.2pt] leaves no text",
              any(r.startswith("Agent / Model: GPT-4o; Metrics / SR: 24.1") for r in rows)
              and not any("1.2pt" in r for r in rows), str(rows))

    review = {
        "leak": (r"Method&HP&Value \\ \midrule \multirow{3}{*}{MeZO}&bs&64 \\ &lr&1e-7 \\ "
                 r"&eps&1e-3 \\ \midrule &note&5 \\ &note2&6",
                 lambda rows: rows[4][0] == "" and rows[5][0] == ""),
        "ticks": (r"Method&Open&Fast \\ \midrule Ours&\cmark&\cmark \\ \midrule Base&\xmark&12.3",
                  lambda rows: rows[0] == ["Method", "Open", "Fast"] and len(rows) == 3),
        "nested": (r"H&I&J \\ \midrule \begin{tabular}[c]{@{}c@{}}Long\\Name\end{tabular} "
                   r"& a & 1.0 \\ & b & 2.0",
                   lambda rows: extract._plain(rows[2][0]) == "Long Name"),
        "star": (r"Method & Acc \\*[2pt] \midrule Ours & 1.0 \\[0.5\baselineskip] Base & 2.0",
                 lambda rows: [r[0] for r in rows[1:]] == ["Ours", "Base"]),
        "specialrule": (r"M&H&V \\ \midrule Adam&bs&8 \\ &lr&3e-5 \\ "
                        r"\specialrule{.1em}{.05em}{.05em} &note&3",
                        lambda rows: rows[2][0] == "Adam" and rows[3][0] == ""),
        "double rule": (r"\hline Agent & Metric \\ \hline\hline Model & SR \\ \hline GPT-4o & 24.1 "
                        r"\\ Claude & 30.2",
                        lambda rows: rows[0][:2] == ["Agent / Model", "Metric / SR"]),
    }
    for name, (body, test) in review.items():
        rows, _, _, _ = extract._table_rows("lll", body)
        check(f"review case: {name}", test(rows), str(rows))

    second_review = {
        "a rule after every row does not make the first data row a header": (
            r"Hyper-parameter & Value \\ \hline Optimizer & AdamW \\ \hline "
            r"Learning rate & 1e-4 \\ \hline Batch size & 32 \\ \hline Epochs & 3 \\ \hline",
            lambda rows: rows[0][:2] == ["Hyper-parameter", "Value"]
            and rows[1][0] == "Optimizer"),
        "digits inside model names are not numbers below a second header": (
            r"Agent & Backbone \\ \midrule Model & Name \\ \midrule Qwen2.5 & Llama3 \\ "
            r"GPT4 & Mistral7B",
            lambda rows: rows[0][:2] == ["Agent", "Backbone"] and rows[1][0] == "Model"),
        "a first header that spans columns lets a second header join": (
            r"\multicolumn{2}{c}{Agent} & Metric \\ \midrule Model & Size & SR \\ \midrule "
            r"GPT-4o & 7 & 24.1",
            lambda rows: rows[0] == ["Agent / Model", "Agent / Size", "Metric / SR"]),
        "a section row is not a full rule for a second header": (
            r"Model & Size & Acc \\ \midrule \multicolumn{3}{l}{Encoders} \\ "
            r"Ours & base & large \\ \midrule BERT & base & 80.1 \\ RoBERTa & large & 81.0",
            lambda rows: rows[0] == ["Model", "Size", "Acc"] and rows[1][0] == "Ours"),
        "a cdashline dash pattern leaves no text": (
            r"A & bs & 8 \\ \cdashline{1-3}[2pt/2pt] C & x & 4",
            lambda rows: rows[1] == ["C", "x", "4"]),
        "an hdashline is a full rule and leaves no text": (
            r"M & H & V \\ \midrule A & bs & 8 \\ & lr & 3 \\ \hdashline B & x & 4 \\ & y & 5",
            lambda rows: [r[0] for r in rows[1:]] == ["A", "A", "B", "B"]),
        "an hdashline dash pattern leaves no text": (
            r"M & H & V \\ \midrule A & bs & 8 \\ & lr & 3 \\ \hdashline[2pt/2pt] B & x & 4 "
            r"\\ & y & 5",
            lambda rows: [r[0] for r in rows[1:]] == ["A", "A", "B", "B"]),
        "a carried label does not carry bold": (
            r"M & H & V \\ \midrule \textbf{A} & bs & 8 \\ & lr & 3",
            lambda rows: extract._cell_text(rows[1][0]) == "A [bold]"
            and extract._cell_text(rows[2][0]) == "A"),
        "a bare \\\\* is a line break and leaves no star": (
            r"M & H & V \\ \midrule A & bs & 8 \\* B & lr & 3",
            lambda rows: [r[0] for r in rows[1:]] == ["A", "B"]),
    }
    for name, (body, test) in second_review.items():
        rows, _, _, _ = extract._table_rows("lll", body)
        check(f"second review case: {name}", test(rows), str(rows))
    full_review = {
        "data rows above the first rule stay data rows": (
            r"\hline Model & Acc \\ BERT & 81.2 \\ RoBERTa & 83.4 \\ \hline Ours & 85.0 "
            r"\\ \hline",
            lambda rows: rows[0][:2] == ["Model", "Acc"]
            and [r[:2] for r in rows[1:]] == [["BERT", "81.2"], ["RoBERTa", "83.4"],
                                               ["Ours", "85.0"]]),
        "a number row under a spanning header is still a header row": (
            r"Method & \multicolumn{3}{c}{Shots} \\ & 1 & 5 & 10 \\ \midrule "
            r"Ours & 0.1 & 0.2 & 0.3",
            lambda rows: rows[0] == ["Method", "Shots / 1", "Shots / 5", "Shots / 10"]
            and len(rows) == 2),
        "a number row under a spanning header and a partial rule is still a header row": (
            r"& \multicolumn{2}{c}{Acc} \\ \cmidrule{2-3} Model & 2019 & 2020 \\ \midrule "
            r"Ours & 0.1 & 0.2",
            lambda rows: rows[0][:3] == ["Model", "Acc / 2019", "Acc / 2020"]
            and len(rows) == 2),
        "a partial rule alone does not make a row of numbers a header row": (
            r"\toprule & A & B \\ \cmidrule{2-2} \cmidrule{3-3} Harmful & 76.2 & 88.4 \\ "
            r"Harmless & 2.00 & 3.00 \\ \midrule Gap & 74.2 & 85.4",
            lambda rows: rows[0][:3] == ["", "A", "B"]
            and [r[0] for r in rows[1:]] == ["Harmful", "Harmless", "Gap"]),
        "a span over the label columns does not make a row of numbers a header row": (
            r"\multicolumn{2}{c}{Models} & \multicolumn{2}{c}{Ours} \\ "
            r"\multicolumn{2}{c}{Metric} & MSE & MAE \\ ETTm2 & 96 & 0.171 & 0.254 \\ "
            r"& 192 & 0.237 & 0.298 \\ \midrule ECL & 96 & 0.138 & 0.233",
            lambda rows: rows[0] == ["Models / Metric", "Models / Metric", "Ours / MSE",
                                     "Ours / MAE"]
            and [r[1] for r in rows[1:]] == ["96", "192", "96"]),
        "two word header rows above the first rule still join": (
            r"& Acc & F1 \\ Model & dev & dev \\ \midrule Ours & 0.1 & 0.2",
            lambda rows: rows[0][:3] == ["Model", "Acc / dev", "F1 / dev"] and len(rows) == 2),
    }
    for name, (body, test) in full_review.items():
        rows, _, _, _ = extract._table_rows("lll", body)
        check(f"full review case: {name}", test(rows), str(rows))
    print("all passed" if ok else "SOME FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
