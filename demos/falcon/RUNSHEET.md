# Project Falcon: run sheet

One matter, 36 hours, eight beats, every one of them an email. Carrow Lisle LLP
acts for Northwind Holdings Limited, buying Falcon Logistics Limited from Beta
Trading Limited through a new company, Falcon Topco Limited (it was called
Falcon Holdings Limited until the agreed form). Harrowgate LLP act for Beta.
Everything and everyone here is invented; the other side's addresses are at
the reserved domain `harrowgate.example`. The exceptions are the lawyer's
mailbox (the one allowlisted sender, who sends every email) and the agent's
address: those are whoever runs the demo, set as `DEMO_LAWYER`, `DEMO_AGENT`
and `DEMO_FIRM_DOMAIN` in the environment or in `demo.env` in the primary
deployment's private folder (`deployments/<name>/demo.env`; see
`demos/identity.py`). Without them the build and the send refuse, and
`--example` writes them with invented addresses to look at.

The thread through the talk: **a Word add-in sees one document; Redline Desk
sees the email it travels in, the version before it, what we asked for last
round, and what happens after it is sent.** Every beat is one of those.

## Before the day

1. **Build and rehearse offline.** Both are free and take seconds.

   ```bash
   python -m demos.falcon.build                # demos/falcon/build/: documents and .eml files (needs DEMO_*)
   python -m demos.falcon.rehearse --stub      # every beat through the real handler
   ```

   The rehearsal prints each step's first line and fails if a reply is
   missing its headline. The replies, HTML and attachments are in
   `demos/falcon/out/<step>/`. Open a few attachments in Word.

2. **Configure production** (the vars in the deployment's
   `deployments/<name>/tenant.jsonc`, then `lra tenant render <name>`; or
   `.env` for a laptop run), then push:

   | Setting | Value | Why |
   | --- | --- | --- |
   | `PLAYBOOK_ADMINS` | the address in `DEMO_LAWYER` | Otherwise a Gmail sender is not a playbook admin and beat 1 is refused |
   | `MANAGED_PLAYBOOK_AGENT_ID`, `MANAGED_COMMENTS_AGENT_ID`, `MANAGED_CLOSING_AGENT_ID` and the review ids | from `lra agents apply` | Beats 1, 3 and 7e to 7g need their own agents |
   | `SANDBOX_SKILL_ID`, `SANDBOX_PLAYBOOK_SKILL_ID`, `SANDBOX_COMMENTS_SKILL_ID`, `SANDBOX_CLOSING_SKILL_ID`, `SANDBOX_NEGOTIATION_SKILL_ID` | from `lra skills sync <name>` | Without the negotiation skill, beat 4 is an ordinary review and the lie detector never runs |

   Register the client once: `lra clients add 1042 "Northwind Holdings Limited"
   --alias Northwind --domain northwind.example`. Put credit on the Anthropic
   account.

3. **Rehearse live the day before**: `python -m demos.falcon.rehearse --live`
   (reads `.env`; costs about one review session per model beat). Read every
   reply in `out/`. The live model decides its own words, so the lines below
   are the stub's; check the live ones say the same thing.

4. **Mailbox.** Send from the `DEMO_LAWYER` address only. For each beat, open
   `build/emails/<step>.eml` for the subject, body and attachments (attachments
   are also under `build/<n> .../`). Replies: press Reply on the agent's reply
   named in the "Thread" line, paste the body. `python -m demos.falcon.send
   --list` prints the order.

**Credit.** Beats 1a, 2, 3, 4, 5, 6, 7a, 7e, 7f, 7g and 8 call a model. 1b, 7b,
7c, 7d and 7h are deterministic and cost nothing.

## The beats

### 1. 09:00. Teach it the playbook (steps 1a, 1b)

- **Setup:** none; a fresh firm with no playbook.
- **Action:** new email, subject "Our buy-side positions", body from
  `1a`, attach the memo and the Heron and Kestrel SPAs (`build/1 Playbook/`).
  When it answers, reply `approve playbook`.
- **Audience sees:** `Playbook updated: 12 positions from 3 documents (...)`,
  one thing it would not guess ("The memo gives no walk-away for the de
  minimis and basket"), then `Nothing changes until you reply "approve
  playbook".` After approval: `Approved. From the next review, documents are
  checked against the firm's playbook (12 positions)`.
- **Talk track:** the knowledge team maintains positions in a memo, not a
  product console. Every position cites the memo word for word, and the
  quotes are checked by code, not trusted. Nothing is firm-wide until a named
  admin approves it by email. An add-in's playbook lives in its own admin UI.
- **Fallback:** "isn't set up yet" means `MANAGED_PLAYBOOK_AGENT_ID` is empty;
  "Only the firm's playbook admins" means `PLAYBOOK_ADMINS`. Skip to beat 2:
  the starter playbook covers the same points, and the findings say
  "Starter playbook".
- **Credit:** 1a yes, 1b no.

### 2. 11:30. Their paper, forwarded with no instructions (step 2)

- **Action:** forward Dana's email (`2` body) with `Project Falcon - SPA v1
  (Harrowgate).docx`. Write nothing above it.
- **Audience sees:** `Their draft: 7 points to push back on.` and `I read this
  as their draft, because you forwarded it from dana.whitlock@harrowgate.example,
  so I have not marked up their wording (14 typo and style points left alone).`
  Attached: `... (issues list).docx`: clause, their words, our position,
  proposed response.
- **Talk track:** nobody said "their draft" or "issues list". It knew whose
  paper it was from who sent it, and it did not spend the associate's evening
  fixing Harrowgate's spelling. An add-in opens a document; it never sees the
  forward.
- **Fallback:** if it marks up their typos, it did not read it as theirs:
  resend with "their draft" above the forward.
- **Credit:** yes.

### 3. 13:00. Turn the partner's comments (step 3)

- **Action:** new email, `turn these`, attach `Project Falcon - SPA (our
  response, JP comments).docx` (five comments by J. Partner).
- **Audience sees:** `Turned 5 comments: 4 done and resolved, 1 left open
  (cl. 12.1: Left open for you: 18 months is the memo's fallback ...)`. In
  Word: three tracked changes, every comment answered in its thread, four
  marked resolved, the question to the client left open.
- **Talk track:** this is the associate's afternoon, done as Word threads it,
  and it knows which comment is an instruction and which is a question for
  the client.
- **Fallback:** "isn't set up yet" means the comments agent or skill id is
  missing. If a comment comes back unanswered, it says so by clause.
- **Credit:** yes.

### 4. 16:00. v2, and a covering email that is not true (step 4)

- **Action:** forward Dana's v2 email (`4`), attach `Project Falcon - SPA v2
  (Harrowgate).docx`. Nothing above it.
- **Audience sees:** first line `Not what they said. Besides the cap, v2
  moves the long-stop date from 31 March to 31 January and deletes the fraud
  carve-out in 9.4.`, then `5 of your 7 points were accepted; 2 were not — and
  one of those isn't mentioned in their email.` and under "What their email
  says": `“We've accepted all your points except the liability cap.” Not
  true: Clause 12.1 still gives 12 months ...`. Attached: the issues list
  again with a status column.
- **Talk track:** it remembered the seven points from 11:30, compared v2
  against what we sent, and read Harrowgate's email as a claim to check. The
  carve-out went missing without a word. No add-in has the ledger, the
  previous version or the covering email.
- **Fallback:** if the reply is an ordinary review, the negotiation skill id
  is not set; say the line yourself and open the attached comparison
  ("changes since last time"), which shows 9.4 deleted and the date moved.
- **Credit:** yes.

### 5. 19:00. Do the numbers work? (step 5)

- **Action:** new email, body from `5` ("Their draft v2 again ... do the
  numbers and the dates actually work? Nothing else."), attach v2 again.
- **Audience sees:** `Their draft: 1 point to push back on, and 1 error in it.`
  then `The Purchase Price is £12,500,000 in clause 3.1, but Schedule 2 totals
  £12,050,000.` and the point on 12.2: claims against the escrow must be
  notified within 90 days, but the escrow is released after six months.
- **Talk track:** the arithmetic is code, not a guess: it adds up Schedule 2
  and compares it with the defined price. The timing problem was created by
  accepting our own point (six-month escrow) and leaving 12.2 alone: exactly
  what a tired reader misses at 7pm. (The price error was already in the 16:00
  reply, three screens down; this is the partner asking directly.)
- **Fallback:** the arithmetic line is deterministic and always there; if
  the live model does not raise 12.2, point to the dates table in the same
  reply ("Retention period — six months after Completion").
- **Credit:** yes.

### 6. 23:47. The save (step 6)

- **Setup:** `harrowgate.example` is a reserved domain, so Gmail will bounce
  the copies to Dana and Theo. Expected; say so, or change the To and Cc to a
  second mailbox you own.
- **Action:** a new email **to Dana, Cc Theo, Bcc review@**, subject "Project
  Falcon - SPA: our v3", body from `6`, attach **`Project Falcon - SPA v2
  (Harrowgate) - JP notes.docx`** (the wrong file).
- **Audience sees:** `Already sent, with 3 errors in it.`, `This already went to
  dana.whitlock@harrowgate.example, theo.marsh@harrowgate.example.`, then
  `1 internal comment(s) are still in the document. Comments from J. Partner
  will be visible` and `This is Harrowgate's v2 with J. Partner's notes, not
  your v3`, and the comment itself: “we can go to 2x if pushed”.
- **Talk track:** the BCC habit is the product. Nobody asked for a review at
  23:47; the mail rule copied the agent, and it read the email against the
  attachment. A Word add-in cannot know what was attached to which email, to
  whom, or what the email promised.
- **Fallback:** the leftover-comment line is deterministic and always there.
  If the live model misses the wrong file, read the email aloud against 9.4.
- **Credit:** yes.

### 7. Next morning: the agreed form to the executed set (steps 7a to 7h)

- **7a, 08:30, final check.** New email, attach `Project Falcon - SPA (agreed
  form).docx`. Audience sees `Don't send yet: 3 things to fix first.` with
  `Should this be 4 or 5?`, `Should this be 30 or 13?` and `Is the Buyer
  Falcon Topco Limited or Falcon Holdings Limited?` (the Buyer was renamed
  everywhere but its signature block). Credit: yes.
- **7b, reply `30; 4; clean copy`.** Audience sees `Clean copy attached, but 1
  question is still open.` and `Noted: 30 and 4. I have made those changes.`
  No credit.
- **7c, reply `sig pages`.** Audience sees `Not ready to sign. 2 things to
  settle first.` and `The Buyer's signature block names Falcon Holdings
  Limited as the Buyer, but the parties clause says Falcon Topco Limited.`
  Talk track: it will not produce a signature page for a company that is not
  the party. No credit.
- **7d, reply `Falcon Topco Limited; clean copy`.** Audience sees `Noted:
  FALCON TOPCO LIMITED. I have made that change.` and a clean copy. No credit.
- **7e, 09:15, new email** `Sig packets for the closing, please ...` attaching
  `Project Falcon - SPA.docx` (the execution version: the same text as 7d's
  clean copy), the Disclosure Letter and Topco's board resolution. Audience
  sees `0 of 6 in. Waiting on: Beta's director — SPA, Disclosure Letter;
  Falcon Topco's director — ...; Northwind's director — SPA.` and a packet
  per signatory. Credit: yes.
- **7f, 14:05, forward Dana's email** with `Beta signed pages.pdf`. Audience
  sees `2 of 6 in.` Credit: yes.
- **7g, 16:20, reply on the 7e thread** with `Northwind signed pages.pdf`.
  Audience sees `All 6 in.`, `Executed set compiled: 3 documents and the
  closing index.` Attached: each document executed, and the closing index.
  Credit: yes.
- **7h, reply on the 7d thread:** `send me the deadlines as a calendar`.
  Audience sees `1 dated deadline from ..., as a calendar file.` with an .ics
  to open: the Long Stop Date (clause 4.4). The Accounts Date is a reference
  date before signing, not a deadline, so it stays out; the periods that run
  from Completion are listed underneath. No credit.
- **Signed pages, live:** the prebuilt PDFs match the stub's plan. A live
  closing agent names its own signatories and page ids, so sign the packets
  it actually sent: `python -m demos.falcon.signing "Signature packet -
  <name>.pdf" --signer "Martin Hale" --out "Beta signed pages.pdf"` (the live
  rehearsal does this itself).
- **Talk track:** Transact's job by email: packets, a running tally counted
  by code from the stored state, signed pages read and filed, the executed set.
- **Fallback:** "Closings by email aren't set up yet" means the closing
  agent id is missing; skip to 7h, which needs no model.

### 8. A week later: the encore (step 8)

- **Action:** forward Dana's NDA email (`8`; its first line says "Northwind
  again, matter 1042"), attach `Beta Freight - NDA (Harrowgate).docx`.
- **Audience sees:** `Their draft: 3 points to push back on.`: one-way
  confidentiality, 12 months instead of three years, and a non-solicit that
  catches people answering a public advertisement.
- **Talk track:** same client, new matter: the review mounts Northwind's
  client memory and this matter's, and never another client's (the stub
  rehearsal asserts exactly that). The memory wall is by client, not by
  lawyer.
- **Credit:** yes.
