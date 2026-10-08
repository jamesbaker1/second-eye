# The archive

Every message the agent is copied on can be stored, indexed and searched. This
is what turns a document reviewer into something that knows what is going on,
and it is what makes diff-based review possible: you cannot compare a draft
against the version you sent on Tuesday if you did not keep it.

It is also the most sensitive component in the system, and it is **off by
default**.

## Why it is off by default

The archive holds privileged client correspondence. That makes it three things
at once: a discovery target, a breach target, and, depending on jurisdiction and
how the system is structured, a potential question about whether privilege is
preserved when a third-party system holds the communications.

None of that is a reason not to build it. It is a reason to require a decision
from someone with the authority to make it, rather than having the feature
switch itself on because the code shipped. `ARCHIVE_ENABLED` defaults to false
and the store is a no-op until a person changes that.

## Launch blockers

These are requirements, not aspirations. The scaffolding implements the shape of
each; the production versions are outstanding work.

**When they bite.** Every row below exists to protect somebody else's privileged
material. DECISIONS 13 puts the first deployment on Jim's own documents, where
there is none, so the archive can be switched on for that without waiting on
this table. Nothing about starting with one user makes any row here optional
later: a second user, or a live client matter, and all of them stand again. The
two that need the most lead time -- a signed retention policy and firm-held
encryption keys -- are the ones to start before they are needed, not after.

| Requirement | Status |
| --- | --- |
| The firm's general counsel has signed a written retention policy | Not started. Needs a person, not code |
| Encryption at rest with firm-held keys | Partly done. Attachment bytes are sealed with `DATA_KEY` (AES-256-GCM, `blobs.stash`) like every stored document. Not under the key: the archived message rows (subject, body, participants) and the full-text index, which holds the extracted attachment text, because an index over ciphertext finds nothing. Those sit in D1 under Cloudflare's encryption at rest only. The key must also be held by the firm, not only by us |
| Per-lawyer scoping, enforced in code | Done. Every read takes the owner as a required argument |
| An audit log of every read | Done. `access_log` records actor, action and time |
| A deletion path for when a client demands one | Done. `second-eye purge --client`, `--matter` or `--lawyer` (`purge.py`): messages, attachments, search entries, and the scope's read-log detail blanked |
| Retention expiry | Implemented. The daily retention sweep (`retention.py`, from the Worker's cron) runs it, as does every archive write; `ARCHIVE_RETENTION_DAYS` defaults to 7, like everything else about a document, and a firm sets it longer, or 0 for indefinitely, by its own decision |
| Tokens and blobs excluded from backups that leave the tenant | Not started. Deployment concern |

## The two rules enforced in code

**Every read is scoped to one lawyer.** There is no function in `archive.py`
that returns another lawyer's mail. The owner is the first argument of every
read, and it is not optional. This is deliberately more awkward than a filter
parameter, because a filter parameter can be forgotten.

**Every read is logged.** If a matter later becomes contested, the firm can say
exactly what was accessed and when.

## What it enables

- **Compare to the last version.** "Is this different from what I sent Tuesday?"
  answered without a second attachment.
- **Search your own mail.** "What did I send Acme about the indemnity cap?"
- **The tracker.** Matters, counterparties, and what is in flight, derived
  rather than maintained.
- **Counterparty history.** Which positions this opposing party has taken before.

## Design notes

The full-text index is a standalone FTS5 table rather than an external-content
one. External-content tables need triggers to stay in sync with the base table,
and a silently stale search index on privileged material is a worse problem than
the duplicated storage, which is small at per-lawyer scale.

Personal mail domains are never recorded as counterparties. A lawyer emailing
their own Gmail is not a deal.

A redline and its original are recognised as the same document, so
`Acme NDA (redline).docx` finds `Acme NDA.docx` when looking for prior versions.
