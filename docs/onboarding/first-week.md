# The first week

For the firm's pilot lawyers. Five things to try, one a day, each a single
email. Nothing to install. Send from your own firm address to the review
address you were given; replies come back to you and nobody else.

Two habits make everything work better:

- **Put the matter number in the subject**, as `Matter 10234-0007`. That is
  how what it learns stays with the right client.
- **Read the first line.** It is the verdict: whether you can send.

If anything comes back wrong, reply to it saying so in plain words, and tell
the firm's pilot contact. A wrong finding is the thing we most need to hear
about.

## Day 1: send a draft you think is finished

The point: see it catch an error you would have sent.

Pick a recent draft of your own, ideally one that has already gone out. Then:

```
To:      the review address
Subject: Matter 10234-0007 - SPA final check
Body:    (nothing, or "final check before this goes out")
Attach:  the Word document
```

**What comes back** in a few minutes: a first line such as "Don't send yet:
3 things to fix first", the points clause by clause, and your document with
tracked changes, each with its reason in the margin. The checks that find
broken cross-references, placeholders left in (`[●]`, "NTD"), amounts whose
words and figures disagree, a party called two different names, sums that do
not add up, a weekday that does not match its date, and comments left in the
file are exact. They run on every document and do not need the model.

Then reply `help` to see everything else it does.

## Day 2: their draft, forwarded with nothing written

The point: an issues list against the firm's positions, without being asked.

Forward the other side's latest draft, as it arrived from them:

```
Forward: the email from opposing counsel, with their draft attached
Body:    (nothing; or "their draft" if it came to you from a colleague)
```

**What comes back**: "Their draft: N points to push back on." It reads the
document as theirs because you forwarded it from outside the firm, so it
does not mark up their typos (it counts them in one line). Attached:
`<name> (issues list).docx`, with the clause, what their draft says, the
firm's position and a proposed response. Until the firm's playbook is loaded
and approved, each point says it rests on the starter playbook.

This one needs the model.

## Day 3: answer its questions and get the clean copy in one reply

The point: from draft to sendable copy without opening Word.

Take a reply from day 1 or 2 that asked you questions. Each question is
numbered or offers options ("Should this be 30 or 13?"). Reply to it:

```
Reply to: its review
Body:     30; 2; clean copy
```

Semicolons separate requests. It makes those changes and sends back a clean
copy: every tracked change accepted, comments and author details removed. If
a question is still open, the first line says so.

Also try, on the same thread: `undo 2` (the numbers are beside each change),
or a plain instruction such as `call them the Purchaser throughout`.

## Day 4: signature pages and the deadlines

The point: the closing chores.

On the thread of a document that is ready to sign, reply:

```
Reply to: its review or clean copy
Body:     sig pages
```

**What comes back**: an execution copy and a signature page per party, or
exactly what is in the way ("Not ready to sign. 2 things to settle first"),
such as a signature block naming a company that is not a party.

Then, on the same thread:

```
Body: send me the deadlines as a calendar
```

**What comes back**: the dated deadlines in the document as a table and a
calendar file to open.

## Day 5: BCC it on something you send

The point: the habit. It checks what goes out without you deciding to.

When you next send a document to a client or the other side, add the review
address in **BCC**. Nothing else.

**What comes back**, to you only: "Already sent", with anything it would
have caught. Nobody you sent it to hears from it, and an email with nothing
attached gets no reply at all, so copying it on ordinary mail costs nothing.
Reply `setup` for how to make this a habit in your mail program. If you
would rather it check before sending, forward it first instead.

## What it will not do

- Send anything to anyone but you.
- Accept a change, file a document or advise on the deal.
- Review a client's documents on the firm's no-AI list with the model: those
  get the exact checks only, and the reply says so.

## Words that work on any thread

| Reply | What it does |
| --- | --- |
| `help` | What it can do |
| `30` | Answers its question with that value |
| `undo 2`, `undo everything` | Reverses one change, or all of them |
| `clean copy` | Every change accepted, metadata removed |
| `renumber` | Fixes typed clause numbers and every reference to them |
| `stop flagging <thing>` | Stops raising that with you; `flag it again` brings it back |
| `remember that` | Keeps a note it offered to remember; nothing is kept until you say so |
| `stop` | Leaves the thread alone |
