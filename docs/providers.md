# Email infrastructure options

Researched September 2026. The decision here is mostly reversible: everything
above `src/secondeye/mail/base.py` is vendor-agnostic, so this is a one-file swap.

## The actual distinction

Most transactional email APIs solve *sending*. Receiving is an afterthought:
they parse an inbound message into a webhook POST and forget it. No inbox, no
thread state, no history. That is fine for "reply to this notification" and bad
for an agent that has to hold a conversation across hours and revisions.

A newer category gives the agent a real inbox with threads, labels, and search.
That matters here because the product is explicitly conversational: the user
replies "no, keep clause 7 as it was" and the agent needs the prior state.

## Candidates

### AgentMail
Purpose-built inbox API for AI agents. Y Combinator company, raised $6M seed in
March 2026 led by General Catalyst. Create an inbox with one API call, API-key
auth rather than OAuth, webhooks and websockets for real-time delivery, native
threading, labeling, and full-text search. Usage-based pricing rather than
per-inbox monthly fees. Reports 10M+ emails processed.

- **For**: the only option where "inbox" is a first-class object. Per-user or
  per-matter inboxes become cheap, which unlocks a lot of the UX below.
- **Against**: youngest vendor here. Deliverability reputation is unproven
  relative to Postmark. Attachment handling needs verification at our sizes.
- **Verdict**: default choice for the prototype.

### Postmark
Mature transactional provider with structured inbound parsing built in. Inbound
attachments cap at 35 MB cumulative. Inbound messages retained 45 days. Roughly
$15-16.50/month for 10,000 emails.

- **For**: best-in-class deliverability reputation. Boring in the good way.
- **Against**: no inbox model, no thread state. We would rebuild threading from
  `In-Reply-To` and `References` ourselves. Inbound webhooks are not signed, so
  the endpoint needs a secret path plus basic auth.
- **Verdict**: the fallback, and the likely answer if a client demands a vendor
  with a long compliance track record.

### Mailgun
Routing rules and inbound parsing to UTF-8 JSON, plus address validation.
Strong if inbound routing logic gets complicated (per-matter addresses, catch-all
patterns).

- **Verdict**: hold in reserve. Its edge is routing, which we do not need yet.

### Resend
Inbound parsing shipped November 2025 as a webhook, 30-day retention. No inbox,
no thread model, no per-agent provisioning. Free to 3,000 emails, $20/month Pro.

- **Verdict**: excellent send-side developer experience, wrong shape for this.

### Cloudflare Email Routing + Email Workers
Inbound mail hits a Worker directly. 25 MiB max inbound message size. Attachments
parseable from the Worker. Free tier exists; complex handlers can exceed CPU
limits on the free Workers plan.

- **For**: Jim already runs DNS and hosting on Cloudflare, so the domain and
  routing are already there. Cheapest possible path to a working address.
- **Against**: a Worker is the wrong place to run a multi-second model call and
  a document rewrite. It would be a thin forwarder to this service, which means
  running two things instead of one.
- **Verdict**: strong candidate specifically for *receiving* while sending goes
  out through a reputable provider. Worth pricing as a hybrid.

### Amazon SES
Cheapest at volume, most assembly required. Inbound to S3 plus SNS plus Lambda.

- **Verdict**: the answer at scale, not the answer at prototype.

## Recommendation

Build on AgentMail. Keep the Postmark adapter compiling as insurance. Revisit
once there is a real deliverability requirement or a client security review.

## Sources

- https://docs.agentmail.to/introduction
- https://www.agentmail.to/blog/best-email-api-for-ai-agents-2026
- https://techcrunch.com/2026/03/10/agentmail-raises-6m-to-build-an-email-service-for-ai-agents/
- https://postmarkapp.com/support/article/1056-what-are-the-attachment-and-email-size-limits
- https://postmarkapp.com/developer/webhooks/inbound-webhook
- https://developers.cloudflare.com/email-service/platform/limits/
- https://mailtrap.io/blog/best-email-api-for-ai-agents/
