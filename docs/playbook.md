# The playbook

Litera's Lito reviews against a firm's playbook: for each clause, is it on the
firm's position, at the fallback, or off the book. This is our version, and it
is a skill rather than a prompt because a playbook is a set of files a
knowledge lawyer maintains, not a paragraph an engineer edits.

## Where it lives

`skills/lra-playbook/`. `skills/lra-playbook/SKILL.md` tells the reviewing agent how to use it; one
file per clause type sits in `positions/`, in the format `skills/lra-playbook/positions/README.md`
sets out: position, fallback, walk-away, what to watch for, and model wording
for the fallback. The agent opens only the files for clauses the document has,
so a twelve-clause playbook costs nothing on a two-clause letter.

## Starter positions

The product ships twelve starter files (indemnity, limitation of liability,
termination, assignment and change of control, governing law and disputes,
confidentiality, payment terms, intellectual property, non-solicitation, force
majeure, insurance, data protection), written from a customer-side commercial
stance. Each says `status: starter` in its front matter, and every finding that
rests on one begins "Starter playbook (not yet your firm's):", so nobody
mistakes a starter for the firm's view. The firm replaces the files and changes
the status to `firm`; nothing else changes.

## What the agent does with it

Off-playbook clauses are `substantive` findings in category `playbook`, phrased
as a question: the firm can accept anything, so this is never a blocker. The
fallback wording becomes a proposed tracked change only when it can replace the
clause's operative sentence verbatim; a fallback that would have to be adapted
goes in the explanation instead, because a half-fitted fallback accepted in one
click is worse than none.

## Uploading it

```bash
.venv/bin/second-eye skills sync lra-playbook      # prints SANDBOX_PLAYBOOK_SKILL_ID
```

Set the id in `.env` (or the deployment's `tenant.jsonc` vars) and run
`second-eye agents apply`, which attaches the skill to both agents alongside
`lra-document-tools`. Run the sync again after editing a position and the next
review uses the new version; the agents ask for `latest`.

## On the other side's draft

When the document is the counterparty's paper (`src/secondeye/pipeline/their_paper.py`),
the review answers it rather than polishing it. It is their paper when the
lawyer says so ("their draft", "opposing counsel's"; "our draft" says the
opposite and wins), when the lawyer forwarded it from someone outside the
firm, or when the document's author, last-saved-by or tracked-change author
is an email address outside the firm. A name alone is never judged, and a
document already sent, a proofread and a question are never treated as theirs.

On their paper none of their typos or style points is written into their
text (the reply counts them in one line), the agent reports each off-playbook
clause with `our_position` and `response`, and those substantive points go
out as `<stem> (issues list).docx`: clause, what their draft says, our
position, proposed response. The verdict reads "Their draft: 5 points to push
back on." Placeholders, broken references and contradictory figures are still
reported.

## Teaching it by email

A playbook admin (`PLAYBOOK_ADMINS`; empty means the `ALLOWLIST_CONTACT`
address, or failing that anyone on `FIRM_DOMAINS`) emails "here's our
playbook" with the firm's precedents, positions memo and checklist attached
(Word, PDF, Excel), or writes "update the playbook: we now accept a 2x cap".
`src/secondeye/playbook.py` hands the material to the playbook agent
(`agents/playbook.agent.yaml`, its own agent: its job is to extract, never to
draft, and it carries none of the associate's tools), in a session opened with
an outcome whose rubric (`agents/playbook_rubric.md`) says every position cites
its source and nothing is invented. The agent reports through
`record_playbook`; every quote it cites is looked for in the text we extracted,
and the misses are handed back to it once. What still misses is named in the
reply under "Check before approving".

The reply reads "Playbook updated: 9 positions from 3 documents (Limitation of
liability, Indemnity, ...). 2 things I couldn't settle: ..." with the draft
attached as `Firm playbook (for approval).docx`. Each position becomes a file
in exactly the format above, `status: firm`, with a `## Source` section.

**Nothing takes effect until the sender replies "approve playbook".** A
playbook is firm-wide, and this is the firm's policy on firm-wide rules
(open question O19), not a check on the model, which wrote every word of the
draft. "discard playbook" drops a draft. "undo the last playbook change"
puts back the previously approved version, or the starters if there was
none, and can be repeated.

### Where the firm's playbook lives

In a memory store per firm (`firm-playbook`), not in a custom skill version
per firm. Skills attach to the agent definition, so a firm's own skill would
need an agent per firm, or re-applying the shared reviewer whenever one firm
changes a position. Memory stores attach per session: every review and
instruction session mounts the firm's store read-only beside the lawyer's
memory (`memory.stores_for`), and the skill's instructions tell the reviewer
that a mounted firm playbook replaces the starter files entirely. The store
keeps a version of every file it has held; our own `playbook_versions` table
keeps every draft and approval, with who and when, and is the source of
truth the store is projected from, as in `memory.py`.
