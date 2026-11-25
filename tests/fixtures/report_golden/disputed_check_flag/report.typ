#let data = ("verdict": "BLOCK UNTIL FIXED", "verdict-colour": "#b54708", "verdict-without": none, "verdict-without-colour": none, "disputed-flags-removed": "1 disputed flag removed", "dispute-reason-colour": "#667085", "commit": "cccccccccccc", "action": "Fix each issue that blocks the verdict, then run it again.", "reason": none, "dispute": ("heading": "How to Dispute False Issues", "code": "verifier dispute . --flag F-3a9c1e --why \"your reason\"  # mark one flag as false, with your reason\nverifier diagnosis . --share  # write the diagnosis of your disputes to one file", "send": "Please read the file to double-check its anonymity, then send it to verifier@meno.sh."), "facts": (("Documents checked", "0 (no tracked .tex file has \\documentclass, so the paper checks are off)"), ("Claims checked", "2"), ("Sources", "recomputed from data 2")), "axis-colours": ("integrity": "#b42318", "missing_inputs": "#6941c6", "soundness": "#c4320a", "presentation": "#b54708", "strength": "#0e7090", "caveat": "#667085"), "overview-heading": "Overview of Issues", "has-issues": true, "severities": (("presentation", "presentation issue (blocking)", "1"), ("strength", "strength issue (disclosure)", "5")), "sections": (("heading": "Claims Needing Attention", "note": none, "warning": none, "is-caveats": false, "anchor": "section-0", "count-words": "6 issues (1 disputed)", "count": "6 (1 disputed)", "issue-count": 6, "groups": (("kind": "claim", "claim-id": "good", "statement": "Training lowers the Brier score to 0.075 on the main run.", "place": "Claim 1 of 2", "inferred-from": none, "count-words": "1 issue (1 disputed)", "issues": (("flag": "F-f8dc80", "code": "undeclared-difference", "axis": "strength", "effect": "strength issue (disclosure)", "detail": "compared against prior but judge.model also differs ('claude-haiku-4.5' vs 'gpt-4o')", "quote": none, "facts": (), "why": "The method reads runs/old/; the notes use runs/fixed/."),), "anchor": "group-0-0"), ("kind": "claim", "claim-id": "prior", "statement": "The untrained model has a Brier score of 0.205.", "place": "Claim 2 of 2", "inferred-from": none, "count-words": "5 issues", "issues": (("flag": "F-6c8d68", "code": "caption-names-other-model", "axis": "presentation", "effect": "presentation issue (blocking)", "detail": "caption says model 'Llama-3-8B', run setup says 'Qwen3-8B'", "quote": none, "facts": (), "why": none), ("flag": "F-da9073", "code": "below-baseline", "axis": "strength", "effect": "strength issue (disclosure)", "detail": "does not beat the trivial baseline: scored 0.205 against a baseline of 0.200, and lower is better here, so this result is no better than always predicting the base rate", "quote": none, "facts": (), "why": none), ("flag": "F-751760", "code": "too-few-seeds", "axis": "strength", "effect": "strength issue (disclosure)", "detail": "only 1 seed(s); the text must not claim reliability", "quote": none, "facts": (), "why": none), ("flag": "F-3cb308", "code": "sample-shrank", "axis": "strength", "effect": "strength issue (disclosure)", "detail": "scored episodes dropped to 4 from 10 -- measurement may be destroyed, not improved", "quote": none, "facts": (), "why": none), ("flag": "F-759ec3", "code": "undeclared-difference", "axis": "strength", "effect": "strength issue (disclosure)", "detail": "compared against good but judge.model also differs ('gpt-4o' vs 'claude-haiku-4.5')", "quote": none, "facts": (), "why": none)), "anchor": "group-0-1"))), ("heading": "Caveats", "note": "These do not block: a check did not run, or a cautious check flagged a sentence.", "warning": none, "is-caveats": true, "anchor": "section-1", "count-words": "0 issues", "count": "0", "issue-count": 0, "groups": ())), "measures": (), "not-run": (("interval", "the computational method gives no interval", 2), ("run_data", "no run given", 2), ("run_integrity", "no run given", 2), ("run_outputs", "no run given", 2)), "not-run-claims": ())

#let title = sys.inputs.at("title", default: none)
#let axis-colour(axis) = rgb(data.axis-colours.at(axis))
#let tag(axis, label) = box(
  fill: axis-colour(axis).lighten(88%), inset: (x: 4pt, y: 2pt), radius: 3pt,
  text(size: 8pt, weight: "bold", fill: axis-colour(axis), label))
#let mono(s) = text(font: "DejaVu Sans Mono", size: 7.5pt, s)
#let muted(s) = text(fill: luma(100), s)
#let flag-id(id) = text(font: "DejaVu Sans Mono", size: 7pt, fill: luma(110), id)
#let dispute-reason(why) = text(size: 8.5pt, fill: rgb(data.dispute-reason-colour),
  [Disputed because: #why])
#let struck-if-disputed(why, body) = if why == none { body } else { strike(body) }
// The paragraphs and bullets of a fact (TypstReportRenderer.fact_data): bullets in a row
// make one list.
#let fact-body(parts) = {
  let out = ()
  let items = ()
  for (is-bullet, t) in parts {
    if is-bullet { items.push(t) } else {
      if items.len() > 0 { out.push(list(..items)); items = () }
      out.push(par(t))
    }
  }
  if items.len() > 0 { out.push(list(..items)) }
  out.join()
}

#set document(title: "Verifier report")
#set page(paper: "a4", margin: (x: 1.7cm, y: 1.8cm),
  footer: context [
    #set text(size: 8pt, fill: luma(110))
    Verifier report #if title != none [-- #title] #h(1fr)
    #counter(page).display("1 / 1", both: true)])
#set text(font: "Libertinus Serif", size: 10pt)
#set par(justify: false, leading: 0.55em)
#set table(stroke: none, inset: (x: 5pt, y: 3.5pt))
#show heading.where(level: 1): it => block(above: 1.4em, below: 0.7em,
  text(size: 13pt, weight: "bold", it.body))

#text(size: 20pt, weight: "bold")[Verifier report]
#if title != none [#h(0.5em) #text(size: 20pt, fill: luma(90), title)]
#v(0.3em)

#let colour = rgb(data.verdict-colour)
#if data.verdict-without == none [
  #block(width: 100%, fill: colour.lighten(90%), stroke: (left: 4pt + colour),
    inset: (x: 12pt, y: 10pt), radius: (right: 4pt))[
    #text(size: 16pt, weight: "bold", fill: colour, data.verdict)
    #if data.commit != none [#h(1fr) #muted[commit] #mono(data.commit)]
    #v(0.2em)
    #text(weight: "bold", data.action)
    #if data.reason != none [\ #text(size: 9pt, data.reason)]
  ]
] else [
  #let half = colour.lighten(50%)
  #let after = rgb(data.verdict-without-colour)
  #block(width: 100%, fill: colour.lighten(95%), stroke: (left: 4pt + half),
    inset: (x: 12pt, y: 7pt), radius: (right: 4pt), below: 3pt)[
    #text(size: 12pt, weight: "bold", fill: half, strike(data.verdict))
    #if data.commit != none [#h(1fr) #muted[commit] #mono(data.commit)]
    #v(0.1em)
    #text(size: 9pt, weight: "bold", data.action)
    #if data.reason != none [\ #text(size: 8.5pt, data.reason)]
  ]
  #block(width: 100%, fill: after.lighten(90%), stroke: (left: 4pt + after),
    inset: (x: 12pt, y: 10pt), radius: (right: 4pt))[
    #text(size: 16pt, weight: "bold", fill: after, data.verdict-without)
    #h(0.4em) #text(size: 10pt, fill: after, [(#data.disputed-flags-removed)])
  ]
]

#table(columns: (auto, 1fr),
  ..data.facts.map(f => (text(weight: "bold", f.at(0)), f.at(1))).flatten())

#if data.dispute != none [
  = #data.dispute.heading
  #block(width: 100%, fill: luma(245), inset: 8pt, radius: 3pt,
    raw(data.dispute.code, lang: "bash", block: true))
  #data.dispute.send
]

#let type-group(g) = [
  #block(breakable: false, above: 1.3em, below: 0.6em)[
    #heading(level: 3, outlined: false)[#text(size: 11pt, raw(g.code))
      #h(0.4em) #text(size: 10pt, weight: "regular", fill: luma(90), g.count-words)
      #h(1fr) #tag(g.axis, g.effect)] #label(g.anchor)
    #line(length: 100%, stroke: 0.6pt + axis-colour(g.axis))
  ]
  #if g.same != none [
    All #g.issue-count say: _#g.same _
    #let kept = g.subjects.zip(g.flag-ids, g.dispute-reasons).filter(x => x.at(2) == none)
    #if kept.len() > 0 [
      #columns(g.columns, gutter: 10pt, kept.map(((s, f, w)) =>
        [#flag-id(f) #mono(s)]).join(linebreak()))
    ]
    #for (s, f, w) in g.subjects.zip(g.flag-ids, g.dispute-reasons).filter(x => x.at(2) != none) [
      #strike[#flag-id(f) #mono(s)] \ #dispute-reason(w) \
    ]
  ] else if g.table [
    #let first = g.shared == none
    #if not first [In #mono(g.shared):]
    #table(
      columns: (auto,) + (if first { (if g.wide.contains(true) { auto } else { 1fr },) }
        else { () }) + g.wide.map(w => if w { 1fr } else { auto }),
      align: (left,) + (if first { (left,) } else { () })
        + g.wide.map(w => if w { left } else { right }),
      stroke: (x, y) => (top: if y > 1 { 0.4pt + luma(220) }),
      table.header([*Flag*], ..(if first { ([*#g.subject-head*],) } else { () }),
        ..g.labels.map(l => [*#l*])),
      table.hline(stroke: 0.6pt + luma(160)),
      ..g.issues.map(it => {
        let cells = ((flag-id(it.at(5)),) + (if first { (mono(it.at(0)),) } else { () })
          + it.at(3).map(f => f.at(1))).map(c => struck-if-disputed(it.at(6), c))
        if it.at(6) == none { cells } else {
          cells + (table.cell(colspan: cells.len(), dispute-reason(it.at(6))),) }
      }).flatten())
  ] else if g.cards [
    #for it in g.issues [
      #block(width: 100%, breakable: true, above: 1em, below: 1em,
        stroke: (left: 2pt + axis-colour(g.axis).lighten(40%)), inset: (left: 9pt, y: 3pt))[
        #struck-if-disputed(it.at(6))[#flag-id(it.at(5)) #h(0.3em)
          #text(size: 10.5pt, weight: "bold", it.at(0))]
        #if it.at(4) != none [#h(1fr) #tag(g.axis, it.at(4))]
        #v(0.1em)
        #grid(columns: (4.6cm, 1fr), column-gutter: 8pt, row-gutter: 6pt,
          ..it.at(3).map(f => (
            struck-if-disputed(it.at(6),
              text(size: 8.5pt, weight: "bold", fill: luma(90), f.at(0))),
            struck-if-disputed(it.at(6),
              if f.at(2) { f.at(1).split(", ").map(s => mono(s)).join(", ") }
              else { fact-body(f.at(3)) }),
          )).flatten())
        #if it.at(6) != none [#dispute-reason(it.at(6))]
      ]
    ]
  ] else [
    #let detail(it) = [#struck-if-disputed(it.at(6))[#flag-id(it.at(5)) #it.at(1)
      #if it.at(2) != none [
      \ #text(size: 8.5pt, fill: luma(90), style: "italic", it.at(2))]]
      #if it.at(6) != none [\ #dispute-reason(it.at(6))]]
    #if g.shared != none [
      In #mono(g.shared):
      #table(columns: (1fr,), stroke: (x, y) => (top: if y > 0 { 0.4pt + luma(220) }),
        ..g.issues.map(it => detail(it)))
    ] else if g.stacked [
      #table(columns: (1fr,), stroke: (x, y) => (top: if y > 0 { 0.4pt + luma(220) }),
        ..g.issues.map(it => [#struck-if-disputed(it.at(6), mono(it.at(0)))
          \ #pad(left: 10pt, detail(it))]))
    ] else [
      #table(columns: (auto, 1fr), stroke: (x, y) => (top: if y > 0 { 0.4pt + luma(220) }),
        ..g.issues.map(it => (struck-if-disputed(it.at(6), mono(it.at(0))), detail(it))).flatten())
    ]
  ]
]

#let claim-group(g) = [
  #block(breakable: false, above: 1.3em, below: 0.6em)[
    #heading(level: 3, outlined: false)[#text(size: 11pt, g.place) #h(0.4em)
      #text(size: 9pt, weight: "regular", raw(g.claim-id)) #h(0.4em)
      #text(size: 10pt, weight: "regular", fill: luma(90), g.count-words)] #label(g.anchor)
    #line(length: 100%, stroke: 0.6pt + luma(160))
  ]
  #if g.statement != none [#block(below: 0.4em, emph(g.statement))]
  #if g.inferred-from != none [
    #block(above: 0.7em, below: 0.6em, text(size: 9pt)[#g.inferred-from.])]
  #for it in g.issues [
    #block(width: 100%, breakable: true, above: 0.8em, below: 0.8em,
      stroke: (left: 2pt + axis-colour(it.axis).lighten(40%)), inset: (left: 9pt, y: 3pt))[
      #struck-if-disputed(it.why)[#flag-id(it.flag) #h(0.3em) #mono(it.code)]
      #h(1fr) #tag(it.axis, it.effect)
      #if it.detail != none [\ #struck-if-disputed(it.why, it.detail)]
      #if it.quote != none [\ #text(size: 8.5pt, fill: luma(90), style: "italic", it.quote)]
      #if it.facts.len() > 0 [
        #grid(columns: (4.6cm, 1fr), column-gutter: 8pt, row-gutter: 6pt,
          ..it.facts.map(f => (
            struck-if-disputed(it.why, text(size: 8.5pt, weight: "bold", fill: luma(90), f.at(0))),
            struck-if-disputed(it.why,
              if f.at(2) { f.at(1).split(", ").map(s => mono(s)).join(", ") }
              else { fact-body(f.at(3)) }),
          )).flatten())
      ]
      #if it.why != none [#dispute-reason(it.why)]
    ]
  ]
]

#if data.has-issues [
  = #data.overview-heading
  #table(columns: (1fr, auto), align: (left, right),
    table.header([*Section*], [*Issues*]),
    table.hline(stroke: 0.6pt + luma(160)),
    ..data.sections.filter(s => s.issue-count > 0)
      .map(s => (link(label(s.anchor), s.heading), s.count)).flatten())
  #table(columns: (1fr, auto), align: (left, right),
    table.header([*Severity*], [*Issues*]),
    table.hline(stroke: 0.6pt + luma(160)),
    ..data.severities.map(((axis, effect, n)) => (tag(axis, effect), n)).flatten())
]

#let checks-not-run = [
  #block(above: 1.3em, below: 0.6em, heading(level: 2, outlined: false,
    text(size: 11pt)[Checks that did not run]))
  #table(columns: (auto, 1fr, auto), align: (left, left, right),
    table.header([*Check*], [*Why it did not run*], [*Claims*]),
    table.hline(stroke: 0.6pt + luma(160)),
    ..data.not-run.map(n => (mono(n.at(0)), n.at(1), [#n.at(2)])).flatten())
  #for (name, rows) in data.not-run-claims [
    #block(above: 1.2em, below: 0.5em, text(size: 10.5pt, weight: "bold")[Why #raw(name) did
      not run])
    #table(columns: (auto, 1fr), stroke: (x, y) => (top: if y > 1 { 0.4pt + luma(220) }),
      table.header([*Claim*], [*Why it did not run*]),
      table.hline(stroke: 0.6pt + luma(160)),
      ..rows.map(r => (mono(r.at(0)), r.at(1))).flatten())
  ]
]

#for s in data.sections [
  #heading(level: 1)[#s.heading] #label(s.anchor)
  #if s.note != none [#muted(s.note)]
  #if s.warning != none [#block(below: 0.8em, text(fill: luma(70), emph(s.warning)))]
  #for g in s.groups [
    #if g.kind == "claim" { claim-group(g) } else { type-group(g) }
  ]
  #if s.is-caveats and data.not-run.len() > 0 { checks-not-run }
]

#if data.measures.len() > 0 [
  = Numbers the claim checks measured
  #table(columns: (auto, auto, 1fr), align: (left, left, right),
    table.header([*Claim*], [*Check*], [*Number*]),
    table.hline(stroke: 0.6pt + luma(160)),
    ..data.measures.map(m => (mono(m.at(0)), mono(m.at(1)), m.at(2))).flatten())
]

#if data.sections.len() == 0 and data.measures.len() == 0 [
  Nothing to report.
]
