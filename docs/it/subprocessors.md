# Sub-processors

Who besides Second Eye touches a firm's data or its deployment, what each
receives, where, and what independent assurance each publishes. Vendor
claims are linked to the vendor's own page with the date it was read; nothing
here is a certification of Second Eye, which holds none
(`questionnaire.md`, "Governance").

Last checked: 2026-10-04.

## Whose contract

A firm can run its deployment in **its own** Cloudflare account and **its
own** Anthropic organisation (`lra tenant new --account-id`, and the firm's
API key in `deployments/<firm>/.env`). Then Cloudflare and Anthropic are the
firm's own vendors under the firm's own agreements, and the firm can assess
them as it already assesses any cloud provider. Otherwise they run in our
accounts and are our sub-processors. Which of the two is a **[Founder
decision]** to agree with each firm (`docs/onboarding/README.md`, 1.1).

## The list

| Sub-processor | Role | What it receives | Where | Assurance (vendor's own pages) |
| --- | --- | --- | --- | --- |
| **Cloudflare, Inc.** | Mail in and out, compute, storage | Every message sent to the agent (body, attachments, headers); the reply; documents (sealed under the firm's key before they are stored); D1 rows in plaintext (senders, filenames, findings, the text of each tracked change, remembered preferences, the audit trail); Workflow step state (filenames, a short notice); logs | Worker and Email Routing at Cloudflare's edge. D1 and R2 in the EU if created with `--jurisdiction eu`, otherwise Cloudflare's default. Container placement not verified (`architecture.md`) | SOC 2 Type II; in-scope products listed on the [SOC 2 FAQ](https://www.cloudflare.com/trust-hub/compliance-resources/soc-2/) include Workers, D1, R2, Durable Objects and Queues. ISO 27001, 27701 and 27018 ([ISO certifications](https://www.cloudflare.com/trust-hub/compliance-resources/iso-certifications/)). **Not listed** in the SOC 2 product scope as read on 2026-10-04: Containers, Workflows, Email Routing and the Email Service. Ask Cloudflare for current scope. Reports are under NDA from the Cloudflare dashboard. Sub-processors: [cloudflare.com/gdpr/subprocessors](https://www.cloudflare.com/gdpr/subprocessors/) |
| **Anthropic** (the contracting entity is in the firm's or our agreement) | The model and the review sandbox (Claude Managed Agents) | The document under review (mounted in a session's sandbox), the email's instructions, the deterministic checks' findings, memory notes for this lawyer, client and firm, and what the email says (which can include the names and addresses it was sent to). Never a DMS token or our credentials | US. Workspace geography is US only; inference `us` or global ([Data residency](https://platform.claude.com/docs/en/manage-claude/data-residency)) | SOC 2 Type I and Type II, ISO 27001:2022, ISO/IEC 42001:2023 ([What certifications has Anthropic obtained](https://privacy.claude.com/en/articles/10015870-what-certifications-has-anthropic-obtained)); reports via the [Trust Portal](https://trust.anthropic.com/). No training on customer content: "Anthropic may not train models on Customer Content from Services" ([Commercial Terms, B](https://www.anthropic.com/legal/commercial-terms)). DPA with SCCs (Modules 2 and 3) and the UK Addendum, 48-hour breach notice, 15 days to object to a new sub-processor ([DPA](https://www.anthropic.com/legal/data-processing-addendum)). Retention: [API and data retention](https://platform.claude.com/docs/en/manage-claude/api-and-data-retention) and `architecture.md` |
| **GitHub, Inc.** | Source code and deploys; **no firm documents** | The code; each firm's `tenant.jsonc` (allowlisted addresses, admins, the agent address, and any client names or matter numbers on `NO_AI_MATTERS`); the per-firm Cloudflare API token as an Actions secret, used by `deploy.yml` | GitHub's hosting; the repository is private | SOC 1 Type 2, SOC 2 Type 2 and ISO/IEC 27001:2022 for GitHub Enterprise Cloud ([GitHub Trust Center](https://ghec.github.trust.page/); [SOC reports announcement](https://github.blog/changelog/2024-12-06-the-latest-github-and-github-copilot-soc-reports-are-now-available/)). The repository is on a plan that does not offer branch protection for private repositories (checked 2026-10-04). **Not verified:** whether the reports cover the plan this repository is on |

Not sub-processors, but on the path:

- **The firm's own mail stack** (Exchange Online, Google Workspace,
  Mimecast, Proofpoint) carries the message to Cloudflare. It is the firm's.
- **The firm's document system**, only if connected (off by default), is
  read with each lawyer's own OAuth token (`docs/oauth.md`).
- **Package registries** (PyPI and the like) can be reached from Anthropic's
  sandbox, which has package managers allowed and no other host
  (`docs/sandbox.md`). A lookup names a package; it carries no document.

## Change notice: a commitment to agree

Written as a clause the founder can sign, with the blanks the firm and the
founder fill. **[Founder decision]**: the notice period, the channel and the
remedy. Anthropic gives its own customers "reasonable notice" and fifteen
days to object (DPA, C.3); thirty days is offered here so a firm has time
to run its own vendor review.

> **Sub-processor changes.** Second Eye will give the Customer at least
> **[30]** days' written notice, by email to **[the Customer's named
> contact]**, before any new sub-processor receives Customer Data, or before
> an existing sub-processor receives a new category of Customer Data or
> processes it in a new country. The notice will name the sub-processor,
> what it will receive, where, and why. The Customer may object in writing
> within the notice period on reasonable data-protection grounds; if the
> parties cannot resolve the objection, the Customer may terminate the
> affected service without penalty, and Second Eye will delete the
> Customer's data under the offboarding procedure (`incident-response.md`).
> The current list is `docs/it/subprocessors.md` in the version supplied to
> the Customer, and every change to it is dated.
>
> Where the Customer runs the deployment in its own Cloudflare account and
> Anthropic organisation, those vendors are the Customer's own, and this
> clause covers only any vendor Second Eye adds to the software's path.

The same applies to a change in what we send an existing sub-processor that
is not already in the table above, for example a new Cloudflare product in
the mail path or a new Anthropic feature that stores data.
