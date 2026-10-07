# Code review, for legal documents

## The thesis

Code review works not because any single review is brilliant, but because it
runs on everything. Nobody decides whether a pull request is worth reviewing.
The review is part of the workflow, it is fast, it ends in a verdict, and its
suggestions can be accepted with one click. The habit is the product.

Legal documents have no equivalent. A lawyer produces a document, reads it once
more if there is time, and sends it. Whether anyone else looks at it depends on
seniority, deadline pressure, and luck. The checking that does exist lives in a
Word add-in somebody has to remember to open.

This product is the missing equivalent: something you run on every document you
produce, always, without deciding to.

## What "always" demands

Optimizing for ubiquity rather than depth changes almost every design decision.

**The trigger cannot be a decision.** If running the review is a choice, it will
be skipped exactly when it matters, which is at 11pm under deadline. Two
triggers, one engine:

| Trigger | Code analogue | When it fires |
| --- | --- | --- |
| Document checked into the matter | CI on every push | Ambient. Nobody asks for it |
| Emailed, forwarded, CC'd or BCC'd | Opening a pull request | Deliberate, at the send moment |

The email path is where the product starts, because it needs nothing installed.
The check-in path is where it becomes genuinely unskippable.

**It has to be cheap enough to run on everything.** This is why the
deterministic layer is the core of the product rather than a supporting act.
Placeholders, cross-references, numbering, defined terms, amounts, dates, party
names, metadata leftovers: exact, instant, free, and impossible to hallucinate.
That layer can run on every document forever at no marginal cost. The agent is
escalation, not the baseline.

**False positives are existential, not annoying.** A linter that cries wolf gets
disabled. A review tool that runs on everything and is wrong ten percent of the
time gets a filter rule in Outlook within a fortnight. When the review was
opt-in, a false positive cost a few seconds. When it runs on everything, it
costs the habit, and the habit is the entire product.

This is why the target is fewer than one false positive per clean document, why
half the check tests assert silence rather than detection, and why conservative
redlining is the default.

**It has to end in a verdict.** Not a report, not a score. Approved, or changes
requested, or do not send. A lawyer scanning a preview pane on a phone should
know in one line whether they can hit send.

**Suggestions must be acceptable in one motion.** The tracked change is our
version of committing a suggestion from a review comment. This is why the OOXML
writer is the gating piece of work and why a redline that has to be re-keyed is
worth nothing.

## What this reframing changes

Three things move up, and one large thing moves down.

**Up: the diff is the review.** Code review reviews changes, not files. The most
valuable review of a document is almost never "here is everything wrong with
this contract." It is "here is what changed since the version you sent Tuesday,
and here is what I think about it." That makes the archive a core component
rather than a nice-to-have, because you cannot diff against a version you did
not keep.

**Up: status, not prose.** A reviewed document has a state, the way a pull
request does. Clean, changes requested, blocked. That state is what the tracker
is built from, and it is what makes "which of my documents are not ready" a
question with an answer.

**Up: coverage as the metric.** The number that matters in year one is not
finding quality. It is what fraction of the documents a firm sends were reviewed
at all. A mediocre review on everything beats an excellent review on the ten
percent somebody remembered to check.

**Down: depth-first features.** Benchmarking, counterparty profiles and
negotiation intelligence are genuinely valuable, and none of them matter until
the thing runs on everything. They are what you build in year two to keep people
who already have the habit.

## Where this goes

The destination is a junior associate who lives in the address book: anything
you would hand a junior, you can email to this, including first drafts from the
firm's forms and, as a stretch goal, research (DECISIONS 26).

That does not change anything above. It is the reason for it. A junior is not
given the research memo on day one. They are given the proofread, and they get
the bigger work by never being wrong on the small work. Code review is how this
associate earns the rest of the job, and "AI associate" is a pitch every firm
has already heard, while "the check that runs on everything" is not. So the
review is the way in, coverage stays the metric, and each new kind of work is
added only once the habit underneath it is holding.

## The one-sentence version

Not "AI that reviews contracts." Every firm has three of those.

This is the check that runs on everything before it leaves the building, and the
reason it can run on everything is that it costs nothing to run and it is almost
never wrong.
