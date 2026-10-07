# The firm's playbook: template

For the knowledge lawyer or partner who writes down the firm's negotiating
positions. Once loaded and approved, every review measures each clause
against them: on the firm's position, at its fallback, or off the playbook.
Until then reviews use the product's starter positions, and every finding
from one says "Starter playbook (not yet your firm's)".

A Word version of the blank template is `playbook-template.docx`, beside
this page.

## How it is read

A playbook agent reads what the firm sends (`src/lra/playbook.py`,
`docs/playbook.md`). It is a scribe, not an adviser. Four things follow, and
they decide how to write the template:

- **It records only what the text says.** A part left blank stays blank. It
  will not fill a fallback from general practice.
- **Every part must rest on a word-for-word quote.** Code checks each quote
  against the text of what was sent, and anything it cannot find is listed
  under "Check before approving". Short, plain sentences quote cleanly.
- **Model wording is copied, never drafted.** It must be wording the firm
  actually uses, word for word. It becomes a one-click tracked change only
  when it can replace the clause's operative sentence as it stands, so write
  it as a complete sentence.
- **One entry per clause family**, named as the product's starter files are
  named, so the firm's entry replaces the starter of the same name.

## Rules for writing it

1. One heading per clause family. Use these names where they fit:
   Limitation of liability; Indemnity; Termination; Assignment and change of
   control; Governing law and disputes; Confidentiality; Payment terms;
   Intellectual property; Non-solicitation; Force majeure; Insurance; Data
   protection. Any other family gets a plain name of its own ("Warranties",
   "Restrictive covenants").
2. Under each heading, the same six labels, in this order: Side, Position,
   Fallback, Walk-away, Watch for, Model wording (fallback).
3. Be concrete. "Reasonable cap" cannot be measured and will come back as
   something the agent could not settle. "100% of the charges paid or payable
   in the 12 months before the claim" can.
4. Leave out a family the firm has no position on. Leave a part blank rather
   than guess.
5. Say which side the positions are written from (customer or supplier,
   buyer or seller, lender or borrower). Different sides are different
   families: "Limitation of liability (supplier side)".
6. Do not contradict yourself across documents. If you also attach a
   precedent that says something different, the agent will report the
   conflict rather than choose.

## The template

Copy this block once per clause family.

```
## <Clause family>

Side: <whose side these positions are written from>

Position: <what the firm asks for, in one or two sentences>

Fallback: <what the firm will accept if pushed>

Walk-away: <what the firm will not accept, however it is dressed up>

Watch for:
- <a drafting trick or silence that defeats the position>
- <another>

Model wording (fallback): <the firm's own fallback wording, word for word
from a precedent, as one complete sentence that could replace the clause's
operative sentence>
```

## Three filled examples

These are examples of the shape, invented for this page. **They are not
anyone's positions. Do not send them.**

```
## Limitation of liability

Side: Customer.

Position: The Supplier's total liability in each Contract Year is capped at
200% of the Charges paid or payable in that Contract Year. Liability under
the indemnities, for breach of confidentiality and for breach of the data
protection obligations is uncapped.

Fallback: A cap of 100% of the Charges paid or payable in the Contract Year,
with the indemnities, confidentiality and data protection still outside the
cap.

Walk-away: Any cap below 100% of a year's Charges. Any cap that applies to
the indemnities.

Watch for:
- An exclusion of "loss of data" where the Supplier hosts our data.
- A cap measured on "Charges paid" alone, which is nil in the first month.
- One aggregate cap for the whole term instead of a cap per Contract Year.

Model wording (fallback): Subject to clause [X], each party's total
aggregate liability arising out of or in connection with this Agreement in
any Contract Year shall not exceed an amount equal to the Charges paid or
payable in that Contract Year.
```

```
## Governing law and disputes

Side: Either.

Position: English law and the exclusive jurisdiction of the courts of
England and Wales.

Fallback: English law with arbitration in London under the LCIA Rules, one
arbitrator, in English.

Walk-away: Any governing law other than English law. Non-exclusive
jurisdiction clauses.

Watch for:
- An expert determination clause that covers more than pricing disputes.
- A clause that lets one party alone choose between courts and arbitration.

Model wording (fallback): Any dispute arising out of or in connection with
this Agreement shall be referred to and finally resolved by arbitration
under the LCIA Rules, which are deemed to be incorporated by reference into
this clause.
```

```
## Assignment and change of control

Side: Customer.

Position: Neither party may assign without the other's prior written
consent, except that we may assign to a member of our group.

Fallback: Consent not to be unreasonably withheld or delayed, with our
intra-group assignment still free.

Walk-away: The Supplier free to assign or subcontract the whole of the
services without our consent.

Watch for:
- A change of control of the Supplier that is not treated as an assignment.
- "Subcontract" allowed freely where assignment is not.

Model wording (fallback): Neither party shall assign, transfer or
subcontract any of its rights or obligations under this Agreement without
the prior written consent of the other party, such consent not to be
unreasonably withheld or delayed.
```

## Sending it

1. **From a playbook admin's address.** Only addresses named in
   `PLAYBOOK_ADMINS` (or the named contact, if none are) can change the
   playbook. Anyone else is told so and nothing changes.
2. **A new email to the review address** (the deployment's `MAIL_AGENT_ADDRESS`,
   such as `review@legal.example.com`).
3. **Subject:** `Our playbook`
4. **Body**, for example:

   ```
   Here's our playbook.
   ```

   Any natural wording works: "Attached are our negotiating positions",
   "Please load our playbook". The subject alone is enough too.

5. **Attach** the filled-in template (Word, PDF or plain text all work;
   Excel and PowerPoint are read too). You may also attach the precedents
   the model wording comes from. Keep the whole under 25 MB.
6. **The reply** reads "Playbook updated: N positions from M documents
   (...)", lists anything it could not settle, and attaches
   `Firm playbook (for approval).docx`. Read that file. Every position cites
   its source.
7. **Nothing is used until you reply** `approve playbook`. Reply
   `discard playbook` to drop the draft.

Afterwards:

- To change one position, email: `Update the playbook: we now accept a cap
  of 150% of the annual Charges.` Only that family changes; you approve it
  the same way.
- To go back: `undo the last playbook change`. It can be repeated, back to
  the starter positions.

## Before the first one

Playbook intake by email needs the playbook agent: `lra agents apply` prints
`MANAGED_PLAYBOOK_AGENT_ID`, which goes into the deployment's `vars`. Without
it the reply says intake "isn't set up yet" and nothing changes.
