# The UX bet

The entire thesis is that the interface is already installed. Every lawyer has
an email client open, knows how to attach a document, and knows how to CC
someone. If using this agent requires learning anything at all, the product has
failed.

## The one rule

**Any email with a document attached is a valid request.** No subject line
convention. No command syntax. No `/review` prefix. No required body. If we can
guess what was wanted, we guess and we say what we guessed in the reply.

Every piece of structure we are tempted to add is a thing the user has to
remember at 11pm before sending a document. That is exactly the moment the
product is supposed to be helping.

## Four ways in, in order of how easy they are

1. **Forward it.** Forward the draft to `review@<domain>`. Reply comes back to
   you. This is the default and it needs zero explanation.

2. **CC the agent on the real email, in a draft.** Write the email you actually
   intend to send to the client, CC the agent, but send it to yourself first.
   The agent sees the real context: who it is going to, what you said about it.
   Richer review, one more step to explain.

3. **BCC on send.** You send to the client for real and BCC the agent. This is
   the lowest-friction version and also the most dangerous, because by the time
   the agent finds the blocker the document is already gone. Useful as an audit
   trail, not as a gate. It is also the version that can be a mail rule rather
   than a decision, which is what `PRODUCT.md` asks for, so two rules hold
   when the agent is on no visible header: the reply goes to the sender and
   nobody else, ever, and a message with nothing to review gets no reply at
   all. A rule that copies the agent on every outgoing email sends mostly
   cover notes and scheduling, and a "could not find a document" reply to each
   is how the rule gets deleted.

4. **Reply in thread.** The agent keeps working on the same document. This is
   where an inbox-native mail provider earns its cost. **Half built.** A reply
   answering a question it asked ("Acme Holdings Ltd") and a reply undoing
   something ("no, leave clause 7", "undo everything") both work today and come
   back with a rebuilt copy. A free-form instruction ("also check the
   indemnity") gets a plain sentence saying instruction handling is not wired up
   yet, rather than silence or a wrong guess.

## Whole-message replies that need no document

The body is only the phrase, quoted history and signature stripped. Anything
longer goes to the agent as an instruction.

| You write | What happens |
| --- | --- |
| `help`, `what can you do` | The list of everything above, one line each. The same list rides once on your first review, and under the "attach a .docx" refusal |
| `setup` | How to make copying the agent a habit, with the honest note that Outlook's sent-mail rule can only Cc |
| `stop` | Nothing more on this thread until a new document arrives on it. Remembered, not just promised |
| `thanks`, `all accepted`, `done` (with or without a sign-off) | Nothing. A thank-you that earns a reply teaches you that every message costs another |
| `stop flagging X` | Confirmed in one line and remembered from your next review; `flag X again` lifts it. With an instruction in the same message ("stop flagging shall and fix clause 4") the instruction still runs, and the next review says, once, that it has stopped |
| `flag it again` | Lifts whatever the last review said it had stopped doing. A kind of change you undo often enough (five undos, mostly rejected) is no longer made for you, only pointed out, and the next review says so in one line, once: "I've stopped making date-format corrections for you, since you undid 5 of them" |
| `clean copy`, `accept all` | The current version with every tracked change accepted. Offered in the footer whenever changes were made. Any question still unanswered is named in its first line, so a copy that still says "thirty (13)" does not read as ready to send |
| `sig pages`, `signature pack`, `execution version`, `get this ready to sign` | The execution copy (every change accepted, comments and metadata out) and one signature page per party, each headed "Signature page to [Agreement] dated [date]": PDFs where LibreOffice is installed, Word files otherwise, and the reply says which. Signature blocks set in tables count. All or nothing: a drafting note, a blank, a highlighted bracket, a block signing on behalf of nobody, a date filled in for one party and not another, a DRAFT watermark or a question still open on the thread and nothing is attached; the reply lists what is in the way, clause first. Needs a verb aimed at us or a line to itself, so "attaching the execution version" is still a review |
| `30; 2; clean copy` | Several of these in one reply, separated by semicolons, new lines or "then": the undo first, then the answers, then the clean copy of the result, in one email back. The footer offers exactly this when answering its questions settles every blocker. Done only when every part is understood; an undo that is not certain stops the whole reply and asks |
| `B is fine`, `ignore B`, `B and D are fine` | Every point a review raises that is not a tracked change has a letter beside it (changes keep their numbers). Dismissed on the thread in one line back, "Noted: B is fine (…)", naming the point so a mistyped letter shows; a dismissed question is no longer "still open" in a clean copy. Five dismissals of one kind of point and the next review says, once, "I've stopped flagging … points for you". A letter that names nothing asks and does nothing, and "undo B" asks rather than reversing a change. Mentioned in the footer only when something is lettered |
| `undo`, `undo the X change`, `undo everything` | Reversed, and the copy rebuilt from the original |
| `connect`, `revoke` | Document-system access on and off |

## What the reply looks like

The reply has to be useful read on a phone, in the preview pane, without opening
the attachment. That means:

- **A slow review says so, once.** If the model is still working 90 seconds
  in, one line goes back in the same conversation: "Reviewing Acme NDA.docx;
  back in about 5 minutes." A duration, since the lawyer's time zone is not
  known. Never for a quick review, a follow-up, a clean copy, a comparison or
  a document already sent, and never after the verdict.
- **First line is a verdict.** "Not ready to send. 2 blockers." or "Ready to send.
  Nothing to flag. Clean draft." Nothing before it. No greeting, no "I've reviewed your
  document." It names a section that exists: "Send with care. 2 points worth
  your judgment" points at the heading "Worth your judgment".
- **Forty tracked changes are not forty lines.** Above eight, the mechanical
  ones are counted by kind ("23 defined-term corrections") and only the
  substantive ones are named, with "reply 'clean copy'" as the one motion that
  accepts them all.
- **Blockers next**, before anything else, with the consequence stated.
- **What I changed**, as a short list of titles.
- **What I flagged but did not change**, because it needs a human call.
- **One line telling you that replying works.**
- **Every reply it asks for is one tap.** In the HTML body, each option, the
  footer's "30; 4; clean copy", "clean copy" and "undo 2" beside each numbered
  change are plain mailto: links that open a reply with that text in it,
  addressed to `review+t<token>@` so it finds the conversation without an
  In-Reply-To. The token only ever resolves for the lawyer who owns the
  thread, and only for mail that authenticates. A bare "undo" is never a link.
  The plain-text body is unchanged.

The attachment is named `<original> (redline).docx` so it sorts next to the
original and is obviously not the original. It is a Word file whatever
arrived: a PDF's changes are in a Word copy built from its text, and the note
under the verdict says so rather than letting the layout imply otherwise.

## What the agent must never do

- Send anything to anyone except the person who emailed it. Not the client on
  the CC line, not anyone in the quoted thread. Ever.
- Silently change the document. Every edit is a tracked change.
- Return a file that will not open.
- Produce findings to look thorough. Every false positive costs the user more
  than it costs us.
- Give advice on whether to do the deal. It reviews the document, not the
  transaction.

## Known UX hazards

| Hazard | Mitigation |
| --- | --- |
| Signature images and logos read as the document | Filter by extension and size, never pick an image |
| Two documents attached | Ask, do not guess, unless exactly one is a .docx |
| A PDF, a .doc or a text file arrives | A Word copy is converted or built from it and the tracked changes go into that, named `<original> (redline).docx`; the reply says how the copy was made and that a rebuilt one has not kept the layout. A PDF also comes back annotated, a note per finding beside its text and the blockers highlighted. A scan is transcribed by the model, so it too gets a Word copy, with a note that the text is a transcription to check against the scan and that the mechanical checks did not run on it; where no model can be called, notes only, and the reply says so |
| The agent is CC'd on a real client send | Detect external recipients and warn in the reply |
| A 60-page credit agreement | Truncated at MAX_REVIEW_CHARACTERS, with the reply saying how far it got. Chunking is not built (DECISIONS O16) |
| Reply-all storms | The agent only ever replies to the sender, never reply-all |
| Someone emails it with no attachment | A one-sentence reply telling them what to attach |
