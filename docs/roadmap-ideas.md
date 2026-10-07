# Things this could do that nothing else does

> **Partly out of date.** Several ideas below have since been built: metadata
> checks, the clean copy, execution readiness, "what changed since last
> time", a dates and deadlines table, renumbering, and review of the other
> side's paper with an issues list. Drafting from scratch is no longer ruled
> out (DECISIONS 26). `STATUS.md` is the record of what exists; `ROADMAP.md`
> is the order of what comes next.

The combination of email as the interface, full retention, three-layer memory,
and delegated DMS access unlocks features a generic document reviewer cannot
produce. Ranked by value over effort.

## Built already

The deterministic layer: placeholders, defined terms, cross-references,
numbering, amounts, dates, party names, and docx leftovers (`checks.py`), plus
the email-native checks below (`email_checks.py`). See `docs/litera-parity.md`.

## Only possible because we live in the email

A Word add-in sees the document. We see the document *and* the message it is
about to travel in. Everything in this section is unreachable from a desktop
add-in, which makes it the most defensible part of the product.

### Wrong recipient (built)
The document names Acme Holdings LLC and the message is addressed to
`counsel@betacorp.com`. This is the worst mistake a lawyer can make by email,
and it is mechanically detectable the moment you can see both at once.

### Wrong attachment (built)
The email says "here is the NDA" and the attachment is called
`Share Purchase Agreement.docx`.

### The covering email contradicts the document (built)
"I capped liability at $1,000,000" when the document says liability is
unlimited. "Attaching a clean copy" when the file still carries tracked changes
from an associate. These are testable claims, and only something inside the
email can test them.

### Still to build here
- **Tone and commitment check.** The email promises something the document does
  not do, or concedes something in prose that the document still negotiates.
- **Thread memory.** Read the quoted thread. If the counterparty asked for three
  changes and the document only makes two, say so.
- **Late-night pass.** Documents sent after hours get a more careful read and a
  blunter verdict, because that is when mistakes happen.

## Tier 1: build these

### 1. Learn from what actually got sent
The strongest idea here and the cheapest. When a lawyer sends the final version,
diff the agent's redline against it. Every accepted change reinforces a pattern,
every rejected one suppresses it. The firm generates labeled training data as a
side effect of doing its job. Nothing else on this list matters as much.
Requires a BCC habit or a DMS hook.

### 2. Metadata and leftovers check
Before a document goes to a client: tracked changes from a prior draft still in
the file, internal reviewer comments, author names and editing history in the
document properties, hidden text, a "DRAFT" watermark on an execution copy, text
carried over from an unrelated matter.

Mechanically checkable, catastrophic when missed, and firms have had real
incidents from exactly this. It may be more immediately valuable than the
substantive review and it is much easier to get right.

### 3. Consistency with what the firm has already agreed
"You are asking for a 24-month non-solicit. You agreed to 12 months with this
counterparty in March and 12 months in the two deals before that."

Only possible with matter memory and DMS access. This is the finding that makes
a partner forward the agent's email to someone else.

### 4. Diff against the last version I saw
On a second or third review of the same document, lead with what changed since
last time rather than re-reviewing everything. Cuts latency, cuts false
positives, and matches how people actually work on a draft.

### 5. Deviation from the firm's standard form
The agent has the standard form in the DMS. Open every review of a templated
document with "this differs from the firm's form in four places, here they are."
That is the single most reusable finding type in the entire product.

## Tier 2: build these once tier 1 lands

### 6. Counterparty and opposing-firm profiles
Across matters, build a picture of how each counterparty negotiates: which
positions they always take, which they always concede, how their paper differs
from yours. Turns a document reviewer into negotiation intelligence.

### 7. Execution readiness mode
A separate checklist for the last mile: signature blocks correct, every schedule
and exhibit attached and referenced, all blanks filled, dates consistent, right
entity names and jurisdictions. Different from a drafting review, and it is the
moment mistakes are most expensive.

### 8. Clean version alongside the redline
Two attachments: the redline, and a client-ready copy with internal comments and
metadata stripped, plus a list of what was removed. One of them is obviously
safe to send.

### 9. Obligation and date extraction
Pull every date, deadline, notice period and renewal trigger into a short table
at the bottom of the review email. Cross-check them against each other. Notice
periods that expire before they begin, terms that end before they start,
effective dates after signature dates.

### 10. Ask a question back
"This indemnity differs from your standard form. Want to see the diff?" Turns
the review into a conversation rather than a verdict, and the reply is itself a
training signal about what lawyers actually care about.

## Tier 3: interesting, unproven

### 11. Firm-wide weekly digest
"Here is what I caught across the firm this week." Surfaces systemic drafting
problems and is the artifact that justifies the tool to whoever approves budget.

### 12. Precedent suggestion on a blank page
A lawyer emails "I need an NDA for a manufacturing client in Ohio" with no
attachment. The agent finds the three closest precedents in the DMS and replies
with them. Different product, same interface, almost no new machinery.

### 13. Second-chair mode
CC the agent on the real negotiation thread with opposing counsel. It follows
along and flags when a proposed change conflicts with something agreed earlier
in the thread. High value, high risk, needs a lot of trust first.

## Deliberately not doing

- **Advising on the transaction.** The agent reviews documents, not deals. This
  line is what keeps it defensible with no supervising-attorney queue.
- **Drafting from scratch.** Different product, different risk profile.
- **Sending anything to a client.** The agent replies to the requesting lawyer
  and to nobody else, ever.
- **Anything with a user interface.** One consent screen is the entire budget.
  The moment this needs a dashboard, the core bet has failed.
