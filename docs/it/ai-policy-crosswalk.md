# AI policy crosswalk

What the professional rules and clients' outside-counsel guidelines ask of a
firm using a generative-AI tool, mapped to the control in Second Eye that
meets each, and where the firm still has to act. The controls are settings
and commands that exist in the code today (file named); where one does not
exist, the row says so.

This is not legal advice on the rules; it is a map for the firm's general
counsel and risk committee to check against their own reading. Sources were
read on 2026-10-04 unless marked otherwise.

## The controls, named once

| Control | What it does | Where |
| --- | --- | --- |
| `REPLY_POLICY=sender_only` | Every reply goes to the lawyer who sent the message and nobody else, enforced in the container and again in the Worker | `src/secondeye/policy.py`, `cloudflare/src/outbox.ts`; DECISIONS 31 |
| `NO_AI_MATTERS` and "no AI for X" | A listed matter, client name or client domain gets no model call at all, only the deterministic checks; fails closed | `src/secondeye/policy.py`, `tests/test_no_ai.py` |
| Memory walls | Learned notes are kept per client or matter and read only on that client's matters; personal notes may hold no party, client, sum or proper name; the firm layer comes only from the approved playbook; an unidentified client gets no cross-matter memory; nothing proposed is used until the lawyer confirms it | `docs/memory.md`, `tests/test_memory_walls.py` |
| Audit trail and export | One content-free row per message: sender, matter, file names, what ran, model, session ids, inference geography, where stored and until when, reply recipients, outcome; `second-eye audit` or an admin's email | `src/secondeye/audit.py` |
| `second-eye purge` | Everything held for a client, matter or lawyer, Anthropic's sessions, files and memory stores included; dry run first; recorded | `src/secondeye/purge.py` |
| Retention | Raw message deleted once answered; job rows 24 h; conversations, closings and unconfirmed notes 7 days after last activity (`THREAD_RETENTION_DAYS`); Anthropic sessions deleted when the review ends; no record of kept or undone changes (`LEARN_FROM_OUTCOMES` off); archive off; a daily sweep | `docs/trust.md`, `src/secondeye/retention.py` |
| Human review | Output is tracked changes and comments in Word for the lawyer; nothing is accepted, filed or sent to anyone else | `docs/trust.md` |
| Firm-held key | Documents sealed with the firm's `DATA_KEY` before storage | `src/secondeye/crypto.py` |
| Privilege legend | `PRIVILEGE_NOTICE` on every reply and an `X-Privileged` header | `src/secondeye/policy.py` |
| Web search off | `WEB_SEARCH_ENABLED=false`; the sandbox reaches no host but package registries | `docs/sandbox.md` |
| No training | Anthropic: "Anthropic may not train models on Customer Content from Services" ([Commercial Terms, B](https://www.anthropic.com/legal/commercial-terms)) | `subprocessors.md` |

## ABA Formal Opinion 512 (July 2024)

[ABA Formal Opinion 512, Generative Artificial Intelligence Tools](https://www.americanbar.org/content/dam/aba/administrative/professional_responsibility/ethics-opinions/aba-formal-opinion-512.pdf).

| What it asks | Rule | Control | Left to the firm |
| --- | --- | --- | --- |
| Understand the tool's capabilities and limits well enough to use it competently | 1.1 | This pack; `docs/trust.md`; the reply says when a review was cut short or ran mechanical-only | Training the pilot lawyers |
| Assess the risk that information relating to the representation is disclosed, including to others in the firm and through the tool's use of it | 1.6 | Sender-only replies; memory walled by client; firm layer only from the approved playbook; one deployment per firm; firm-held key; no training | Per-matter ethical screens between lawyers of the same firm are **not built**: a client's memory is read on every review of that client's matters, whoever sends it |
| Informed client consent before inputting client information into a tool that can learn from it and disclose it in other matters; boilerplate in an engagement letter is not enough | 1.6 | The agent learns, so treat it as self-learning (`docs/trust.md`). What it learns from a client stays in that client's wall; the no-AI list lets a client that has not consented be excluded entirely | Whether and how to obtain consent: the firm's decision (`docs/onboarding/README.md`, 1.7) |
| Understand the vendor's terms, retention and security | 5.1, 5.3 | `subprocessors.md`, `architecture.md` (retention at Anthropic stated exactly: deleted when the review ends, under Anthropic's own up-to-30-day backend retention), `questionnaire.md` | Reading them and recording the assessment |
| Verify output before relying on it | 1.1, 3.1, 3.3 | Every change is a tracked change for a lawyer to accept or reject; every returned file is re-verified by code; the deterministic findings are evidence, not the model's word | The lawyer's review |
| Supervise: firm policies, training, and oversight of lawyers and non-lawyers using the tool | 5.1, 5.3 | The audit trail answers "who used it, on what, what ran, where the answer went" for any period | A written AI-use policy |
| Communicate with the client about use of the tool where relevant | 1.4 | The no-AI list; the audit export per matter (`second-eye audit --matter`) | The client conversation |
| Fees reflect time actually spent; no charge for learning the tool | 1.5 | — | Billing practice |

## The SRA and the Law Society (England and Wales)

The SRA's [Risk Outlook report: the use of artificial intelligence in the
legal market](https://www.sra.org.uk/sra/research-publications/artificial-intelligence-legal-market/)
(20 November 2023) says, among other things: "Firms must use AI in ways that
protect sensitive information"; "You cannot delegate accountability to an IT
team or external provider"; "Tell clients when you will be using AI with
their case, and how it will operate"; and "Supervise AI systems to ensure
they are working as expected and providing accurate results". The duty of
confidentiality is paragraph 6.3 of the SRA Code of Conduct for Solicitors.
The Law Society's [Generative AI: the essentials](https://www.lawsociety.org.uk/topics/ai-and-lawtech/generative-ai-the-essentials)
covers the same ground for practitioners, including data protection and
vendor due diligence; **we could not fetch it on 2026-10-04** (the site
refused automated access), so read it directly rather than through this
summary.

| What it asks | Control | Left to the firm |
| --- | --- | --- |
| Protect confidential information and client data in AI use | Sender-only replies; no-AI list; memory walls; firm-held key for documents; no training | D1 rows are not under the firm's key; the model runs in the US with no UK option, so a transfer assessment under UK GDPR (Anthropic's DPA includes the UK Addendum) |
| Accountability stays with the firm, not the vendor or IT | Every setting is the firm's, in a committed file; the audit trail is the firm's record | Naming the accountable partner |
| Tell clients when AI is used on their case and how | `docs/trust.md` is written to be shown to a client; the no-AI list for those who decline | The client communication |
| Supervise the system and check its results | Tracked changes for a lawyer to decide; output re-verified by code; audit trail; `second-eye selftest` for configuration | Spot-checks of output |
| Data protection (UK GDPR): DPIA, processor terms, transfers | `architecture.md` and `subprocessors.md` as DPIA inputs; Anthropic's DPA with SCCs and UK Addendum; D1 and R2 in the EU with `--jurisdiction eu` | The DPIA itself; **our DPA is not drafted** (`questionnaire.md`) |

## Bank and insurer outside-counsel guidelines

Most bank and insurer OCGs are private, so this maps the clauses that recur
in the published ones and in model security controls for outside counsel,
not any one client's text. In a September 2026 review of 1,054 public OCGs,
Fulkerson Advisors found 20 that mention AI: 12 require disclosure of AI
use, 9 bar client data from public AI tools, 8 require lawyer review, 3
require prior written consent, and 1 bans AI
([Fulkerson Advisors](https://www.fulkersonadvisors.com/research/ai-in-outside-counsel-guidelines)).
Security clauses commonly ask for encryption in transit and at rest, MFA,
approval of subcontractors, breach notice within 24 to 72 hours, and return
or destruction at matter end (summary of the ACC model controls:
[Morrison Mahoney](https://morrisonmahoney.com/robert-a-stern/269-acc-issues-cybersecurity-guidelines-for-outside-counsel/)).

| Typical clause | Control | Gap or firm action |
| --- | --- | --- |
| **No AI** on our matters | `NO_AI_MATTERS` with the client's domain (covers scans too) or matter numbers; "no AI for X" by an admin; a configured entry cannot be lifted by email; the reply says "Mechanical checks only" | List each such client before the pilot |
| **Prior written consent** to AI use | The no-AI list holds the client out until consent is recorded; lift it then | Recording consent is the firm's |
| **Disclosure** of AI use | Audit export by matter shows every job that ran "model review", with the model and date | The disclosure itself |
| **No client data in public or consumer AI tools; no training** | Commercial API under Anthropic's Commercial Terms (no training); no consumer product; web search off | — |
| **Lawyer review of AI output** | Tracked changes and comments only; nothing reaches a client without the lawyer sending it | — |
| **Confidentiality walls / need to know** | Memory walled by client; sender-only replies; per-lawyer conversations; DMS read with the lawyer's own permissions if connected | Per-matter screens between lawyers: **not built** |
| **Retention limits; return or destroy at matter end** | `second-eye purge --client` or `--matter`, Anthropic's copies included, with a dry run and a record; retention settings | No deletion certificate yet |
| **Data location** | D1 and R2 can be EU; `INFERENCE_GEO=us` pins one country | No EU or UK model processing |
| **Subcontractor approval** | `subprocessors.md` with a notice clause to agree | The clause is a template **[Founder decision]** |
| **Breach notice in 24-72 h** | `incident-response.md` template | Not yet in a signed agreement **[Founder decision]** |
| **Encryption in transit and at rest** | HTTPS between components; documents sealed with the firm's key | SMTP TLS is as negotiated unless the firm forces it (`mail-flow.md`); D1 rows not under the firm's key |
| **Audit rights / evidence of testing** | Audit trail; `second-eye selftest` report; this pack | No third-party pen test or certification yet |
| **Zero data retention** at the model provider | `ZERO_RETENTION` for the model calls only; Managed Agents is not ZDR-eligible | A client that requires ZDR goes on the no-AI list |
