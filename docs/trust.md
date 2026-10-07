# Trust: where a document goes, who touches it, and what stops it

For a firm's general counsel and risk committee. The same content is published
as `site/public/trust.html`; the one-minute version is
`site/public/security.html`. It describes this deployment as built; the
settings named in capitals are the switches a firm controls. Where this page
relies on a vendor's own terms it quotes them and links them, and where it
cannot verify something it says so.

This is written against the questions ABA Formal Opinion 512 (July 2024) asks
of a firm using generative AI: confidentiality of client information,
including across clients; informed consent where a tool learns from what it
is given; and supervision of the tool and of the lawyers who use it.

**Nothing kept after 7 days. Nothing learned unless you ask.** What we hold
about a document is deleted 7 days after the conversation's last activity,
and nothing is learned unless a lawyer asks. We delete each review session at
Anthropic as soon as it ends; Anthropic may keep its own copies for up to 30
days. The exceptions are listed below.

## The path of one document

```
  Lawyer (firm mailbox)
     |  1. emails a draft to the firm's review address
     v
  Cloudflare Email + Worker      allowlist of firm domains, DMARC check, daily cap;
     |                           anything else is dropped before our code runs
     |  2. sealed with the firm's key (AES-256-GCM)
     v
  Encrypted storage              Cloudflare R2 (documents) and D1 (rows),
     |                           in the firm's Cloudflare account;
     |                           deleted 7 days after the last activity
     |  3. the one document, for one review session
     v
  Anthropic: Claude Fable 5.1    a Managed Agents session: the document is mounted
  in a sandboxed session         in an isolated container, read-only memory for this
     |                           lawyer and this client only; no email, no DMS token;
     |                           we delete the session when the review ends;
     |                           Anthropic may keep its copies for up to 30 days
     |  4. findings and a tracked-changes file, checked by our code
     v
  Back to the lawyer only        one reply, to the sender; never to anyone on CC,
                                 never to the counterparty
```

Nothing leaves the firm except to Cloudflare (which carries and stores it) and
Anthropic (which reads it in a review session and may keep its copies for up
to 30 days; see below). The agent replies only to the lawyer who asked.

## Who processes what

| Processor | What it receives | What it does | Terms that govern it |
| --- | --- | --- | --- |
| **Cloudflare** | The email and its attachments; the rows that describe a job | Receives and sends mail, runs our code in a container, stores documents (R2) and rows (D1) | The firm's (or our) Cloudflare agreement. Documents are encrypted with the firm's key before Cloudflare stores them, on top of Cloudflare's own encryption at rest |
| **Anthropic** | The document under review, the email's instructions, this lawyer's style memory and this client's and matter's memory | Runs Claude Fable 5.1 in a Managed Agents session and returns findings and a file | Anthropic's Commercial Terms of Service (below) |
| **The firm's DMS** (optional) | Searches and reads, as the lawyer | Lets a review compare against earlier drafts on the same matter | The firm's own DMS; our code calls it with the lawyer's own OAuth token, never Anthropic's container |

**Training.** Anthropic's Commercial Terms say, in section B: "Anthropic may
not train models on Customer Content from Services." API use under those terms
is not used to train models by default.
Source: <https://www.anthropic.com/legal/commercial-terms> (read 2026-09-28).

**Anthropic's retention.** Anthropic's privacy centre says: "For Anthropic API
users, we automatically delete inputs and outputs on our backend within 30
days of receipt or generation", with exceptions it lists (for example, where
the customer has agreed otherwise, such as a zero data retention agreement).
Source: <https://privacy.claude.com/en/articles/7996866-how-long-do-you-store-my-organization-s-data>
(read 2026-09-28).

- **Claude Fable 5.1**, the default, requires that 30-day retention.
- **`ZERO_RETENTION=true`** is for a firm whose Anthropic organisation is on
  zero data retention. It puts every model call and every agent on Claude
  Opus 5, a model available under ZDR, at some cost in judgment.
- **The sandbox is not zero-retention either way.** Managed Agents, where
  every review runs, is not eligible for zero data retention, whatever the
  model. We delete each session as soon as the review ends
  (`managed._clean_up`); Anthropic's documentation says a delete permanently
  removes the session's record, its events and its sandbox. It does not say
  whether anything of a deleted session stays on its backend within the 30
  days above, so up to 30 days is what we can promise. Memory stores (short
  sentences a lawyer asked it to keep, not documents) stay until deleted. A
  client that needs nothing retained anywhere belongs in `NO_AI_MATTERS`,
  which sends its documents to no model at all. Details: `docs/sandbox.md`.

**Certifications.** This page makes no certification claims for either vendor.
Check Cloudflare's and Anthropic's current certifications (SOC 2, ISO 27001
and the like) on their own trust portals at the time you decide. We hold none
of our own (below).

## What we keep, and for how long

A daily sweep (`retention.py`, run by the Worker's cron) deletes each item
within a day of its window ending. The firm can shorten any window; `0`
keeps for ever.

| What | Kept for | Setting |
| --- | --- | --- |
| The conversation and its document, earlier versions, findings, tracked changes and negotiation record | 7 days after the last reply or new version. Undo, clean copy and answers work until then | `THREAD_RETENTION_DAYS` |
| A closing and its signed pages | 7 days after the last email on it | `THREAD_RETENTION_DAYS` |
| A note the agent proposed that the lawyer did not confirm | 7 days, never used | `THREAD_RETENTION_DAYS` |
| At Anthropic: our uploads and the files a session writes | Deleted when the session ends | none |
| At Anthropic: the session and its turn-by-turn record | Deleted when the review ends; a delete that failed is retried once the job is 7 days old | `THREAD_RETENTION_DAYS` |
| The raw incoming message | Until answered; 7 days if never | R2 lifecycle rule |
| The job row (sender, status) | 24 hours | `RETENTION_HOURS` |
| Which changes were kept, undone or dismissed | Not recorded | `LEARN_FROM_OUTCOMES` (off) |
| The mail archive | Off; 7 days if switched on | `ARCHIVE_ENABLED`, `ARCHIVE_RETENTION_DAYS` |
| What a lawyer asked it to remember (our rows and Anthropic's memory stores) | Until the lawyer or a purge removes it | none |
| Mail held by the kill switch, sealed | Until the switch is turned off, then reviewed | `SERVICE_PAUSED` |
| Delivery bookkeeping (ids only: no names, addresses or contents) | 31 days | none |
| The audit trail (names, times, ids; no contents) and purge records | Until the firm deletes them; a purge blanks its scope's names and ids | none |

**Deleting a client, a matter or a lawyer.** `lra purge --client 10234`,
`--matter 10234-0007` or `--lawyer jane@firm.com` deletes everything held for
that scope, ours and Anthropic's, at once rather than after 7 days:

- conversations and their documents (the rows, and the objects in R2), every
  earlier version kept for a blackline, and the negotiation ledger;
- closings and their signed pages;
- archived mail, its attachments and its search-index entries;
- remembered notes and suppressions, and any record of accepted, undone and
  dismissed suggestions;
- at Anthropic: each review session that worked on the scope, with every
  file it wrote; every file uploaded for one; and the scope's memory stores.
  A store shared with other work (the client's, when one of its matters is
  purged) is rewritten without what was deleted;
- for a lawyer, also their personal notes and memory store, and any
  document-system access (the token, or the credential in their Anthropic
  vault).

It is a dry run until `--apply` is given: it lists how many of each it would
delete and every Anthropic id it would touch. If Anthropic cannot be reached
part way, what failed is recorded and the same command run again finishes it.

**The audit trail is kept, and blanked.** It is the firm's record of
supervising the tool, so a purge does not delete it: who used it, when, what
ran, on which model, where the reply went and how it ended stay. What names
the client's work goes from each of the scope's rows: the documents' and
returned files' names, the matter, the client, the session and file ids, and
the triage plan. Each purge also leaves its own record, with no contents:
who ran it, the scope, how many of each kind, and when (`lra purge --history`).

What it cannot reach, said plainly:

- a client or matter purge finds rows by the matter or client recorded on
  them. From 4 October 2026 the client is recorded as each review resolves
  it; earlier conversations, closings and archived mail with no matter number
  cannot be attributed to a client. The dry run counts them; purging the
  lawyers who sent them reaches them;
- what Anthropic keeps after a delete, under its own retention (up to 30
  days, see above): deleting the session is the request we can make;
- the firm's own settings: the client register (`lra clients`), the no-AI
  list, the playbook and firm memory, and a lawyer's personal style notes on
  a client purge (they hold no client content, see below).

`lra purge` with no scope runs the daily sweep by hand.
Details: `docs/deploy-cloudflare.md`.

## Encryption

- Documents (every stored copy: the one under review, earlier versions,
  signed pages, held mail, a review's state between steps) and DMS tokens are
  encrypted with AES-256-GCM under a key the firm generates (`lra keygen`)
  and holds. Withdrawing the key turns everything stored into noise. The
  service refuses to start on hosted storage without one.
- Not under the firm's key, and said plainly: the rows in D1 (who sent what
  and when, filenames, findings and the text of each tracked change,
  remembered notes, the audit trail), and the archive's search index if the
  archive is on. Only Cloudflare's own encryption at rest covers those; check
  its current documentation for what that is.
- Everything travels over HTTPS between Cloudflare and Anthropic. Between our
  container and the Cloudflare Worker that holds the storage and mail
  bindings, nothing crosses the internet at all.

## Access

- **Who can send to it.** An allowlist of the firm's own domains or
  addresses (`ALLOWED_SENDERS`), enforced at the mail edge. Each entry is an
  exact address or an exact domain: `firm.com` does not admit
  `london.firm.com`, so every domain the firm sends from is listed. Mail from
  anyone else is dropped silently before any of our code runs: no bounce, no
  reply, so opposing counsel who reply-all learn nothing. An empty list
  drops everything. A message is admitted only if its sender's domain passed
  DMARC on Cloudflare's own check; one that failed, or whose domain publishes
  no DMARC record, is dropped the same way, so a forged partner address gets
  nothing. A firm domain without a DMARC record can be listed in
  `DKIM_ONLY_DOMAINS` until it has one, and is then admitted on a DKIM
  signature for exactly that domain.
- **What our code can reach.** The container that runs our code has no
  internet of its own (`CONTAINER_EGRESS=allowlist`). It reaches Anthropic's
  API, the firm's document system's sign-in endpoint if one is connected, and
  the firm's own storage and mail through Cloudflare, which refuses anything
  else before it leaves. The endpoints our code uses for the database,
  documents and sending exist only inside Cloudflare: on the public internet
  they do not exist.
- **What a review can reach.** The document it was sent, and, if the firm
  connects its DMS, only documents on the same matter, read with the lawyer's
  own permissions. The DMS token never enters Anthropic's container.
- **Who gets the answer.** The sender, and only the sender
  (`REPLY_POLICY=sender_only`, the default), enforced where every message
  leaves, whatever was composed. Nobody on CC, and never anyone outside the
  firm. Every message carries the firm's privilege legend
  (`PRIVILEGE_NOTICE`) and an `X-Privileged` header for its mail rules.
- **Clients whose guidelines exclude AI.** Listed in `NO_AI_MATTERS` (matter
  ids, client names or mail domains), or added by a playbook admin emailing
  "no AI for Acme". Their documents get the deterministic checks and tools
  only, with no model call, and the reply says so. List the client's domain or
  matter to cover its scans, whose parties can only be read by a model.

## Human in the loop

The agent returns tracked changes and comments in Word. It never accepts a
change, never sends a document to a client, and never files anything. A lawyer
reads every change and decides. It is told not to advise on the transaction.

## Memory: off until asked, and walled by client

By default it learns nothing a lawyer did not ask it to. A note the agent
proposes is used only once the lawyer replies "remember that", and is deleted
after 7 days if they do not. Undos, dismissals and the version the lawyer sent
teach it nothing unless the firm turns that on (`LEARN_FROM_OUTCOMES`). What
a lawyer does ask it to keep ("remember that", "stop flagging X") makes it a
self-learning tool in Opinion 512's sense, and the firm should tell clients
so. What it keeps is walled:

- Anything learned from a client's documents is kept under that client (or
  that matter) and read only on that client's matters.
- A lawyer's personal memory holds drafting style only ("'will', not
  'shall'"), and code refuses any personal note that names a party, a client,
  a sum or a proper name.
- The firm-wide layer is written only from the firm's own approved playbook,
  never from a client's document.
- A client the system cannot identify with certainty gets no cross-matter
  memory at all.

Details and tests: `docs/memory.md`, `tests/test_memory_walls.py`,
`tests/test_retention.py`.

## Supervision: the audit trail

Every job leaves one row with no contents: received at, sender, matter,
document names, what ran, the model and session ids, inference geography,
where it was stored and until when, who the reply went to, when, and how it
ended.

- `lra audit --since 2026-09-01 [--lawyer x] [--matter y] --format csv|json`
- A playbook admin emails "audit report for September" and gets the CSV back,
  to them alone.

A session can be read turn by turn in the Anthropic Console while it runs.
It is deleted when the review ends, so the audit row is the lasting record.

## Kill switch

- `SERVICE_PAUSED=true` stops everything at once: mail is accepted and held
  sealed (never bounced), nothing is reviewed and nothing is sent. Switched
  back, the held mail goes through. The daily deletion keeps running.
- A lawyer can reply "revoke" to disconnect their document-system access.
- Withdrawing `DATA_KEY` makes everything stored unreadable.

## What we don't have yet

No SOC 2 or ISO 27001 certification and no independent penetration test. The
D1 rows are not under the firm's key. This is a pilot run by one person, not
a security team.

## Before the pilot starts

Confirm each setting named here is as the firm wants it in the deployment
itself (`docs/deploy-cloudflare.md` says where each is set), and register the
firm's clients (`lra clients add`) so memory can be walled by them.
