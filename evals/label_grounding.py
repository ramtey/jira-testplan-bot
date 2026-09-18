"""Label grounding verdicts by hand, blind, so the judge can be measured.

A judge nobody checked is an opinion with a percentage sign. This shows the
same thing the judge saw — the assertion, the citation, the code — and asks
for your verdict without showing you its verdict. Agreement is then a real
number, and it is what licenses running a cheaper model: if Sonnet or Haiku
agrees with you as often as Opus does, every future run costs a fifth as much,
with evidence rather than hope.

    python evals/label_grounding.py sample     pick 100, stratified
    python evals/label_grounding.py label      work through them, resumable
    python evals/label_grounding.py agreement  Cohen's kappa, held-out

Sampling is stratified on purpose. Uniform sampling of 362 mostly-fine
citations would hand you ~90 easy SUPPORTS and tell you nothing about the
verdict the gate actually fires on. CONTRADICTS-suspects and citations with a
shape problem are oversampled so the rare, expensive class is measured.

The split matters as much as the sample: 70 to tune the judge prompt against,
30 held back and scored once. A kappa reported on the set the prompt was tuned
against is not a kappa.

Labels are written to EVAL_RESULTS_DIR, never into this repo, which is public.
"""

import argparse
import json
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RESULTS = Path(os.environ.get("EVAL_RESULTS_DIR", Path.home() / "sk_eval_results"))
CLAIMS = RESULTS / "grounding"
SNIPPETS = RESULTS / "snippets"
LABELS = RESULTS / "labels.json"
SAMPLE = RESULTS / "label_sample.json"

VERDICTS = {"s": "SUPPORTS", "i": "SILENT", "c": "CONTRADICTS"}
TUNE_SIZE = 70


def _all_claims():
    out = []
    for f in sorted(CLAIMS.glob("*.json")):
        doc = json.loads(f.read_text())
        for c in doc.get("claims") or []:
            if c.get("resolution") == "ok" and c.get("snippet_id"):
                out.append(c)
    return out


def phase_sample(size):
    claims = _all_claims()
    if not claims:
        sys.exit("No resolved claims. Run score_grounding.py resolve first.")
    rng = random.Random(20260918)

    # Three strata. The judge's own verdict decides two of them, which is a
    # sampling aid only — the labelling screen never shows it.
    suspect = [c for c in claims if c.get("shape_problems")]
    contradicts = [c for c in claims if c.get("verdict") == "CONTRADICTS"]
    rest = [c for c in claims
            if c not in suspect and c.get("verdict") != "CONTRADICTS"]

    want_c = min(len(contradicts), max(size // 4, 1))
    want_s = min(len(suspect), size // 3)
    want_r = max(size - want_c - want_s, 0)
    picked = (rng.sample(contradicts, want_c)
              + rng.sample(suspect, min(want_s, len(suspect)))
              + rng.sample(rest, min(want_r, len(rest))))
    rng.shuffle(picked)

    ids = [{"id": c["snippet_id"], "split": "tune" if i < TUNE_SIZE else "holdout"}
           for i, c in enumerate(picked)]
    SAMPLE.write_text(json.dumps(ids, indent=2))
    print(f"""
  sampled {len(ids)} claims -> {SAMPLE}
    contradicts-suspect  {want_c}
    shape-problem        {min(want_s, len(suspect))}
    everything else      {min(want_r, len(rest))}

    tune split {TUNE_SIZE}   holdout {max(len(ids) - TUNE_SIZE, 0)}

  Now run:  python evals/label_grounding.py label
""")


def _by_id():
    return {c["snippet_id"]: c for c in _all_claims()}


def phase_label():
    if not SAMPLE.exists():
        sys.exit("No sample yet. Run `sample` first.")
    sample = json.loads(SAMPLE.read_text())
    labels = json.loads(LABELS.read_text()) if LABELS.exists() else {}
    claims = _by_id()
    todo = [s for s in sample if s["id"] not in labels]
    print(f"\n  {len(labels)} labelled, {len(todo)} to go. Ctrl-C to stop; progress is kept.\n")

    for n, item in enumerate(todo, 1):
        c = claims.get(item["id"])
        if c is None:
            continue
        snippet = (SNIPPETS / f"{item['id']}.txt").read_text()
        print("=" * 78)
        print(f"  [{n}/{len(todo)}]  {c['key']}  {c['section']}:{c['index']}")
        print("=" * 78)
        print(f"\nASSERTION\n  {c.get('expected')}\n")
        print(f"CITES\n  {c.get('expected_source')}\n")
        print("CODE AT THAT COMMIT")
        print(snippet)
        print()
        print("  Does the cited code ESTABLISH the assertion?")
        print("    [s] SUPPORTS    a reader of these lines could stop checking")
        print("    [i] SILENT      real, related, but does not decide it")
        print("    [c] CONTRADICTS the code says otherwise")
        print("    [?] skip   [q] quit")
        while True:
            try:
                choice = input("  > ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print("\n  stopped; progress saved.")
                return
            if choice == "q":
                LABELS.write_text(json.dumps(labels, indent=2))
                print(f"\n  saved {len(labels)} labels to {LABELS}")
                return
            if choice == "?":
                break
            if choice in VERDICTS:
                labels[item["id"]] = VERDICTS[choice]
                LABELS.write_text(json.dumps(labels, indent=2))
                break
            print("    s / i / c / ? / q")
    LABELS.write_text(json.dumps(labels, indent=2))
    print(f"\n  done — {len(labels)} labels in {LABELS}")


def _kappa(pairs):
    """Cohen's kappa: agreement above what chance alone would produce."""
    if not pairs:
        return None
    cats = sorted({v for p in pairs for v in p})
    n = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / n
    expected = sum(
        (sum(1 for a, _ in pairs if a == c) / n) * (sum(1 for _, b in pairs if b == c) / n)
        for c in cats
    )
    if expected == 1:
        return 1.0
    return (observed - expected) / (1 - expected)


def phase_agreement():
    if not LABELS.exists():
        sys.exit("No labels yet. Run `label` first.")
    labels = json.loads(LABELS.read_text())
    sample = {s["id"]: s["split"] for s in json.loads(SAMPLE.read_text())}
    claims = _by_id()

    for split in ("tune", "holdout", "all"):
        pairs = [
            (labels[i], claims[i]["verdict"])
            for i in labels
            if i in claims and claims[i].get("verdict")
            and (split == "all" or sample.get(i) == split)
        ]
        if not pairs:
            continue
        agree = sum(1 for a, b in pairs if a == b)
        k = _kappa(pairs)
        print(f"\n  {split.upper():8} n={len(pairs)}   raw agreement {agree}/{len(pairs)} "
              f"{agree / len(pairs):.0%}   kappa {k:.2f}")
        # The headline number is not overall accuracy. A judge that is 95%
        # right overall and misses half the contradictions is useless for a
        # gate that fires on contradictions.
        human_c = [(a, b) for a, b in pairs if a == "CONTRADICTS"]
        if human_c:
            caught = sum(1 for _, b in human_c if b == "CONTRADICTS")
            print(f"           of {len(human_c)} you called CONTRADICTS, "
                  f"the judge caught {caught} ({caught / len(human_c):.0%})")
        judge_c = [(a, b) for a, b in pairs if b == "CONTRADICTS"]
        if judge_c:
            real = sum(1 for a, _ in judge_c if a == "CONTRADICTS")
            print(f"           of {len(judge_c)} it called CONTRADICTS, "
                  f"you agreed with {real} ({real / len(judge_c):.0%})")

    print("\n  Confusion (row = yours, col = judge):")
    cats = ["SUPPORTS", "SILENT", "CONTRADICTS"]
    print("               " + "".join(f"{c[:11]:>13}" for c in cats))
    for a in cats:
        row = [sum(1 for i in labels
                   if labels[i] == a and i in claims and claims[i].get("verdict") == b)
               for b in cats]
        print(f"    {a:11}" + "".join(f"{v:>13}" for v in row))
    print()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("phase", choices=["sample", "label", "agreement"])
    ap.add_argument("--size", type=int, default=100)
    args = ap.parse_args()
    if args.phase == "sample":
        phase_sample(args.size)
    elif args.phase == "label":
        phase_label()
    else:
        phase_agreement()


if __name__ == "__main__":
    main()
