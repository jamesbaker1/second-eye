# Writing tracked changes into .docx

This is the load-bearing technical problem. Everything else in this repo is
plumbing that has been built a hundred times. This part is where the product
either works or does not.

## Why it is hard

A tracked change in Word is not a diff overlay. It is revision markup inside
`word/document.xml`: deleted text wrapped in `<w:del>` with its `<w:t>` turned
into `<w:delText>`, inserted text wrapped in `<w:ins>`, each carrying
`w:author`, `w:date`, and a unique `w:id`.

The failure mode that kills naive implementations is *run splitting*. Word
splits a sentence into arbitrary runs based on formatting, spell-check state,
and editing history. The phrase you want to replace routinely spans three runs
with a bold word in the middle. Rewriting text without correctly splitting and
reassembling runs produces a file that either loses formatting or does not open.

A document that does not open is a catastrophic failure for this product. It is
a client document, and the user forwarded it here to be made safer.

## Two viable strategies

### A. Direct markup
Locate the anchor text in the document tree, split the runs at the boundaries,
wrap the old runs in `<w:del>`, insert new runs in `<w:ins>`.

- Precise control, one edit per finding, clean authorship attribution.
- Every run-splitting edge case is ours to get right.
- Libraries: `docx-redline` (writes `w:ins`/`w:del`/`w:moveFrom` on top of
  python-docx and lxml), `docx-revisions` (read/write revisions, accept/reject),
  `docx-editor` (track changes and comments without Word).

### B. Clean edit, then diff
Have the model produce a fully edited clean document, then diff the original
against the edited version and let a comparison engine generate native tracked
changes.

- Sidesteps run splitting entirely. The diff engine handles it.
- Library: `Python-Redlines`, an open-source DOCX comparison tool that generates
  native Word tracked changes without a Microsoft Word dependency.
- Cost: we lose the one-to-one mapping between a finding and a change, so the
  email cannot say "this specific edit corresponds to this specific reason"
  unless we reconstruct it from the diff.

## What was built, and why not what this section first recommended

The original recommendation here was to start with **B** for speed, then move to
**A** for the findings where a one-to-one mapping is worth it, and keep B as the
fallback for anything A cannot anchor.

**What shipped is A only.** `pipeline/ooxml.py` writes `w:ins` and `w:del` at
the lxml level, one revision per finding. Python-Redlines is not a dependency
and there is no diff path anywhere in the repo.

The reason is the email, not the XML. Strategy B produces a document whose
changes are real but anonymous: the redline says *what* changed and the memo
says *why*, and nothing connects the two. This product's reply is a manifest --
"changed X because Y", with the lawyer deciding change by change -- and that
manifest is only truthful if each tracked change came from one named finding.
A diff cannot give that back without reconstructing it, at which point it is
strategy A with extra steps.

**There is no B fallback, and the gap is filled differently.** When an anchor is
missing, ambiguous, or spans paragraphs, `redline.apply` does not diff -- it
declines to write the edit and the finding degrades into a line in the email
instead, with a note saying so. A wrong edit in a client's contract is worse
than a missing one, and a finding that arrives as a sentence still gets read.

`redline.verify_opens()` runs on every output before it is attached to an email.
If it fails, the reply degrades to memo-only rather than sending a broken file.
That degradation path is not optional.

## Non-negotiables

1. Never send a document that has not round-tripped through a parser.
2. Never bake an edit in silently. Every change is tracked and attributable.
3. Never strip pre-existing tracked changes or comments from someone else.
4. Preserve headers, footers, numbering, styles, and embedded objects. A redline
   that reformats the document is worse than no redline.

## Sources

- https://github.com/JSv4/Python-Redlines
- https://pypi.org/project/docx-redline/
- https://github.com/balalofernandez/docx-revisions
- https://github.com/pablospe/docx-editor/
- https://learn.microsoft.com/en-us/answers/questions/2259409/how-to-add-tracked-change-to-xml-in-docx-file-prog
