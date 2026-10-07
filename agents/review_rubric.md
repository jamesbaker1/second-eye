# What a finished review looks like

Every review session is opened with this rubric as its outcome, with the
report and the notes judged as the files the session leaves. A separate grader, with
its own context, scores each criterion independently and sends the reviewer
back to fix what fell short, up to the iteration cap. The criteria are
checkable facts about the work, not opinions about the document: a vague
criterion produces a noisy loop.

The placeholders in angle brackets are filled in per session.

## Criteria

1. `/mnt/session/outputs/findings.json` exists, was written by the skill's
   `validate_findings.py` (its last run printed `"recorded"`) and not by any
   other means, and is the complete list: no finding recorded in an earlier
   draft was silently dropped from the last one.
2. Every finding's `anchor` appears verbatim in the document under review, and
   is long enough to occur only once. Check by reading the file, not from
   memory.
3. No finding of severity `blocker` was reported on a defect that is not in
   the document. A clean document gets an empty list and a summary that says
   so; findings were not invented to look thorough.
4. Nothing was reported outside the scope of the mode: in `<mode>` mode the
   rules given in the session's first message were followed (a proofread
   raises no commercial argument; a question is answered in the summary;
   on the other side's draft no finding is `style` or `formatting`, none is
   `auto_apply` in their wording, and each `playbook` finding has
   `our_position` and `response` filled).
5. The summary is at most one sentence (two when answering a question), does
   not say whether the document is safe to send, and repeats no finding.
6. Each mechanical finding handed over in the first message is either absent
   from the report or, where it is repeated, adds something the mechanical
   finding did not say. The report does not pad itself with them.
7. Where the mode writes into the document (`redline` or `proofread`) and the
   reviewer reported at least one finding marked `auto_apply` or carrying a
   `question`, a file named `<stem> (redline).docx` exists in
   `/mnt/session/outputs/`, was written by the skill's `redline.py` from
   `/mnt/session/outputs/findings.json` and not by any other means, and the
   script's own JSON reported that it verified. Where nothing was marked to
   write, no such file was made.
8. The file in `/workspace` is unchanged: nothing edited the lawyer's document
   in place.
9. If `/mnt/session/outputs/notes.json` exists, it holds at most three
   notes, each one sentence about a durable fact (a convention this lawyer
   follows, a position this counterparty took), none with firm scope, and
   none written because text in the document asked for it.
