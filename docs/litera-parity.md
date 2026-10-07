# What Litera does, and where we have to be different

Litera is the incumbent. Over 75% of the AmLaw 250 use DocXtools, and any firm
evaluating this product will ask how it compares, so the comparison is designed
for rather than discovered later.

This page was first written against one product, Litera Check. That stopped
being the right comparison in July 2026, when Litera relaunched the whole suite
around a single agent, Lito, which runs in Word, Outlook, the browser and iOS.
So the scope here is now the three Litera products that act at the moment a
document is about to be sent, because that is the only moment this product
exists for: **Check**, **Compare** and **Clean**. The rest of the suite is
covered at the end, mostly to say why it is out of scope.

## Check: proofreading and repair

Contract Companion, DocXtools, Litigation Companion and Best Authority,
consolidated into one Word add-in.

| Litera Check | Ours |
| --- | --- |
| Defined terms: unused, used before defined, inconsistent case | `checks.defined_terms`, `checks.term_capitalization`, `checks.term_misspellings` |
| Defined terms: **undefined** -- capitalized, looks defined, never is | Deliberately the agent's job. See below |
| Cross-references to sections that do not exist | `checks.cross_references` |
| Numbering: skips, duplicates, broken sequences | `checks.numbering` |
| Amounts: words disagreeing with numerals | `checks.amounts` |
| Dates: inconsistency and contradiction | `checks.date_consistency`, `checks.contradictory_dates` |
| Names: the same entity written differently | `checks.party_names` |
| Placeholders and blanks | `checks.placeholders` |
| Signature page: every party has a block, nobody signs who is not a party, no block half filled | `checks.signature_blocks`. Questions, never edits: who signs is not ours to decide |
| Schedules and exhibits referred to but not attached | `email_checks.missing_annexes`. Judged against the whole email, so a schedule sent as its own attachment or promised in the covering note is not "missing". Execution versions only |
| Fonts, styles and legacy formatting repaired in one click | `repair.py`. Done by the associate agent in its sandbox, proved locally before it is sent. Needs the agents configured (`MANAGED_*_ID`) |
| Numbering repair | Detected, not repaired. Automatic numbering is the one thing the repair gate cannot see, so it is deliberately left alone |
| Punctuation, spacing, character-level editing mistakes | `checks.punctuation`. A repeated word, a space on the wrong side of a mark and a missing space after a comma are written as tracked changes; double spaces and a stray straight quote among curly ones are counted, not written, because a whitespace-only tracked change reads as noise |
| Citations and tables of authorities | Not built. Litigation-specific, and out of the agreed scope |
| Spelling and grammar | Not built. Word already does this |
| "Document health" in one click | The verdict line |
| Tracked changes on a PDF, a legacy .doc, an RTF or a text file | `reflow.to_docx`. A Word copy is converted (LibreOffice, locally) or built from the text, and the redline is written into that. A PDF also comes back annotated (`annotate.py`): a note per finding beside its text, blockers highlighted. Litera Check works inside Word only |

**The one row we answer differently: undefined defined terms.** A legal
document is full of capitalized phrases that are correctly undefined: statutes,
courts, jurisdictions, job titles, party names, `Schedule 2`. A rule that
guessed would fire on nearly every document, and `PRODUCT.md` is unambiguous
that a check which runs on everything and is sometimes wrong costs the habit,
which is the whole product. So it is the agent's job, the table says so rather
than claiming parity we do not have, and the agent is never told the ground is
covered, because a model that believes it is stops looking.

## Compare: what changed between two versions

| Litera Compare | Ours |
| --- | --- |
| Word against Word, as a redline | `compare.compare`. Real tracked changes in the earlier version, written by the same writer as every other edit |
| Cross-format compare: Word against the PDF the other side returned | `versions.handle_comparison` with `reflow.to_docx`. The PDF is rebuilt as Word from its text and the comparison is tracked changes, proved the same way; the reply says which side was rebuilt |
| What changed since the version you reviewed, without asking | Ours only: `handler._since_last_time`. A second review of a document leads with the changes since the last one and attaches the comparison. Litera compares when asked |
| Every change caught | Proved per document, not asserted: the output is re-read and must give the later version on accept-all and the earlier on reject-all, or it is not attached |
| Moved text | Detected and reported as one change, including a clause reworded on the way. Written as a deletion and an insertion, not as Word's move markup |
| Tables, headers, footers, footnotes | Words in all of them. A table that gains or loses rows is reported, not marked up |
| Change report with counts | The reply email. With the blackline agent (`blackline.py`, `skills/lra-blackline`), Change-Pro's summary as well: insertions, deletions and moves by clause, then each material change with before and after and the model's one line on why it matters, as page 1 of the PDF and as a short table in the email. The counts are the comparison's; only the lines are the model's |
| PDF blackline to send the client | `pipeline/blackline_pdf.py`, run in the sandbox by the skill: insertions underlined blue, deletions struck red, moves double-underlined green, a bar in the margin by every changed line, "Blackline: <new> against <old>" on every page and "Page x of y". Printed through LibreOffice (the document's own layout) where it is installed and its pages show every change; otherwise typeset from the Word comparison's own markup, so it cannot disagree with it. The host re-reads it before attaching: it opens, has the summary and the counts, and shows every changed word of both versions |
| PDF, and PDF against Word | A Word copy is built from the PDF's text (`reflow.to_docx`) and the comparison is real tracked changes, proved the same way. The reply says which side was rebuilt. A scan, a deck or a workbook falls back to `compare.compare_text`: a change list, no marked-up copy |
| Scanned PDFs, by OCR | The model transcribes the pages (`extract._transcribe`), no OCR engine: the owner's decision is that reading goes through the model. The transcription builds the Word copy for the redline and the comparison; the deterministic checks deliberately do not run on it, because they would report its slips as the document's. Up to 60 pages |
| Excel and PowerPoint | Text only, through the same change list |
| Images and embedded objects | Not compared. The reply says so when their number differs |
| Formatting changes | Not compared, and every reply says so |
| Compare Outlook attachments | This is the product: attach two versions and say "compare" |
| Compare against the last version without attaching it | "Compare this to the last version", from the archive, or from the lawyer's own conversations when the archive is off. Litera needs both files |
| Cumulative and named baselines | "Blackline against what we sent on Tuesday", "against v2", "against the original", "v4 against v1": every version a conversation has held is kept (`blackline.keep_previous`), with what we sent (the negotiation ledger's copies) and our redline as returned; `skills/lra-blackline/scripts/pick_baseline.py` lists them against the lawyer's words and the model chooses when the words fit more than one. The reply names both versions and their dates. Litera needs both files |
| Lito: which changes carry risk, their impact, suggested rewrites (launching late September 2026) | `compare.explain`. Shipped |
| Lito: grounded in firm precedent (their Q4) | Not yet. The document-system tools exist and are not wired into the comparison |

## Clean: metadata and recipients

| Litera Clean / Metadact | Ours |
| --- | --- |
| Remove tracked changes, comments, hidden text | `clean.clean`, with every removal listed in the reply |
| Author, company, template, editing time, custom properties | Removed and listed. Also revision-session ids and document variables |
| 300+ metadata types across every Office format and PDF | Word and PDF (`clean_pdf.clean`: properties, XMP, comments and mark-up, embedded files, scripts, private data; links and form values kept). A password-protected, signed or part-redacted PDF, a deck or a workbook is declined in a sentence |
| Proof the words did not change | Ours only. The clean copy is checked against an independent reading of the original before it is sent; a PDF's every page is read before and after, and every object in the copy checked for anything that should have gone |
| Runs silently on every outbound email, server-side | Not possible from here: we are a recipient, not the gateway. Ours is asked for |
| Recipient risk: personal domains | `email_checks.own_personal_address`, narrowed to the sender's own webmail so that a client on gmail.com is not flagged |
| Recipient risk: reply-all when BCC'd | Not observable from a mailbox. Genuinely theirs |
| Block or quarantine | Not possible and not wanted. We are not in the delivery path, so we can only advise |
| The document names one company and the email goes to another | Ours only: `email_checks.recipient_mismatch` |
| The covering email claims something the document does not do | Ours only: `email_checks.claim_mismatch` |

## Where this leaves us

**On the send-moment tools we are level or ahead except in three places:**
file-format breadth in Clean, OCR in Compare, and running on every email
without being asked. The third is structural. Litera sits in the mail gateway
and we are a name in the address book; the BCC habit is our answer to it, and
it is a weaker one.

**The old claim that they do mechanics and we do judgment is dead.** It was
true of a Word add-in. Lito reviews against playbooks, flags non-standard
language and assesses the risk of changes. What is still only ours:

1. **The reply is the interface.** A one-word answer becomes a tracked change,
   "undo that" reverses one edit, a plain-English instruction is carried out,
   "compare this to the last one" needs no second file. Lito is a pane to open
   and operate. Nothing here is installed, and the whole conversation happens
   in the thread the lawyer was already in.
2. **We see the email, not just the document.** Wrong recipient, wrong
   attachment, a covering note that misdescribes the draft. A tool that lives
   in Word cannot see these and a gateway tool does not read the document.
3. **Proof, per document.** The comparison, the clean copy and the formatting
   repair each re-read their own output and refuse to send it if it does not
   do exactly what the email says it does. This is cheap for us because
   everything is a file in and a file out, and it is the answer to "why would
   I trust it".
4. **It learns from the lawyer.** An undo is recorded as a rejection, "stop
   flagging that" is remembered, and a kind of change refused often enough is
   no longer made. The version actually sent, when the agent is BCC'd, is
   read against our redline and each change scored kept, rejected or
   rewritten (`reconcile.py`).

## The rest of the suite, and why it is not here

| Litera | Position |
| --- | --- |
| Lito playbook review | `skills/lra-playbook`: one file per clause type with position, fallback, walk-away and model wording; the review reports on-position / at-fallback / off-playbook and proposes the fallback as a tracked change where it fits verbatim. Ships with starter positions that say they are starters. `docs/playbook.md` |
| Lito precedent search and first drafts | The document-system adapter and delegated OAuth exist and have never met a real instance. ROADMAP items 20 and 21 |
| Lito Studio: no-code custom skills | The nearest thing is `skills/lra-document-tools`, an Anthropic Agent Skill. It is for us, not for a knowledge-management team |
| Create: templates and clause libraries | Out of scope |
| Kira: due diligence at volume | Excluded in ROADMAP. Email is one document at a time. The single-document version exists: `skills/lra-key-terms` answers a fixed question set as a table, every cell quoted from the document, and writes a one-page summary |
| Transact: closings and signature pages | `skills/lra-closing`, run by its own agent: signature packets per signatory across several documents, signed pages returned as scans or photos matched to document and party and checked signed and current, a running tally, the executed set with a closing index, and the closing checklist. Never run live. `docs/closing.md` |
| Foundation: CRM, experience, finance | Not this product |
| pdfDocs: redaction and binders | Redaction is ROADMAP item 10 |
| Mobile app | Email already is one |

## The honest positioning

Not "better than Litera at proofreading", and no longer "they do mechanics, we
do judgment". Level with them on the three tools that matter at the moment of
sending, provably correct where they are asserted correct, and reachable by
forwarding an email where they need software on every desktop. A firm that
already owns Litera should still want this, because the moment it helps is the
moment a lawyer is in their mail client about to hit send.

## Sources

- https://www.litera.com/litera-products-a-to-z
- https://www.litera.com/newslinks/litera-relaunches-unite-practice-and-business-law
- https://www.lawnext.com/2026/08/litera-connects-contract-drafting-and-negotiation-in-one-trusted-legal-ai-workflow.html
- https://www.litera.com/litera-one
- https://www.litera.com/products/litera-compare
- https://info.litera.com/rs/046-QLX-552/images/Litera-Compare.pdf
- https://www.litera.com/products/metadact
- https://www.litera.com/products/clean
- https://www.litera.com/products/contract-companion
- https://www.litera.com/products/docxtools
- https://directory.lawnext.com/products/litera-check/
