"""`vcheck label-candidates`, a claim step that code does before the agent's round.

Answer each open candidate with quotes or a drop, from a small model's keep score
(core/claim_steps/labeller.py). The claim files lock is not held while the model answers.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core import cli_command
from core.claim_steps import labeller
from core.project import watched_project
from utils import candidate_rules, claim_files, models


class VcheckLabelCandidatesCommand(cli_command.CLICommand):
    """Runs `vcheck label-candidates`, a claim step of code (runtime/daemon.py)."""

    def configure_parser(self, p: argparse.ArgumentParser) -> None:
        """Add the arguments of `vcheck label-candidates`."""
        return None

    def execute(self, args: argparse.Namespace) -> int:
        """Label the open candidates, then write their quotes and drops.

        Open are the candidates with no answer, and those the rules dropped as a repeat of
        a candidate whose quotes do not yet answer for them (`candidate_rules.holds`): such
        a drop stands only if its twin is kept, with a quote that answers for it. The
        files are read under the lock, the model is asked without it, and the files are
        read again under the lock before the answers are written. A candidate that got an
        answer in the meantime keeps that answer.
        """
        root: Path = args.root
        state_dir: Path = args.state
        with claim_files.lock(state_dir).held():     # loading can begin a round, which writes
            files, _ = claim_files.loaded(state_dir, root)
        project = watched_project.WatchedProject.load(root, state_dir)
        settings = labeller.Settings.from_options(project.run_options(project.check_registry()))
        quoted = claim_files.quoted_candidates(files)
        words = claim_files.quote_words(files)
        repeats = {cid: twin for cid, d in files.dropped.items()
                   if isinstance(d, dict) and d.get("created") == claim_files.NEW
                   and (twin := candidate_rules.twin(str(d.get("why", "")))) is not None
                   and not candidate_rules.holds(files.candidates.get(cid, {}),
                                                 words.get(twin, []))}
        todo = {cid: c for cid, c in files.candidates.items()
                if cid in repeats or (cid not in quoted and cid not in files.dropped)}
        cache = labeller.read_cache(state_dir) if settings.cache else None
        count = labeller.LabelCount()
        try:
            labels = labeller.label_candidates(
                todo, settings, cache=cache, label_count=count,
                save=None if cache is None else lambda c: labeller.write_cache(state_dir, c))
        except (models.ProviderError, models.BadReply) as e:
            print(f"error: a model call failed, so no label was written: {e}", file=sys.stderr)
            return 1
        finally:
            if cache is not None:
                labeller.write_cache(state_dir, cache)
        with claim_files.lock(state_dir).held():
            files, r = claim_files.loaded(state_dir, root)
            quoted = claim_files.quoted_candidates(files)
            # a candidate answered while the model ran keeps that answer
            still_open = {cid for cid in todo
                          if cid in repeats or (cid not in quoted and cid not in files.dropped)}
            labels = {cid: a for cid, a in labels.items() if cid in still_open}
            quotes, drops = labeller.apply_labels(todo, labels, settings.cutoff, repeats,
                                                  claim_files.quote_words(files),
                                                  taken=files.quotes)
            setup = {cid for cid, why in drops.items() if why.startswith(claim_files.AUTO_SETUP)}
            for qid, q in quotes.items():
                files.quotes[qid] = claim_files.new_entry(**q)
                for cid in q["candidates"]:                 # a repeat whose twin was dropped
                    files.dropped.pop(cid, None)
                r.keep_scores.pop(q["candidates"][0], None)
            for cid, why in drops.items():
                files.dropped[cid] = claim_files.new_entry(why=why)
                keep = labels[cid]["keep"]
                assert isinstance(keep, int), keep
                r.keep_scores[cid] = keep
            register = claim_files.load_setup_register(state_dir)
            for cid in setup:
                c = todo[cid]
                register[cid] = {"text": str(c.get("text", "")), "kind": c.get("kind"),
                                 "section": c.get("section"), "keep": r.keep_scores[cid]}
            # keep only the setup facts that still stand, in paper order
            standing = {cid for cid, d in files.dropped.items() if isinstance(d, dict)
                        and str(d.get("why", "")).startswith(claim_files.AUTO_SETUP)}
            claim_files.save_setup_register(state_dir, {
                cid: register[cid] for cid in files.candidates
                if cid in register and cid in standing})
            claim_files.save(files, state_dir)
            claim_files.save_round(r, state_dir)
        kept = len({q["candidates"][0] for q in quotes.values()})
        print(f"{settings.model} labelled {len(todo)} candidates in {count.calls} "
              f"calls ({count.cached} answered from the cache): {kept} kept as "
              f"{len(quotes)} quotes, {len(drops) - len(setup)} dropped below "
              f"{settings.cutoff}"
              + (f", {len(setup)} sent to the setup register." if setup else "."))
        return 0
