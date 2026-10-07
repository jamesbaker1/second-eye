# Redline Desk

A legal review agent that lives as an email contact.

You are about to send a document to a client. Before you hit send, you email it
to `review@<your firm's domain>` instead. A minute later you get a reply: a
verdict you can read in the preview pane, a list of what is wrong, and the same
document back with real Word tracked changes you can accept or reject.

No portal. No login. No plugin. No new app. The interface is the address book.

> **Not legal advice.** Redline Desk is software that checks documents. It does
> not give legal advice, and using it creates no lawyer–client relationship with
> anyone. Its output can be wrong, incomplete or missing things a careful lawyer
> would catch. The lawyer who sends a document remains responsible for every
> word of it, for every change they accept, and for checking everything the
> agent says. The software is provided without warranty (sections 15 and 16 of
> the licence).

## What it does

A lawyer forwards, CCs or BCCs a draft to one address the whole firm shares.
The agent works out which file is the document and what is being asked, runs
a deterministic pass (placeholders, defined terms, cross-references,
numbering, amounts, dates, party names, leftover tracked changes and metadata,
and the covering email against the attachment), then has the model review it
with the firm's playbook and the lawyer's own drafting preferences. It replies
to the sender alone. Replies in the thread answer its questions, undo its
changes ("undo 2"), or ask for more: a clean copy, a comparison with the last
version, renumbering, signature pages, a deadlines calendar.

An example, with only the mechanical pass running:

> **From:** a.lawyer@firm.example
> **To:** review@firm.example
> **Subject:** NDA for Acme - sending this to the client in an hour
>
> Quick look before I send this out? Care most about the defined terms.
> 📎 Acme NDA.docx

> **From:** Redline Desk
> **Subject:** Re: NDA for Acme - sending this to the client in an hour
>
> Don't send yet: 2 things to fix first.
>
> Fix before sending:
> - Unfilled square-bracket placeholder: [COUNTERPARTY NAME]. What should [COUNTERPARTY NAME] be?
> - Section 2: 'March 5, 2026' contradicts the date first written above. Is the effective date March 3, 2026 or March 5, 2026?
>
> Quick questions:
> - Section 4: Section 7 does not exist. Which did you mean? [4 or 2 or 1]
>
> 2 minor points: numbering jumps from 2 to 4; author metadata identifies python-docx.
>
> I could not complete the full review, so this covers only the mechanical
> checks. Treat it as incomplete: nothing here speaks to the substance of the
> document.

That is a real reply from `lra replay samples/example.eml` with no model
configured, lightly shortened. With the model, the review adds the
substantive points and returns `Acme NDA (redline).docx` with the changes it
can articulate tracked and a reason for each in the margin. The lawyer
answers its questions in a reply and gets the document back with the
answers made, or replies "clean copy" for a client-ready file.

## Status: honest

This is early software, run in production by one person on their own
documents. Read this before relying on any of it.

- **Everything that does not need the model runs in production.** Clean
  copies, renumbering with cross-references repaired, signature pages, the
  deadlines calendar, answers and undo in a thread, and the mechanical half
  of a review have been sent from a real mailbox through the Cloudflare
  deployment and checked in the inbox.
- **Everything that needs the model is built and tested, but has not yet run
  live on this code.** The substantive review, issues lists on the other
  side's paper, the playbook, turning comments, closings, blacklines and
  model triage are tested against fakes of the Anthropic platform that are
  checked against the SDK's own types. The last live run was on an earlier
  architecture and found six bugs no test had. Expect the same again.
- **Nothing it produces has been opened in real Microsoft Word.** Every file
  is checked structurally and round-tripped through LibreOffice in CI, which
  is an independent parser but not Word. `python -m evals.word_pack` writes
  the files and a checklist for whoever does it first.
- **The document-system integration (iManage, through an MCP server) was
  written from documentation** and has never met a real instance.
- **Tests:** 2,294 Python tests, run against local SQLite and again through
  the Cloudflare D1 adapter; 80 Worker tests and 20 for the document-system
  server, in workerd. Without LibreOffice installed, 10 skip: the
  round-trips through an independent word processor, and one that runs only
  through the D1 adapter. CI installs LibreOffice.
- **Evals:** on a synthetic corpus, the deterministic checks catch 27 of 27
  planted defects with no false positives across 15 clean documents, and 29
  of 29 outbound-message defects with none across 28 clean messages
  (`python -m evals.run_checks`). The corpus tests only defects someone
  thought to plant.

`STATUS.md` is the detailed position and is kept current; `DECISIONS.md`
records what is settled and why.

## How it works

```
 lawyer's mailbox
     |  SMTP
     v
 Cloudflare Worker        drops anything not from an allowlisted sender that
     |                    passes DMARC; seals the message with the firm's key
     v
 Cloudflare Workflow      one per message, retried step by step
     |
     v
 Python container         intake, deterministic checks, thread state;
     |                    no internet except Anthropic's API
     v
 Anthropic Managed Agents one session per review, the document mounted in a
     |                    sandbox, our tools there as a skill; deleted when done
     v
 Python container         verifies every returned file before attaching it
     |
     v
 Worker outbox            one reply, to the sender only
```

- **Deterministic code is the evidence; the model decides.** The checks run
  first, free and exact, and their output goes to the model, which decides
  what the email asks, which findings are real and what the reply says
  (`DECISIONS.md`, 30). Firm policy it cannot override: replies go only to
  the sender, clients excluded from AI get no model call, memory is walled
  by client (31).
- **Tracked changes are written directly as Word revision markup**, never by
  diffing, and nothing is attached unless it passes verification
  (`docs/redlining.md`, `docs/verification.md`).
- **The document system is reached with the requesting lawyer's own OAuth
  token**, so the agent sees exactly the matters that lawyer can open
  (`docs/oauth.md`).
- **One deployment per firm**, in our Cloudflare account or the firm's own.

`docs/it/architecture.md` has every hop, store, key and trust boundary.

## Your data

In short (`docs/trust.md` has the detail and the sources):

- Documents are encrypted with AES-256-GCM under a key the firm generates
  and holds, before Cloudflare stores them. The database rows (who sent
  what, findings, the text of each tracked change) are not under that key,
  only Cloudflare's own encryption.
- Nothing is kept more than 7 days after a conversation's last activity by
  default, and nothing is learned unless a lawyer asks it to remember.
- Each review runs in an Anthropic Managed Agents session that is deleted
  when the review ends. Anthropic's terms say it does not train on this
  content; it may keep its own copies for up to 30 days, and Managed Agents
  is not eligible for zero data retention.
- `lra purge --client/--matter/--lawyer` deletes everything held for that
  scope, ours and Anthropic's. `lra audit` exports who used it and how.
- No SOC 2, ISO 27001 or independent penetration test of Redline Desk
  itself.

For a firm's IT and security review: `docs/it/README.md`.

## Run it locally

You need Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv venv && uv pip install -e ".[dev]"
cp .env.example .env
.venv/bin/lra replay samples/example.eml    # prints the reply it would send
```

Nothing is sent: the console mail provider prints the reply. Without an
Anthropic key and agent ids in `.env` it runs the mechanical pass only and
says so, as in the example above. Call the venv directly rather than through
`uv run`, which re-syncs the environment without the `dev` extra.

Several things it does by email also run from the command line with no model
and no network:

```bash
.venv/bin/lra compare earlier.docx later.docx   # writes "later (comparison).docx"
.venv/bin/lra clean draft.docx                  # writes "draft (clean).docx"
.venv/bin/python -m evals.word_pack             # files to open in real Word, with a checklist
```

With an Anthropic organisation set up, `lra agents apply` creates the agent
definitions and `lra live-check --dry-run` shows the steps of a first live
run and its budget before anything is spent.

## Deploy it

Redline Desk runs on Cloudflare end to end: Email Routing and the Email
Service, a Worker, Workflows, a container, D1 and R2, with Anthropic for the
model.

- `docs/deploy-cloudflare.md`: the deployment guide, one deployment per firm,
  and the settings a firm controls.
- `docs/it/self-hosted.md`: for a firm that runs it in its own Cloudflare
  account and Anthropic organisation, from a tagged release, with no standing
  access for anyone else.

The same `Dockerfile` builds an ordinary container image, and the Python
service also runs behind a mail provider's inbound webhook
(`.venv/bin/uvicorn lra.main:app`), though the Cloudflare path is the one in
production.

## Where things are

| Path | What it is |
| --- | --- |
| `src/lra/` | The Python application: intake, checks, the review, redlines, threads, memory, retention, the CLI |
| `src/lra/pipeline/` | The document pipeline: extraction, deterministic checks, comparison, clean copies, the OOXML revision writer |
| `cloudflare/` | The Worker, the review Workflow and the outbox; `cloudflare/dms-mcp/` is the document-system MCP server |
| `agents/` | The Managed Agents definitions and the rubric each review is graded against |
| `skills/` | Our tools, packaged as Agent Skills to run in Anthropic's sandbox |
| `evals/` | The synthetic corpus and the scorers |
| `docs/` | Design notes, the trust page, deployment, and `docs/it/` for a firm's IT |

Background, for anyone who wants the reasoning: `PRODUCT.md` (the thesis: code
review for legal documents), `ROADMAP.md`, `DECISIONS.md`, `docs/agentic.md`,
`docs/memory.md`, `docs/litera-parity.md` and `docs/redlining.md`.

## Licence

Redline Desk is free software under the **GNU Affero General Public License,
version 3 only** (`AGPL-3.0-only`; the text is in `LICENSE`). A firm can run
it, unmodified or modified, for its own lawyers; if you modify it and others
use it over a network, you must offer them your source.

A **commercial licence** is available for firms whose policy does not allow
the AGPL, or who want to embed or host it on other terms. There is also a paid
hosted service and support. `LICENSING.md` explains all of this in plain
English, including what the AGPL means for a firm and the use of the name.

## Contributing

Contributions are welcome: Word compatibility reports, mail-client reply
formats, checks, false positives, document-system adapters and docs.
`CONTRIBUTING.md` has the setup, the tests in both storage modes and the
style. Contributors sign a contributor licence agreement (`CLA.md`) once,
by a comment on their first pull request, so contributions can be included
in both the open source and commercial editions; you keep your copyright,
and your code always stays open source (`LICENSING.md`, "Why is there a
CLA?").

Please never put a real client document in an issue, a pull request or a
test. Security problems go to private reporting (`SECURITY.md`). Everyone
taking part follows the `CODE_OF_CONDUCT.md`.
