# Mail flow: what IT changes, and what it must not

The only part of the firm's estate IT has to touch is mail. Lawyers can use
Second Eye with no change at all, by forwarding a draft to the agent's
address. What a firm usually wants for a pilot is the **BCC habit**: every
message a pilot lawyer sends outside the firm with a document attached is
copied to the agent, which reads what went out and replies to that lawyer
alone. This page sets that up as a narrowly scoped server-side rule.

Steps marked **verify on your tenant** were written from the vendor's
documentation, linked, and have not been run on a real tenant by us.
Nothing here has been tested against a production Exchange, Mimecast or
Proofpoint configuration. Read and adapt before applying; run the
transport rule in audit mode first.

Placeholders: `review@legal.firm.com` is the agent's address; `firm.com` is
the firm's own domain; `SecondEye-Pilot` is a mail-enabled security group
holding the pilot lawyers.

## What the agent does with what it receives

So IT can judge the blast radius before turning anything on:

- It accepts mail only from `ALLOWED_SENDERS` (exact addresses or exact
  domains), silently drops anything else, and drops a message whose
  `Authentication-Results` says `dmarc=fail` (`architecture.md`).
- A BCC'd message with nothing attached gets **no reply**. A BCC'd message
  with a document gets a reply to the sender only, never to anyone the
  message was addressed to (`REPLY_POLICY=sender_only`, enforced twice). A
  document already sent to a client gets an "already sent" report, not a
  redline.
- Every accepted message counts toward `MAX_EMAILS_PER_DAY`, including the
  silent ones. Past the cap the Worker **rejects** the message, which the
  sending server reports as a bounce to the lawyer. Size the cap from the
  pilot group's real outbound volume before turning the rule on.
- Replies come from `review@legal.firm.com` with the firm's privilege legend
  and an `X-Privileged` header your DLP and journaling rules can match.

## Microsoft 365 / Exchange Online (recommended)

### Why a transport rule, and not lawyers' Outlook rules

A lawyer's own Outlook rule that forwards or redirects to an outside address
is **automatic external forwarding**, which Exchange Online blocks by default
in most tenants: the outbound spam policy's "Automatic - System-controlled"
setting changed to "Off - Forwarding is disabled" in 2021 for new
organisations and those not already using it, and a blocked message comes
back to the sender as `5.7.520 Access denied, Your organization does not
allow external forwarding` ([Microsoft Learn: control external email
forwarding](https://learn.microsoft.com/en-us/defender-office-365/outbound-spam-policies-external-email-forwarding),
read 2026-10-04). Opening external forwarding for every lawyer to make this
work would be the wrong trade. Microsoft's page names inbox rules and
mailbox (SMTP) forwarding as the forwarding that policy controls; a mail
flow rule's **Bcc** action is an admin rule, not on that list. **Verify on
your tenant** that the outbound spam policy does not act on the rule's copy.

### The rule

One mail flow rule, scoped to the pilot group, copying outbound mail with a
document attached:

| Setting | Value | Microsoft name (EAC / PowerShell) |
| --- | --- | --- |
| Apply if the sender is a member of | `SecondEye-Pilot` | The sender is a member of / `FromMemberOf` |
| And the recipient is located | Outside the organization | The recipient is located / `SentToScope NotInOrganization` |
| And any attachment's file extension includes | `docx doc pdf rtf odt` | Any attachment's file extension matches / `AttachmentExtensionMatchesWords` |
| Do the following | Add `review@legal.firm.com` to the Bcc box | Add recipients to the Bcc box / `BlindCopyTo` |
| Except if the subject includes | `[no review]` (the firm's opt-out word) | `ExceptIfSubjectContainsWords` |
| Except if any recipient address includes | `legal.firm.com` (no second copy when the lawyer writes to the agent directly) | `ExceptIfAnyOfRecipientAddressContainsWords` |
| Mode | Test (audit) first, then Enforce | `-Mode Audit`, then `Enforce` |

Sources for every name above:
[conditions and exceptions](https://learn.microsoft.com/en-us/exchange/security-and-compliance/mail-flow-rules/conditions-and-exceptions)
and [actions](https://learn.microsoft.com/en-us/exchange/security-and-compliance/mail-flow-rules/mail-flow-rule-actions)
(both read 2026-10-04).

In Exchange Online PowerShell (**verify on your tenant**):

```powershell
Connect-ExchangeOnline

# The pilot group, if it does not exist yet.
New-DistributionGroup -Name "SecondEye-Pilot" -Type Security
Add-DistributionGroupMember -Identity "SecondEye-Pilot" -Member jane@firm.com

New-TransportRule -Name "Second Eye: copy pilot outbound documents" `
  -FromMemberOf "SecondEye-Pilot" `
  -SentToScope NotInOrganization `
  -AttachmentExtensionMatchesWords "docx","doc","pdf","rtf","odt" `
  -BlindCopyTo "review@legal.firm.com" `
  -ExceptIfSubjectContainsWords "[no review]" `
  -ExceptIfAnyOfRecipientAddressContainsWords "legal.firm.com" `
  -Mode Audit

# After checking message trace for a few days:
Set-TransportRule -Identity "Second Eye: copy pilot outbound documents" -Mode Enforce
```

Notes, each from Microsoft's pages above unless marked:

- The Bcc target must be a recipient, not a distribution group. **Verify on
  your tenant** whether your tenant accepts the external address directly;
  if not, create a mail contact for it (`New-MailContact -Name "Redline
  Desk" -ExternalEmailAddress review@legal.firm.com`) and use that.
- Group membership in rules is cached: Microsoft says the cache refreshes
  about every three hours with no SLA, so a lawyer added to the pilot group
  may not be covered at once.
- Whether to keep the "recipient is outside the organization" condition is
  the firm's choice. Without it, internal drafts are copied too, which uses
  more of the daily cap; the agent's wrong-recipient checks work either way.
- Anything a lawyer sends while the rule is in audit mode is not copied.
  Message trace shows which messages would have matched.
- The rule changes nothing for the recipients: a Bcc recipient is not
  visible to them.

### Optional: force TLS to the agent

An outbound partner connector so every copy to the agent's domain goes over
TLS with the certificate checked (**verify on your tenant**; Microsoft:
[configure mail flow using connectors](https://learn.microsoft.com/en-us/exchange/mail-flow-best-practices/use-connectors-to-configure-mail-flow/use-connectors-to-configure-mail-flow)):

```powershell
New-OutboundConnector -Name "Second Eye (TLS required)" -ConnectorType Partner `
  -RecipientDomains "legal.firm.com" -UseMXRecord $true -TlsSettings DomainValidation `
  -TlsDomain "<the name on the certificate the MX presents>"
```

The `TlsDomain` value is the name on the certificate the MX hosts for your
subdomain present (`dig MX legal.firm.com`, then inspect the certificate):
we have not checked it, so start with `-TlsSettings EncryptionOnly` and
tighten once you have.

### Microsoft Purview: DLP, encryption and sensitivity labels

- **DLP policies** that block or encrypt mail to external recipients
  carrying sensitive information will act on the agent's copy too, because
  the agent's address is external. Add an exception for the recipient
  domain `legal.firm.com` to the policies that would otherwise block or
  encrypt it, or the copy bounces or arrives wrapped.
- **Office 365 Message Encryption** rules (for example "encrypt everything
  marked Privileged"): the agent receives a portal link, not the document,
  and cannot open it. Add `ExceptIfRecipientDomainIs legal.firm.com` to
  those rules, if policy allows.
- **Sensitivity labels with encryption (rights management):** a protected
  attachment can be opened only by the identities the label grants, and the
  agent is not one of them. Either leave labelled-and-encrypted documents
  out of the pilot, or decide label by label. Second Eye has not been
  tested with a rights-protected file and does not yet say "this file is
  protected" in so many words; expect a reply that it could not read the
  document.
- The `X-Privileged` header on every reply can drive your own retention or
  journaling label for the agent's replies.

### Inbound: the agent's replies

- The replies come from `review@legal.firm.com`, sent by Cloudflare's Email
  Service with SPF and DKIM set up on the subdomain only
  (`npx wrangler email sending enable legal.firm.com`; the records are
  printed by `npx wrangler email sending dns get legal.firm.com`). Publish a
  DMARC record for the subdomain (`_dmarc.legal.firm.com`), ideally
  `p=reject` once mail flows.
- Anti-impersonation features may treat `legal.firm.com` as a lookalike of
  `firm.com`. If replies are quarantined, add the subdomain to the trusted
  senders and domains of your anti-phishing policy (**verify on your
  tenant**).

### DNS

Only the agent's subdomain changes; the firm's own MX is untouched. The
subdomain needs an MX to Cloudflare Email Routing and the sending records
above, which means its DNS is managed in Cloudflare
(`docs/onboarding/README.md`, 2.11). Check `dig MX legal.firm.com` first:
enabling Email Routing takes over whatever mail that name already receives.
How a subdomain's DNS is put on Cloudflare (its own zone, or the parent
zone already there) depends on the firm's Cloudflare setup: **verify** with
whoever runs the firm's DNS.

## Mimecast

Mimecast sits between Exchange and the internet, so the copy to the agent
passes through it. **Verify every step on your tenant**; Mimecast's names
change between consoles.

- **Content examination / DLP:** a bypass policy scoped to the recipient
  `review@legal.firm.com` so the copy is not held for review or blocked
  ([content examination bypass](https://community.mimecast.com/s/article/email-security-cloud-gateway-content-examination-bypass-policy-configuration)).
- **Secure Messaging / encryption policies:** exclude the agent's address,
  or the agent receives a portal notification instead of the document
  ([Secure Messaging policies](https://mimecastsupport.zendesk.com/hc/en-us/articles/34000464071315-Secure-Messaging-Definitions-Policies)).
- **Attachment management:** make sure Word and PDF attachments to that
  recipient are not stripped, converted or replaced by a link.
- **TLS:** a secure delivery (enforced TLS) policy for the domain
  `legal.firm.com`.
- **Inbound:** if impersonation protection flags `legal.firm.com` as a
  lookalike of `firm.com`, add it to permitted senders for impersonation.

## Proofpoint

Same shape. **Verify every step on your tenant.**

- **Email DLP and encryption:** an exception for the recipient domain
  `legal.firm.com` in rules that block or encrypt
  ([Proofpoint Email DLP & Encryption](https://www.proofpoint.com/us/products/email-dlp-encryption)).
  Watch in particular for rules that encrypt on a keyword such as
  "Privileged" or "Confidential", which most legal drafts carry.
- **TLS:** require TLS to `legal.firm.com` (a TLS fallback rule that
  refuses rather than downgrades).
- **Inbound:** safelist the agent's address if replies are scored as
  impostor or quarantined.

## Google Workspace

In the Admin console, Apps → Google Workspace → Gmail → Routing, add a
routing setting that applies to **outbound** messages, limited to the pilot
group or organisational unit, with **Add more recipients** set to
`review@legal.firm.com`
([Add Gmail routing settings](https://support.google.com/a/answer/6297084)).
Google describes "Add more recipients" as delivering a copy to the added
address. Narrow it with the setting's envelope filter or a content
compliance condition for attachments if you want only documents. **Verify on
your tenant.**

## What IT must not do

- **Do not journal to it.** A journal rule captures inbound and internal
  mail too, in a journal-report wrapper. That is far more than the agent
  needs, most of it other people's mail, and it would count against the
  daily cap and reject real copies by mid-morning.
- **Do not open automatic external forwarding** for lawyers so their
  Outlook rules work. Use the scoped transport rule.
- **Do not use "Redirect the message to"** (`RedirectMessageTo`): it
  delivers to the agent **instead of** the real recipients.
- **Do not add the agent to To or Cc** by rule: the recipients would see it,
  and opposing counsel's reply-all would reach it (they are dropped
  silently, but they would learn the address exists).
- **Do not copy every mailbox.** Scope to the pilot group; shared mailboxes,
  service accounts and distribution lists stay out.
- **Do not add the agent's address to a distribution list**, and do not
  give it a mailbox in the tenant.
- **Do not put a public mail domain** (gmail.com and the like) on
  `ALLOWED_SENDERS`; `lra selftest` fails that.
- **Do not turn the rule to Enforce** before the daily cap is sized from the
  audit-mode message trace.

## Turning it off

Disable the transport rule (`Disable-TransportRule -Identity "Second Eye:
copy pilot outbound documents"`), or the routing setting in Google
Workspace. Copies stop on the firm's side once the change has propagated (Exchange
rule changes are not instant), with no change needed at Second Eye. The rest of the off-switches are in `incident-response.md`.
