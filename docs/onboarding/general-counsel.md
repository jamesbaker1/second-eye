# A note for the firm's general counsel

For the general counsel or risk partner deciding whether the firm's lawyers
may send client documents to Second Eye during a pilot. It describes the
system as built on 2026-10-04. The longer version, with sources, is
`docs/trust.md`. Settings in capitals are switches the firm controls.

## What it does

Second Eye is an email address. A lawyer forwards or copies it on a draft.
It replies to that lawyer with a one-line verdict, the points it found, and
the same document with Word tracked changes. The lawyer can reply to answer
its questions, undo its changes, or ask for a clean copy or signature pages.

It never sends anything to a client or to the other side. It never accepts
a change, files a document or signs anything. A lawyer reads every change and
decides. It is told not to advise on the transaction.

## Where a document goes

1. **Cloudflare** receives the email, stores the document and runs our code.
   Documents are encrypted with a key the firm generates and holds
   (AES-256-GCM) before Cloudflare stores them. Without that key, what is
   stored cannot be read.
2. **Anthropic** reads the document in a review session: the document is
   placed in an isolated container in which the model, Claude, works. It has
   no access to email and no access to the firm's document system.
3. The reply goes back to **the lawyer who sent it, and nobody else**.

Nothing goes anywhere else. If the firm connects its document system, a
review may also read earlier drafts on the same matter from it, as the
lawyer and with the lawyer's own permissions; that connection is optional
and off unless the firm asks for it.

## How long things are kept

**Nothing kept after 7 days. Nothing learned unless you ask.**

| What | Where | How long |
| --- | --- | --- |
| The incoming email | Cloudflare | Deleted once reviewed; 7 days if it never is |
| The conversation and its document, so replies work | Cloudflare | 7 days after the last reply |
| A closing and its signed pages | Cloudflare | 7 days after the last email on it |
| A record of each job (sender, status) | Cloudflare | 24 hours |
| The audit trail (who, when, file names, no contents) | Cloudflare | Until the firm deletes it |
| The review session: the document, the files the model wrote | Anthropic | We delete it when the review ends; Anthropic may keep its own copies up to 30 days |
| Notes a lawyer asked it to remember ("'will', not 'shall'") | Anthropic and Cloudflare | Until deleted |

A daily sweep does the deleting. Each period is a setting the firm can
shorten.

The mail archive, which would keep every message the agent is copied on, is
**off**. We recommend it stays off for the pilot.

**The 30 days at Anthropic.** Anthropic's commercial terms say it may not
train models on customer content. Its stated practice for API customers is to
delete inputs and outputs within 30 days. We delete each review session as
soon as it ends; Anthropic's documentation does not say whether anything of a
deleted session stays on its backend inside those 30 days. The model we use by default, Claude
Fable 5.1, requires that 30-day retention. If the firm's Anthropic
organisation is on zero data retention, `ZERO_RETENTION` moves everything to
Claude Opus 5, which is available under it, at some cost in judgment. **That
covers the model only: the review sandbox (Managed Agents) is not eligible
for zero data retention, whatever the model.** A client that needs
nothing kept by anyone should be on the no-AI list below.

## Who can see what

- **The lawyer who sent it** sees the reply. A lawyer's conversations are
  theirs alone; one lawyer cannot retrieve another's.
- **Nobody on CC**, and nobody outside the firm. Mail from addresses the firm
  has not approved is dropped without a reply. Mail is accepted only if the
  sender's own domain authenticated it (DMARC); anything else is dropped.
- **Whoever runs the deployment** (us, unless the firm runs it in its own
  accounts) can read the database rows: who sent what and when, file names,
  the findings and the text of each tracked change. These are protected by
  Cloudflare's encryption at rest, **not** by the firm's key. Sealing them is
  not built; it is the next step if the firm asks. Whoever has access to the
  Anthropic workspace can read a review session turn by turn while it runs;
  it is deleted when the review ends.
- **Cloudflare and Anthropic**, under their own terms. We make no
  certification claims for either; check their trust portals.

## Privilege and confidentiality

ABA Formal Opinion 512 (July 2024) asks three things of a firm using
generative AI. Here is where each stands.

- **Confidentiality, including between clients.** Covered by the above, and
  by walls in what the system remembers. A note learned from one client's
  documents is kept under that client and used only on that client's
  matters. A lawyer's personal notes may hold drafting style only; code
  refuses a personal note that names a party, a client or a sum. A document
  whose client cannot be identified gets nothing remembered from elsewhere.
  Nothing the agent proposes to remember is used until the lawyer says
  "remember that"; unconfirmed, it is deleted after 7 days. This depends on
  the firm registering its clients with us.
- **Informed consent for a tool that learns.** By default it learns nothing
  unless a lawyer asks: undos, dismissals and sent versions teach it nothing
  (`LEARN_FROM_OUTCOMES` is off). What a lawyer asks it to keep stays within
  those walls. Whether to tell clients, and how, is the firm's decision.
- **Supervision.** A lawyer reviews every change before anything is sent.
  The audit trail records every job without its contents: received at,
  sender, matter, file names, what ran, the model and session, where it was
  stored and until when, who the reply went to. A named admin can ask for
  it by email for any month.

Every reply ends with the firm's privilege legend (`PRIVILEGE_NOTICE`) and
carries it in a header the firm's mail rules can match. Returned documents
are not stamped, because they are usually sent on to the other side.

Whether privilege is affected by a third party holding privileged material
for 30 days is a question for the firm's own judgment and jurisdiction. We
do not offer a view on it.

## What the firm controls

- **Who may send to it**: the approved list of addresses and domains.
- **Who replies go to**: the sender only (`REPLY_POLICY=sender_only`). The
  model cannot change this.
- **Clients that must not go to AI** (`NO_AI_MATTERS`): their documents get
  the mechanical checks only and no model call at all. A named admin can add
  a client by emailing "no AI for Acme".
- **The kill switch** (`SERVICE_PAUSED`): stops all reviewing and sending at
  once. Mail is held, not bounced, and released when switched back.
- **The key**: withdrawing it makes everything stored unreadable.
- **Deletion**: everything held for one lawyer, client or matter is deleted
  on request, ours and Anthropic's (`second-eye purge`).
- **Retention periods**: each of the periods above is a setting.

## What is not finished

We would rather you hear it from us.

- The model-backed review has been tested only against a simulation. It will
  run live before any firm document is sent.
- Deletion by client or matter finds what was recorded with one.
  Conversations from before 4 October 2026 with no matter number are reached
  only through the lawyer who sent them. Nothing we do shortens Anthropic's
  own 30-day copies, and the audit trail is blanked, not deleted.
- No SOC 2 or ISO 27001 certification and no independent penetration test.
  This is a pilot run by one person, not a security team.
- Within the firm, there is no ethical-screen list: a client's remembered
  notes are read on every review of that client's matters, whichever lawyer
  sends it.
- The document-system connection has never run against a real iManage.

## What we ask the firm to sign or decide

1. **A written retention policy**, signed by you, setting the periods above
   (or the firm's own).
2. **Acceptance of Anthropic's processing**, including the 30-day retention
   of the review container, or a decision to keep particular clients off the
   model entirely.
3. **The no-AI list**: clients whose outside-counsel guidelines exclude AI.
4. **Whose accounts it runs in**: the firm's own Cloudflare and Anthropic
   accounts, or ours on the firm's behalf.
5. **Pilot terms between the firm and us.** These have not been drafted yet;
   we will send them separately.
