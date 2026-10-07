# Proving a redline opens

The longest-standing honest gap in this project was that its output had only
ever been parsed by the library that wrote it. Three layers now stand between a
generated document and a lawyer's inbox, and it is worth being precise about
what each one does and does not prove.

## 1. Structural validation

`pipeline/validate.py` checks the OOXML constraints Word enforces on revision
markup: deleted text uses `w:delText` and never `w:t`, insertions carry no
`w:delText`, revision ids are unique, every revision has an author and a
well-formed date, relationship references resolve, and empty deleted text is
flagged because it means content was lost rather than marked deleted.

**Proves**: the file satisfies the rules a tracked-changes writer most often
breaks. **Does not prove**: that any real parser accepts it.

## 2. Round trip through python-docx

Every output is reopened by the library that produced it.

**Proves**: the package is internally coherent. **Does not prove** much beyond
that, because the writer and the reader share assumptions.

## 3. Round trip through LibreOffice

CI installs `libreoffice-writer` and converts each generated redline to plain
text. Converting is the cheapest operation that forces a full parse: a
malformed file fails rather than producing text. The suite includes a
deliberately corrupted document to confirm the gate rejects something, because
a check that passes everything is worthless.

**Proves**: an independent word processor, written by people who have never
seen this code, accepts the file. **Does not prove**: that Microsoft Word
accepts it. LibreOffice is more forgiving of some revision markup.

## What is still outstanding

**Nobody has opened one of these files in real Microsoft Word.**

That remains true and no amount of automated checking changes it. The layers
above make it unlikely that a malformed file reaches a lawyer, and "unlikely"
is not "verified". Before the first real document goes to a real client,
someone opens a redline in Word, accepts one change, rejects another, and
confirms the review pane shows what it should.

It is a ten-minute task and it is the single most valuable unticked item in the
repository.

## Running the checks locally

```bash
uv run pytest -q                   # LibreOffice tests skip if it is absent
uv run python -m evals.run_checks  # catch rate and false positives
```

On macOS, `brew install --cask libreoffice` enables the round-trip tests.

## Scoring the model

Everything above proves a file is well formed. None of it says whether the
review was right. The judgment features (reading a contract against the
playbook, answering the other side's draft, the key-terms table) are scored
by `evals/score_model.py` against `evals/model_corpus.py`: 22 generated
agreements whose answers are known.

| Set | Documents | Scored on |
| --- | --- | --- |
| playbook | 8, with 12 clauses planted off the starter playbook (every position file at least once) | recall: each planted clause owes a `playbook` finding naming the clause or anchored in it |
| silence | 5, every clause on position | any finding is a false positive |
| their paper | 3 supplier drafts with 8 positions to push back on and 9 typos | any tracked change in their text; any planted position missing from the issues list |
| key terms | 3, with the answer table known | parties, term, renewal, cap, governing law, notices: right, wrong or missing |
| realistic | the 3 clean agreements in `evals/realistic.py` | any finding is a false positive |

Every reply is also held to the voice rules in `agents/review_rubric.md`:
summary at most one sentence, no word on whether to send, titles that lead
with the clause, none of check, pass, severity, confidence, tool or sandbox,
starter findings labelled as the starter playbook, and a position and response
on every point of their paper. The mechanical checks are silent on the whole
corpus (a test pins it), so every finding scored is the model's.

The governing-law plant is Texas, not New York: the starter playbook's
fallback accepts New York, and a reviewer that stays quiet about it is right.

### The first live run

With credit on the account and the agents applied (`lra skills sync` for each
skill, then `lra agents apply`), one command:

```bash
.venv/bin/lra eval --live --budget-usd 40
```

It runs each document through `pipeline/review.py` exactly as
`lra review --live` does, with no memory stores mounted, and their-paper
results through the handler's own local steps (cosmetic findings withheld,
issues list built, redline written). Each session is capped on the platform at
`--per-doc-usd` (default $2) and no session starts unless a whole cap still
fits in `--budget-usd`, so the run can cost less than the budget and never
more; at the defaults all 22 fit in $44. `--time-budget` (default 3600
seconds) stops new sessions starting. A refused key or an empty account stops
the run at the first document. `--only playbook,silence` runs some sets.

Results are saved as they come to `work/model-eval/<time>-live/results/`, with
a scorecard (markdown and JSON) beside them. To rescore them after
changing the scorer, without paying again:

```bash
.venv/bin/lra eval --replay work/model-eval/<time>-live
```

Without credentials, `lra eval --stub perfect` and `lra eval --stub bad` score
canned right and wrong answers, which is how the tests prove the scorer tells
them apart. The exit code is 0 only when every target is met.

One gap the first run will show rather than hide: key terms are skill content
with no delivery path of their own. The review keeps only Word files from the
session's outputs, and collects them only in redline and proofread modes, so a
key-terms workbook written in question mode never comes back. The scorer reads
a workbook, a Word table or a table in the reply text, whichever arrives; if
none does, every field scores as missing.
