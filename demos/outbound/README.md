# The send-moment demo

"Word agents review the document you open. Redline Desk reviews what you
send." One email, as a lawyer would send it at 11pm: the execution version of
an SPA, a board deck and a PDF exhibit. Every mistake in it is one a careful
associate checks for before pressing send, and every catch is made without a
model, so it works on production today.

## What is in it

| Where | Planted | Caught as |
| --- | --- | --- |
| Note | "the execution version ... (v5)"; the file is v4 | question: wrong version |
| Note | "with the disclosure letter"; there is none | question: promised attachment missing |
| Note | "removed the exclusivity clause"; clause 5 still has it | your call |
| SPA | header "DRAFT 4: March 1, 2026" on a version called execution | blocker |
| SPA | two internal comments (the client's fallback on the cap) | blocker |
| SPA | `[●]` for the completion date | blocker |
| SPA | the fallback cap as hidden text | your call |
| SPA | a highlighted sentence on a version called final | question |
| SPA | title and document-system client field name Bluewater Shipping LLC | question |
| SPA | the fee workbook embedded whole | your call |
| SPA | author metadata | minor point |
| Deck | speaker notes on slide 3 ("we can go to $3m"), slide 4 hidden | your call, named by file |
| Exhibit PDF | a partner's sticky note and a highlight | blocker, named by file |

Not planted, and correctly silent: the liability cap the note claims
($2,000,000) is in the document, and the companies the note names are the
parties.

## Run it here, no email

```
python -m demos.outbound
```

From the repository root, with the dev dependencies installed (reportlab).
It writes the files to `demos/outbound/files/` and runs `files/email.eml`
through the same handler production runs, printing the reply instead of
sending it. The model is switched off for the run and the database is a
temporary file, whatever `.env` says. `--build` only writes the files.
`lra replay demos/outbound/files/email.eml` does the same with your own
settings.

## Send it to production

Production must be running a build with these checks (deployed from main).

1. From an allowlisted address, write a new email to your deployment's
   agent address (its `MAIL_AGENT_ADDRESS`; `files/email.txt` says
   `review@legal.example.com`). Nobody else on To or Cc.
2. Subject and body: copy them from `files/email.txt`.
3. Attach the three files in `files/`: the SPA (.docx), the deck (.pptx)
   and the exhibit (.pdf).

The reply should open "Don't send yet: 4 things to fix first", with the
mechanical-only line at the end while the account has no credit. The checks
that need someone outside the firm on the message (wrong recipient, a
personal address, an internal marking going out) are not in this demo,
because sending it would mean mailing a real outside address; they are in
the eval (`python -m evals.run_checks`).
