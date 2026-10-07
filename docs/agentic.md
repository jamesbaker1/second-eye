# Why this is an agent, and which agent surface

## Why an agent rather than one call

A single review call can only see the document in front of it. That is enough to
catch a placeholder, a broken cross-reference, or a numbering error, and it is
not enough to catch the findings that make this product worth paying for:

- "This indemnity is broader than the firm's standard form."
- "You agreed to a 12-month non-solicit with this counterparty in March."
- "This is version 4. Sections 7 and 9 changed since the draft you sent Tuesday."

Each of those requires going and looking something up, and which thing to look
up depends on what the document turns out to say. That is the definition of a
task you cannot fully specify in advance, which is the test for whether an agent
is warranted. It passes.

The other three tests also pass. The value is high, the task is well within what
Claude does reliably, and errors are recoverable because a lawyer reads every
output before anything reaches a client.

## Which agent surface

There are four ways to build an agent against Claude, and they are easy to
confuse because two of them have similar names.

| Approach | What it gives you | Fit here |
| --- | --- | --- |
| Manual loop | You write the `while stop_reason == "tool_use"` loop | No reason to hand-write it |
| Tool Runner (`client.beta.messages.tool_runner`) | The SDK runs the loop over tools you define. You host. | **What we used until 2026-09-26.** |
| **Managed Agents** | Anthropic runs the loop and hosts a per-session sandbox | **What we use.** DECISIONS 29 |
| Claude Agent SDK | Claude Code as a library: built-in file, bash, grep, web tools | Wrong for this, see below |

**Managed Agents is the fit now.** The owner's instruction was to do it all
through Anthropic as much as possible and build nothing from scratch that the
platform already does. Read against what the review needs, that maps almost
one to one:

| The review needs | The platform gives |
| --- | --- |
| A loop over tools, with the model deciding what to look up | The session: Anthropic runs the loop |
| A persisted, versioned configuration (model, prompt, tools, skills) | The agent object, `agents/*.yaml`, applied by `lra agents apply` |
| The document readable as a file, not only as text | A Files API upload mounted at `/workspace` |
| Code against the file, and our own tested tools beside the model's | The sandbox, with Anthropic's `docx`/`xlsx`/`pdf`/`pptx` skills and ours |
| A cap on time and money | A dollar budget on the session; our deadline as `user.interrupt` |
| The lawyer's preferences and the firm's conventions | Memory stores, mounted read-only |
| Someone checking the work | An outcome with a rubric, graded by a separate model |
| A trace when something goes wrong | The Console session view, one URL per session in the log, while it runs (the session is deleted when it ends) |

What was our code before and is a platform feature now is gone, not kept as a
fallback: the tool-runner loop, the server-tool menu (capabilities), the
container-upload plumbing, the memory block spliced into the system prompt, the
retry logic.

**The Claude Agent SDK is a different product.** It is the Claude Code harness
packaged as a library. It is excellent for coding agents on your own machine.
Here it would mean hosting the loop ourselves again, on a service that
processes privileged client documents, for a set of tools Managed Agents gives
us in a sandbox Anthropic hosts.

**The conflict this creates** is with decision 8, firm-controlled storage. The
document is mounted in Anthropic's container for the life of the session and
retained under the code-execution terms (`docs/sandbox.md`). Decision 28
accepts that for the proof of concept on the owner's own documents, and makes
production security for a firm's matters a decision to be taken before any
firm's matter goes through this path.

## What runs where

```
 email in ──> Cloudflare edge ──> Workflow ──> our container (flow.py)
                                                 │
                        deterministic checks, thread state, memory table
                                                 │
                                    ┌────────────┴─────────────┐
                                    │  Anthropic Managed Agents │
                                    │  session on the reviewer  │
                                    │  /workspace/<doc>  (ro)   │
                                    │  /mnt/memory/*     (ro)   │
                                    │  skills: docx pdf xlsx    │
                                    │          pptx lra-*       │
                                    │  outputs: (redline).docx  │
                                    └────────────┬─────────────┘
                     custom tools answered here: │ report_findings,
                     DMS lookups (lawyer's OAuth │ token stays here),
                     note_for_next_time          │
                                                 ▼
                        redline.verify locally, fallback to local writer,
                        reply composed, email out
```

**Ours**, because Anthropic does not do it: receiving and sending email
(`cloudflare/`, `mail/`); routing (`handler.py`); the deterministic checks,
which also run locally before the session so the agent is told what was found;
thread state, the change ledger and undo (`thread.py`, which keeps the local
writer); the archive and the learning ledger; every DMS call, because the
lawyer's OAuth token does not leave our side; `redline.verify` on every file
that comes back.

**The platform's**: the loop, the sandbox, the skills, the file mounts, the
memory mounts, the budget, the grader, compaction, caching, the trace.

## The tool surface

The reviewer has no custom tools (docs/migration.md, phases 1 and 5): a
custom tool call in a session nobody is attached to would wait for ever. Its
report is `findings.json`, written by `validate_findings.py` in the
lra-document-tools skill; notes for next time are `notes.json`, kept pending
by `memory.accept_agent_notes`; the document system is our MCP server
(`dms_mcp.py`), bound to the email's matter, with an `always_allow` toolset.
Beside these it has the sandbox's own file and bash tools, with the web tools
off unless `WEB_SEARCH_ENABLED`.

The associate still has custom tools, answered by `managed.run_session` over
the event stream: `read_document`, `note_for_next_time` and `make_changes`.
Their schemas and handlers are in one file (`src/lra/tools`,
`pipeline/instruct.py`), so a parameter cannot be renamed in one and not the
other.

## Session discipline

- **An outcome, not a message.** Every review opens with `user.define_outcome`
  and the rubric in `agents/review_rubric.md`, up to three iterations. The
  grader has its own context; it checks that every anchor is verbatim, that a
  clean document got an empty list, that the summary leads with whether it is
  safe to send, that the redline exists and verified. The rubric is a file so
  it can be tuned without a deploy.
- **Money and time.** A dollar cap on the session (`MANAGED_SESSION_BUDGET_CENTS`)
  is enforced by the platform before each model request. The wall-clock budget
  (`AGENT_TIME_BUDGET_SECONDS`, DECISIONS 16) is ours: past it the session is
  interrupted and whatever draft it recorded stands, marked "Partly reviewed".
- **Read back, not streamed.** `review.finish` polls the session until it
  stops, reads why from its last idle, and reads its files through the
  indexing lag; a missing or invalid findings file gets one correction
  message. The associate's sessions still hold the stream open: every
  (re)connect reads the event history as well and dedupes by event id.
- **Refusal is handled.** A session error naming a refusal fails the job
  cleanly rather than sending an empty review.
- **Nothing per document reaches the system prompt.** It lives on the agent
  definition and cannot. The document, the checks and the diff since last time
  ride in the session's first message inside fences with a random tag.
- **Tools fail in sentences.** A tool that cannot reach the DMS returns a plain
  explanation as its result, because that explanation often ends up in the
  email.
