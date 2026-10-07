# Writing a position file

One file per clause type, named for the clause in lower case with hyphens:
`limitation-of-liability.md`, `governing-law.md`. The reviewing agent opens a
file only when the document has that clause, so keep each file about one thing.

```markdown
---
clause: Limitation of liability
status: firm            # firm | starter
side: customer          # whose side these positions are written from
updated: 2026-09-26
---

## Position
What we ask for, in one or two sentences.

## Fallback
What we will accept if pushed. This is the line the agent measures against.

## Walk-away
What we do not accept. Anything here is off playbook however it is dressed.

## Watch for
The disguises: a carve-out list that swallows the cap, a "to the extent
permitted by law" that does nothing, silence where the clause is expected.

## Model wording (fallback)
The fallback as a sentence that could replace the clause's operative sentence
as written. If it cannot be dropped in verbatim, say so and give it as guidance.

## Source
- Positions memo 2026.docx: "the quote the position rests on"
```

`## Source` (and `source:` in the front matter) is on the firm's own files,
the ones written from the documents the firm sent by email
(`docs/playbook.md`): every position cites the text it came from. The starter
files have none, because they are the product's, not the firm's.

Keep positions short and concrete. "Reasonable" is not a position; "12 months'
fees" is. A file the firm has agreed says `status: firm`; until then the
product's starter files say `status: starter` and every finding from one says
so, so nobody mistakes a starter for the firm's view.
