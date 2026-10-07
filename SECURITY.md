# Security

Redline Desk reads privileged legal documents and replies by email, so a
security problem in it can expose a client's confidences. Thank you for
reporting one privately.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. The report reaches
the maintainers only, and we can discuss it and agree a fix and a disclosure
date there.

Please do not open a public issue, pull request or discussion about a
vulnerability before it is fixed.

Include what you can: the component, the version or commit, the steps to
reproduce, and what an attacker gains. Use synthetic documents and
addresses only. **Never send us a real client document or email**, even one
that demonstrates the problem.

We aim to acknowledge a report within three working days and to tell you
our assessment and plan within ten. This is a small project, not a security
team; we will say so if a fix will take longer, and keep you informed.

## In scope

- **The Worker edge** (`cloudflare/`): admission of inbound mail, the
  allowlist, the daily cap, the kill switch, the outbox and its single
  recipient, the `/internal` endpoints between the container and the Worker,
  the container's egress allowlist, and the Anthropic webhook.
- **Sender authentication**: getting mail admitted without passing DMARC (or
  DKIM for a domain in `DKIM_ONLY_DOMAINS`), forging an allowlisted sender,
  or making a reply go to anyone but the authenticated sender.
- **The container** (`src/lra`, `Dockerfile`): parsing hostile attachments
  (Word, PDF, zip bombs, legacy formats through LibreOffice), prompt
  injection from a document or a covering email that changes who receives a
  reply, what is stored or what a session can reach, and file verification
  before a document is attached.
- **Data handling**: encryption at rest under the firm's key (`crypto.py`,
  `blobs.py`), the memory walls between clients, the document-system tokens,
  and anything that reaches Anthropic or a third party that `docs/trust.md`
  says does not.
- **Retention and purge**: anything kept longer than `docs/trust.md` says,
  or left behind by `lra purge` or `lra tenant offboard`, here or at
  Anthropic.
- **The document-system MCP server** (`cloudflare/dms-mcp/`) and the
  per-lawyer OAuth flow (`oauth.py`, `consent.py`): reaching a matter the
  requesting lawyer cannot open.

Out of scope: vulnerabilities in Cloudflare's or Anthropic's platforms
themselves (report those to them), findings that need control of the firm's
Cloudflare account or `DATA_KEY`, denial of service by volume, and the
model's legal judgement being wrong (that is a bug report, not a security
report, unless it is caused by injected instructions).

## Supported versions

Security fixes are made on `main` and released in the next tagged release.
Only the latest release is supported; a firm running its own deployment
(`docs/it/self-hosted.md`) should upgrade to it.

## More

- `docs/trust.md`: where a document goes, who processes it, what is kept and
  for how long, and what we do not have yet.
- `docs/it/README.md`: the IT pack for a firm's security review, including
  the architecture, the sub-processors, a pre-filled questionnaire, the
  incident runbook, and `lra selftest`, which checks a deployment's security
  settings and prints a dated report.
