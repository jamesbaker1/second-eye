"""Score the deterministic check layer against the synthetic corpus.

Two numbers. The second is the one that decides whether the product survives.

  catch rate       of the defects we planted, how many were found
  false positives  findings raised on documents that are correct

Then the same two numbers for outbound messages (evals/outbound.py): a
covering note, a To line and attachments, with leftovers, wrong versions and
missing attachments planted, and every clean document sent under an ordinary
note as a control.

Run it with:  .venv/bin/python -m evals.run_checks
Add --verbose to see every finding.
"""

from __future__ import annotations

import sys
from collections import Counter

from evals.corpus import CLEAN, DEFECTIVE
from lra.models import Attachment, Severity
from lra.pipeline import checks, extract

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

# Categories that are informational rather than defects. Author metadata is
# reported on every document by design, so it is not counted as a false
# positive; it is an observation, not a claim that something is wrong.
INFORMATIONAL = {"leftovers"}


def run_one(spec):
    content = spec.build()
    att = Attachment(filename=f"{spec.name}.docx", content_type=DOCX,
                     size_bytes=len(content), content=content)
    doc = extract.extract(att)
    return checks.run_all(doc, content)


def main(verbose: bool = False) -> int:
    print("=" * 78)
    print("CLEAN DOCUMENTS  (any finding here is a false positive)")
    print("=" * 78)

    false_positives = []
    for spec in CLEAN:
        found = [f for f in run_one(spec) if not _informational(f)]
        mark = "ok  " if not found else "FAIL"
        print(f"  {mark}  {spec.name:28} {len(found)} finding(s)")
        for f in found:
            false_positives.append((spec.name, f))
            print(f"          [{f.severity.value:11}] {f.category:15} {f.title}")
            if verbose:
                print(f"                        anchor: {f.anchor[:70]!r}")

    print()
    print("=" * 78)
    print("DEFECTIVE DOCUMENTS  (planted defects that must be caught)")
    print("=" * 78)

    caught, missed = 0, []
    for spec in DEFECTIVE:
        found = run_one(spec)
        categories = {f.category for f in found}
        hit = spec.expect & categories
        gap = spec.expect - categories
        extra = categories - spec.expect - INFORMATIONAL

        caught += len(hit)
        for g in gap:
            missed.append((spec.name, g))

        mark = "ok  " if not gap else "MISS"
        print(f"  {mark}  {spec.name:28} caught {len(hit)}/{len(spec.expect)}"
              + (f"  missed: {', '.join(sorted(gap))}" if gap else ""))
        if extra:
            print(f"          also raised (not planted, may be fine): {', '.join(sorted(extra))}")
        if verbose:
            for f in found:
                print(f"          [{f.severity.value:11}] {f.category:15} {f.title}")

    total_planted = sum(len(s.expect) for s in DEFECTIVE)
    clean_docs = len(CLEAN)
    fp_per_doc = len(false_positives) / clean_docs if clean_docs else 0

    print()
    print("=" * 78)
    print("SCORE")
    print("=" * 78)
    print(f"  catch rate            {caught}/{total_planted}"
          f"  ({100 * caught / total_planted:.0f}%)")
    print(f"  false positives       {len(false_positives)} across {clean_docs} clean documents"
          f"  ({fp_per_doc:.2f} per document)")
    print(f"  target                catch > 90%, false positives < 1.00 per document")

    if false_positives:
        print()
        print("  false positives by category:")
        for cat, n in Counter(f.category for _, f in false_positives).most_common():
            print(f"    {n:3}  {cat}")

    if missed:
        print()
        print("  missed defects:")
        for name, cat in missed:
            print(f"    {name}: {cat}")

    blockers = [f for _, f in false_positives if f.severity is Severity.BLOCKER]
    if blockers:
        print()
        print(f"  {len(blockers)} of the false positives are BLOCKERS, which would tell a")
        print("  lawyer not to send a document that is fine. These matter most.")

    o_caught, o_planted, o_fp, o_clean = outbound(verbose)

    ok = (fp_per_doc < 1.0 and caught / total_planted > 0.9
          and o_fp == 0 and o_caught / o_planted > 0.9)
    print()
    print("=" * 78)
    print("TOGETHER")
    print("=" * 78)
    print(f"  documents             {caught}/{total_planted} planted defects caught, "
          f"{len(false_positives)} false positives across {clean_docs} clean documents")
    print(f"  outbound messages     {o_caught}/{o_planted} planted defects caught, "
          f"{o_fp} false positives across {o_clean} clean messages")
    print()
    print("  RESULT: " + ("meets target" if ok else "does not meet target"))
    return 0 if ok else 1


def _informational(f) -> bool:
    """An observation rather than a claim that something is wrong: the author
    line and a DRAFT watermark on a draft. A leftover reported as a point to
    fix (a comment, a tracked change, a hidden sheet) is not one, and counts."""
    return f.category in INFORMATIONAL and f.severity in (Severity.STYLE, Severity.FORMATTING)


def run_message(case):
    """Everything the review runs on a message before the model: the document
    checks on the attachment it reads, and the checks on the message."""
    from lra.pipeline import email_checks

    email, att = case.email()
    doc = extract.extract(att)
    return checks.run_all(doc, att.content) + email_checks.run_all(
        email, doc, att, case.external())


def _hit(marker: str, findings) -> bool:
    category, _, words = marker.partition(":")
    return any(f.category == category and words.lower() in f.title.lower() for f in findings)


def outbound(verbose: bool = False) -> tuple[int, int, int, int]:
    """The outbound messages (evals/outbound.py): planted, and clean controls."""
    from evals.outbound import CONTROLS, PLANTED, corpus_controls

    print()
    print("=" * 78)
    print("CLEAN OUTBOUND MESSAGES  (any finding here is a false positive)")
    print("=" * 78)
    controls = CONTROLS + corpus_controls()
    fps = 0
    for case in controls:
        found = [f for f in run_message(case) if not _informational(f)]
        fps += len(found)
        print(f"  {'ok  ' if not found else 'FAIL'}  {case.name:36} {len(found)} finding(s)")
        for f in found:
            print(f"          [{f.severity.value:11}] {f.category:15} {f.title}")

    print()
    print("=" * 78)
    print("OUTBOUND MESSAGES WITH PLANTED DEFECTS")
    print("=" * 78)
    caught = planted = 0
    for case in PLANTED:
        found = run_message(case)
        hits = {m for m in case.expect if _hit(m, found)}
        gap = case.expect - hits
        caught += len(hits)
        planted += len(case.expect)
        print(f"  {'ok  ' if not gap else 'MISS'}  {case.name:36} caught "
              f"{len(hits)}/{len(case.expect)}"
              + (f"  missed: {', '.join(sorted(gap))}" if gap else ""))
        if verbose or gap:
            for f in found:
                print(f"          [{f.severity.value:11}] {f.category:15} {f.title}")
    print()
    print(f"  outbound catch rate   {caught}/{planted}  ({100 * caught / planted:.0f}%)")
    print(f"  false positives       {fps} across {len(controls)} clean messages")
    return caught, planted, fps, len(controls)


if __name__ == "__main__":
    raise SystemExit(main(verbose="--verbose" in sys.argv))
