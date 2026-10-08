# The sandbox

Every Managed Agents session gets a container. The review agent and the
associate agent run their tools in it: the file tools, bash, Anthropic's
prebuilt `docx`, `pptx`, `xlsx` and `pdf` skills, and this product's own
skills. The lawyer's document is mounted there, read-only, and what the agent
makes comes back through the session's outputs directory. This page is about
what that means for a firm's documents and for the tested code this product
is built on.

## The retention position

The sandbox is **not eligible for zero data retention**, and container data,
including mounted files and anything written to the outputs, is **retained for
up to thirty days**.

What we control is our side of it: every session is deleted, with its
events, its sandbox and the files it wrote, as soon as the review ends
(`managed._clean_up`; a failed delete is retried by the daily sweep in
`retention.py`). Until 2026-10-04 sessions were archived instead, which kept
their record indefinitely. Anthropic's documentation says a delete removes the
session permanently; it does not say whether anything of it stays on its
backend inside the thirty days, so thirty days is the figure we give.

Jim accepted that trade (`DECISIONS.md` #25) and widened it in #28: the whole
review runs there, not only derived files. It is his to make: the first
deployment is his own documents, not a firm's. Before this runs on a firm's
matters it is a question for their general counsel, not a setting to inherit.
Production security for a firm's documents is an open decision, to be made
before any firm's matter goes through this path.

Earlier drafts of this document said "get a ZDR agreement in place before
enabling it". That was wrong, and worth correcting rather than deleting: there
is no ZDR agreement that covers the container. The decision is to accept the
retention, not to eliminate it.

### ZERO_RETENTION, and what it does not do

Claude Fable 5.1, the model everything runs on by default, is a Covered
Model: it requires 30-day data retention, and an Anthropic organisation on
zero data retention gets a 400 on every request. `ZERO_RETENTION=true` is for
a firm whose organisation is ZDR. It puts every direct model call (a scan's
transcription, a comparison's assessment) and every agent definition, when
`second-eye agents apply` next runs, on Claude Opus 5 (`config.ZDR_MODEL`), the most
capable model documented as available under ZDR. Claude Opus 5.5 is not
assumed: its launch documentation says nothing about ZDR.

The trade-off, stated plainly for a general counsel:

- **Gained:** the model requests themselves fall under the organisation's
  zero-retention terms, and the service works at all for a ZDR organisation.
- **Lost:** Fable 5.1's judgment, which is the highest tier; and the
  server-side refusal fallback, which Opus 5 does not have, so a classifier's
  false positive ends that call (the review then sends what the checks found).
- **Unchanged:** this page's first paragraph. A Managed Agents session's
  container, the files mounted and written there, and the memory stores are
  retained as described above whatever the model. ZERO_RETENTION makes the
  model ZDR; it does not make the sandbox ZDR. A firm that needs nothing
  retained anywhere should also list its sensitive clients in
  `NO_AI_MATTERS`, which sends their documents nowhere.

## What goes in

| Session | Mounted | Why |
| --- | --- | --- |
| Review | The Word copy the tracked changes go into, and the file as it arrived when that differs (a PDF beside the Word copy built from it) | So the agent can read the layout, check arithmetic with code, and run the writer on the right file |
| Instruction | The document as the conversation currently holds it | So a derived file is built from the real file, not retyped text |
| Repair, conversion | The one file | The job |

Memory stores are mounted read-only under `/mnt/memory` (`docs/memory.md`).
The lawyer's OAuth token is never in the container: every DMS lookup is a
custom tool answered by our process, and those tools reach only the matter the
email belongs to (`tools.build_tools`). The lawyer's token could open every
client's files; a review of one client's draft, acting on text the other side
wrote, gets that client's matter and nothing else, and with no matter
identified it gets nothing.

What can leave the container is also decided here. The environment has
`limited` networking with package managers allowed, no allowed hosts and no
MCP servers (`managed.NETWORKING`); `second-eye agents apply` creates it that way and
updates an existing environment whose networking differs, rather than only
reading it. A skill can `pip install` what it needs, and a model running code
against a privileged document cannot reach any other host. Package managers
stay on because our skill's scripts import python-docx and lxml, which the
container is not guaranteed to have, and Anthropic's document skills install
what they use at run time. The residual risk is accepted: the agents run their
sandbox tools with `always_allow`, and a registry lookup names a package, so
a determined injection could put a few bytes in a registry's access log. It
cannot reach a host the attacker controls.
Web search and web fetch are not governed by this, because they run on
Anthropic's servers rather than in the container; they are off unless
`WEB_SEARCH_ENABLED` says otherwise, and restricted on the agent definition.

## Reading is still done locally too

Every library the container has for reading documents is a pip install away,
and the deterministic checks run on our side before the session starts, so the
agent is told what was found rather than asked to find it. Decks and workbooks
are read in-process with python-pptx and openpyxl; legacy `.doc` and `.rtf` are
converted by LibreOffice as a local subprocess where it is installed
(`convert.py`), and by the associate agent in its sandbox where it is not.

| Format | How it is handled |
| --- | --- |
| `.docx` | In-process. python-docx and lxml. Mounted for the agent as well |
| `.pdf` | In-process with pypdfium2; mounted so the agent sees the pages |
| `.pptx` | In-process. python-pptx |
| `.xlsx` | In-process. openpyxl |
| Legacy `.doc`, `.rtf` | LibreOffice locally; the associate's sandbox as the fallback |

## Our own code, as a skill

Anthropic's skills teach the container to make a good `.docx` or `.xlsx`. They
do not know how to compare two contracts or write a tracked change that Word
will accept, and a model asked to will write its own version: plausible,
different on every run, and with none of the refusals or the proofs that
`compare.py` and `redline.py` have. A custom Agent Skill closes that gap by
putting the tested code in the container and telling the model to call it.

`skills/lra-document-tools` holds `SKILL.md`, four thin scripts and a stand-in
for pydantic. `second-eye skills sync` copies the real modules in beside them
(`skillsync.BUNDLED`), uploads the folder with `client.skills.create`, and
prints the id to set as `SANDBOX_SKILL_ID`; `second-eye agents apply` then attaches it
to both agents beside Anthropic's four. Run the sync again and it uploads a new
version; because the agents ask for `latest`, the next session uses it. What
runs in the container is the source tree at the moment of the sync, never a
fork, and `tests/test_skillsync.py` executes the built bundle in a fresh
interpreter, with and without pydantic, to prove it stands on its own.

| Script | What it does |
| --- | --- |
| `check.py` | The deterministic checks. The agent is handed these already, so it is told not to re-run it |
| `compare.py` | Two versions into tracked changes, proved by accept-all and reject-all |
| `clean.py` | A metadata-free clean copy, proved against an independent reading |
| `redline.py` | The review's findings into tracked changes: `auto_apply` ones written, questions as Word comments, ambiguous anchors refused, output verified |

## The writer, in the container and out of it

The tracked-changes writer is the load-bearing component: if it damages a
client contract nothing else matters. It used to be the one thing kept out of
the container for that reason. Decision 28 moves it in, as `redline.py` above,
and keeps the reason intact by keeping the proof on our side:

1. The review agent runs `redline.py` on the working copy once its report is
   accepted, and the file lands in `/mnt/session/outputs/`.
2. The handler downloads the session's outputs and runs `redline.verify` on the
   file, expecting revisions. A file that does not verify is not sent.
3. The handler also runs the local writer on the same findings, regardless. Its
   record of what landed, what was skipped and why is the manifest the email is
   built from, because a manifest must come from code we ran, not the model's
   account of a script it ran.
4. The session's file is attached only if it verifies, carries no revisions by
   an author we did not expect, and reads the same as the local writer's
   output after accepting everything. Otherwise the local writer's file is
   attached. A lawyer never gets fewer tracked changes than the local writer
   makes.

`thread.rebuild`, which is how undo and every later round regenerate the
document from the original, uses the local writer only. A test asserts the
writer's modules never import the platform, so this cannot drift.

## Derived files: where the sandbox simply wins

"Give me a table of every payment date", "pull the obligations into a
schedule", "how many times does this say reasonable efforts": each asks for
something derived from the document, not a change to it. There is no anchor,
no ledger entry and nothing to undo, and writing bespoke code per request is
exactly what the container is good at. The associate writes the file to the
outputs and it comes back as an attachment beside the redline.

The boundary is stated in the associate's prompt in terms, and tested: code
produces new files only and never edits the document under review. Every
change to that goes through `make_changes`, the local writer and the ledger,
which is what makes an edit both careful and reversible. A `(redline).docx`
the associate leaves behind is not attached, whatever it holds.

## Formatting repair: the one place the container edits a copy

Formatting repair is the deliberate exception to "new files only", for a
reason and under a condition. The reason is that it is the wrong shape for the
writer: no anchor to refuse on, no single right answer, bespoke per document.
The condition is that the result is not trusted. `repair.gate` runs locally and
the repaired copy is sent only if it opens, every word in every story is
identical and in the same order, no tracked change or comment was added or
removed, and the deterministic checks find nothing they did not find before.
`tests/test_repair.py` plays a session that changes one word, reorders two
paragraphs, or quietly accepts opposing counsel's tracked change, and each is
stopped. Automatic numbering is the one thing that gate cannot see, so the
instruction forbids touching it and the reply tells the lawyer to look.

## Files travel through the Files API

The document goes up with `client.beta.files.upload` and is attached to the
session as a `file` resource with a mount path under `/workspace`; the session
takes its own copy, so the upload is deleted once the session is created.
Outputs come back with `client.beta.files.list(scope_id=session.id)`, which
needs the Managed Agents beta header beside the Files one, and
`files.download`. There is a short indexing lag after the session goes idle,
so an empty listing is retried before it is believed.
