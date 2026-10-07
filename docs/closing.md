# Closings by email

Litera Transact's job, done by email. The lawyer writes to the agent the way
they would write to a junior running the closing.

| The lawyer sends | The agent sends back |
| --- | --- |
| "Sig packets for the closing", the SPA and the disclosure letter attached | `0 of 4 in. Waiting on: Acme's director — SPA p.42, Disclosure Letter p.9; Beta Trading's director — SPA p.42, Disclosure Letter p.9.` One `Signature packet - <signatory>.pdf` each (a cover sheet saying what to sign and how to return it, then each page headed "<Document> — signature page" and stamped with the version reference and page id), and the execution copies |
| Signed pages, as a scanned PDF or phone photos, on the closing's thread or forwarded ("Fwd: signed pages from Beta's counsel") | `3 of 4 in. Waiting on: Beta Trading's director — Disclosure Letter p.9.` then what was filed and what was not: "Not filed: Disclosure Letter p.9 for Beta Trading is unsigned." |
| The last page | `All 4 in. Executed set compiled: 2 documents and the closing index.` with each `<document> (executed).pdf` and `<closing> - closing index.pdf` |
| "Compile the executed set" | The same, on request |
| "Closing checklist" | The conditions precedent, deliverables and signing items from the SPA, each with its clause, as `Closing checklist.pdf`; afterwards every reply carries `Checklist: n of m done.` and signing items tick themselves off as pages arrive |
| "Status" or "what's outstanding" | The tally, from the stored state, with no session |

## Who decides what

The closing agent (`agents/closing.agent.yaml`) decides everything a person
would decide by reading (DECISIONS 30): what the email asks, who the
signatories are, which page each scan is, whether it is signed, whether it
was signed on the current version, what the checklist holds, and the reply's
words after the tally. The skill's scripts (`skills/lra-closing/scripts/`) do
the parts that must be exact: `survey.py` reads each document for signing
through the signature pack (`pipeline/sigpack.py`), `packets.py` builds the
packets and execution copies, `receive.py` turns one page of a scan or a
photo into a one-page PDF and reads its stamp, `compile.py` substitutes the
signed pages and renders the index, `checklist.py` renders the checklist.

## What the host keeps and checks

`src/lra/closing.py` keeps one `closings` row per thread, its state as JSON,
and the files a closing holds (execution copies, signed pages, the executed
set) sealed in object storage. Each email is one session given the state as
`/workspace/closing-state.json` and the held files beside the new
attachments. The session answers with `closing.json` in its outputs: the
whole new state, the reply's lines and the files to attach.

It is stored only if it passes `pipeline/closing_state.py`, the validator the
skill's `validate_state.py` also runs in the sandbox: a received page is
marked signed, carries the current version's reference and comes with the
file it was filed as; a page already received does not go back to waiting
unless its document is a new version; the executed set does not exist while a
page is outstanding. The host then checks the files themselves: a newly
filed page is a one-page PDF this session wrote, with no scripts or embedded
files, and every attachment passes the same vetting as the associate's. The
tally that opens every reply is counted from the stored state. An update that
fails any of this changes nothing, and the lawyer is told so.

## Who may file pages

Only the closing's owner. Strangers are dropped by the allowlist before this
code runs. A colleague who is allowlisted but does not own the closing is
told to send the pages to the owner, and nothing is filed. A forward that is
not on the closing's thread is matched to the owner's open closing when it
says it carries signed pages and attaches a scan or photo; with two open
closings and neither named, the agent asks which.

## Setting it up

```
lra skills sync lra-closing      # prints SANDBOX_CLOSING_SKILL_ID
lra agents apply                 # prints MANAGED_CLOSING_AGENT_ID
```

The closing skill is attached to the closing agent only (`extra_skills` in
its manifest). Without `MANAGED_CLOSING_AGENT_ID`, a closing email is told
closings are not set up and nothing else changes.

## Not yet known

Everything here has run against stubs, with the scripts executed from the
built bundle as the container would run them, and never against the live
API. A live run settles: whether the container has LibreOffice (with it,
pages carry the firm's styles and the tally gives page numbers; without it,
pages are drawn from the words and say so); how well the model reads a
phone photo of a page, and a signature on it; whether outputs with an
apostrophe in their names survive the Files API; and how long a session
with a dozen scans takes.
