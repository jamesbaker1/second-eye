# Contributing

Thank you for wanting to help. Second Eye handles privileged legal
documents, so the bar for a change is that it is tested, that it says what it
does not do, and that it never makes the review noisier. This page covers
setting up, running the tests, the style, and the one formality: the
contributor licence agreement.

## Before you start

- **Never attach or commit a real client document, email or matter**, in an
  issue, a pull request, a test or a fixture. Every document in this
  repository is synthetic. If a bug only shows on a real document, describe
  its shape (the clause, the numbering, the run structure) and build a
  synthetic one that reproduces it.
- **Security problems go to private reporting**, not to an issue
  (`SECURITY.md`).
- For anything larger than a fix, open an issue first and say what you want
  to change and why. Some directions are settled on purpose (`DECISIONS.md`),
  and it is better to find that out before writing the code.

## Setting up

You need Python 3.11 or later, [uv](https://docs.astral.sh/uv/), and Node 22
for the Workers. LibreOffice is optional but recommended: the redline
round-trip tests use it as an independent parser and skip without it (`brew
install --cask libreoffice`, or `apt-get install libreoffice-writer`).

```bash
uv venv
uv pip install -e ".[dev]"
cp .env.example .env           # nothing in it is needed for the tests
```

Call the tools in the venv directly (`.venv/bin/python`, `.venv/bin/lra`),
not through `uv run`. In a directory with a `[project]` table, `uv run`
re-syncs the environment first, without the `dev` extra, and uninstalls
pytest and ruff.

To see it work with no model and no network:

```bash
.venv/bin/lra replay samples/example.eml     # prints the reply it would send
.venv/bin/lra compare earlier.docx later.docx
.venv/bin/lra clean draft.docx
```

## Running the tests

The whole Python suite runs in both storage modes, local SQLite and the
Cloudflare D1 adapter. CI runs both, and a pull request should pass both.

```bash
.venv/bin/python -m ruff check src tests
.venv/bin/python -m pytest -q
LRA_TEST_BACKEND=d1 .venv/bin/python -m pytest -q
.venv/bin/python -m evals.run_checks          # catch rate and false positives
```

With `LRA_TEST_BACKEND=d1`, every test's storage goes through the D1 adapter
and its JSON wire protocol against a fake Worker (`tests/conftest.py`), so a
storage change that only works on SQLite fails. `evals.run_checks` exits
non-zero if the deterministic checks miss a planted defect or raise a false
positive on a clean document.

No test calls Anthropic. The model is replaced by fakes held to the
Anthropic SDK's own types (`tests/api_contract.py`), so a fake that accepts a
request the real API would reject fails the test.

The Workers have their own suites, which run in workerd:

```bash
cd cloudflare && npm ci && npm run check && npm test
cd cloudflare/dms-mcp && npm ci && npm run check && npm test
```

`npm run check` type-checks the Worker and its tests; `npm test` runs them
under Cloudflare's Vitest pool.

## Code style

- Python is checked with ruff (`pyproject.toml`: line length 100). Run it
  before you push.
- Comments say **why**, not what: the constraint, the bug it prevents, the
  decision it follows. Many modules cite the decision (`DECISIONS 30`) or the
  doc they implement; keep that up.
- A fix comes with a test built from the text or file that broke, so the
  same shape cannot come back.
- A deterministic check is pinned from both sides: a test that it fires on
  the defect, and a test that it stays silent on a correct document. False
  positives are the failure that matters most (`PRODUCT.md`); a check that
  adds one to the evaluation corpus will not be merged.
- If you name a file in a doc, it must exist (`tests/test_docs_references.py`
  checks).
- No new dependency without a reason in a comment beside it in
  `pyproject.toml`. **No dependency under the GPL or AGPL**: the project is
  also licensed commercially (`LICENSING.md`), which a copyleft dependency
  would end.

## Commit messages

Look at `git log` for the house style. The subject states the outcome, as a
sentence about the product, not the activity:

```
Mail is admitted only when the sender's domain passes DMARC
A review session at Anthropic is deleted when the review ends, not archived
```

not "Fix DMARC check" or "Update managed.py". The body, wrapped at about 75
columns, explains why: what was wrong, what changed, what was decided, and
what was not tested. Keep separate changes in separate commits.

## The contributor licence agreement

Second Eye is licensed under AGPL-3.0-only and also commercially. To
include your contribution in both, we need you to sign the CLA in `CLA.md`
once. You keep the copyright in your work, and any version that includes
it stays available as open source. `LICENSING.md`, "Why is there a CLA?",
explains the reasons and the limits.

When you open your first pull request, a bot will ask you to comment:

> I have read the CLA Document and I hereby sign the CLA

If you contribute as part of your job, your employer signs too; `CLA.md`
says how. A pull request cannot be merged until the check is green.

## What is welcome

- **Word compatibility reports.** Nothing this produces has yet been opened
  in real Microsoft Word. `.venv/bin/python -m evals.word_pack` writes one
  file of each kind with a checklist; opening them in Word (Windows or Mac,
  say which version) and reporting what you see is among the most useful
  things anyone can do.
- **Reply formats.** Mail clients quote, wrap and sign replies in endless
  ways. A reply the router misreads, as a synthetic fixture with the client
  named, is a welcome bug report and a welcome test.
- **Deterministic checks**, with tests from both sides and a corpus entry.
- **False positives**: a correct clause the checks flag, reduced to a
  synthetic example.
- **Document systems.** The iManage path was written from documentation.
  Testing it against a real instance, or adapters for NetDocuments or
  SharePoint, is welcome; open an issue first.
- **Docs**, especially where they claim something the code does not do.

Less likely to be merged without an issue first: changes to how the model is
called or what it decides (DECISIONS 30), new mail vendors, and anything that
widens what leaves the firm or who receives a reply.

## Conduct

Everyone taking part follows the Code of Conduct (`CODE_OF_CONDUCT.md`).
