# Running Second Eye in your own accounts

For a firm's IT team. This is an option, not the default. By default we host
a separate deployment for each firm: its own Worker, database, storage and
keys, shared with nobody, run by us. Choose this option if your security
policy says client documents may only pass through accounts your firm owns.

With this option, Second Eye runs entirely in your own Cloudflare account
and your own Anthropic organisation. We write the software and publish
releases. You decide which release to deploy and when, and you hold every
credential. We have no standing access to anything.

Last checked against the code: 2026-10-04. Everything below that runs a
command has been tested against fakes only. None of it has run against a
real Cloudflare account or Anthropic organisation yet. The section "What has
and has not been tested" at the end lists what that means.

## What we can and cannot see

| | Hosted by us (the default) | Self-hosted (this page) |
| --- | --- | --- |
| Whose Cloudflare account | Ours | Yours |
| Whose Anthropic organisation and contract | Yours (the key is yours either way) | Yours |
| Who deploys a new version | Us, on every change that passes our tests | You, a release you choose |
| Who holds `DATA_KEY`, the key to stored documents | You and us | You alone |
| Our access to your Cloudflare account | Full, because we run it | None. Only a token you create, scoped and expiring, which you can delete at any time |
| Our access to your Anthropic organisation | The API key you gave us | None, unless you create a key for us and later delete it |
| Our access to your documents | Possible, because we run the system | None. Documents are sealed under `DATA_KEY`. Even a support token never includes R2 storage |
| Turning it off | Ask us, or use the controls below | You, in seconds, with no deploy and no vendor involved |
| Proof everything was deleted when you leave | A certificate from us | A certificate you generate and sign with your own key |

Your sub-processors are Cloudflare and Anthropic, under your own contracts
with them. We are not a processor of your data, because we never receive it.

There is no back door. There is no admin endpoint, no key of ours on your
Worker, no account of ours in your Cloudflare or Anthropic organisation, and
no telemetry sent to us. The code is in the release for you to read.

## What you need

**A Cloudflare account on the Workers Paid plan** (US$5 a month plus usage).
The plan is required: on the free plan, Containers and Email Sending fail
with unhelpful errors. You will use Workers, Containers, Workflows, D1, R2,
and Email Routing and Email Sending on a subdomain you control, such as
`legal.firm.com`. Check that these products are in scope of the Cloudflare
certifications your policy relies on. Newer products may not be in scope yet,
and we have not checked.

**An Anthropic organisation** (Claude Console), with credit, and one workspace
used only for Second Eye. In that workspace:

- **Data retention.** The review model, Claude Fable 5.1, needs 30-day data
  retention turned on. Alternatively, use zero data retention and set
  `ZERO_RETENTION=true` in your tenant file. That covers the model but not
  the sandbox.
- **A monthly spend limit** on the workspace.
- **Customer-managed keys (CMEK), recommended.** Anthropic lets you encrypt
  workspace data at rest under a key in your own AWS KMS, Google Cloud KMS
  or Azure Key Vault. The key covers "Claude Managed Agents (beta) data,
  including agent configurations, environments, webhooks, sessions and their
  events, memory stores", as well as Files API uploads and Skills data. It is
  set per workspace on Claude Platform. Note these limits:
  - It is turned on through your Anthropic account team, not in the Console
    alone.
  - It is US regions only.
  - It only protects data written after the key takes effect. Attach the key
    to the new workspace **before** the first request.
  - It is irreversible. The key cannot be detached or swapped.
  - It locks the workspace's 30-day retention setting.
  - If the key is revoked, everything in the workspace becomes permanently
    unreadable. The revocation takes up to an hour to apply.
  - It does not cover vault credential values. Those are only used if you
    connect a document system.

  Source: [Customer-managed encryption keys](https://platform.claude.com/docs/en/manage-claude/cmek)
  (read 2026-10-04). Azure Key Vault suits a Microsoft 365 firm.

**A machine to deploy from** with Python 3.12, Node 22 and a running Docker
engine. Wrangler builds the container image locally and pushes it into your
account's registry. If your desktops cannot run Docker, run the same
commands in your own CI runner, for example GitHub Actions in your own
organisation.

**The deploy token.** In the Cloudflare dashboard, go to My Profile -> API
Tokens -> Create Token. Start from the "Edit Cloudflare Workers" template.
Add these account permissions: Containers (Write), D1 (Write), Workers R2
Storage (Write), and Workflows if it is listed separately. Under Account
resources, include **your account only**. Set a TTL if your policy wants one.
This is the minimum our own deployment uses. We have not tested a smaller
set.

## 1. Download and check a release

Releases are on the project's GitHub releases page, named `vX.Y.Z`. Each one
contains:

| File | What it is |
| --- | --- |
| `second-eye-vX.Y.Z-source.tar.gz` | The code at that tag. It excludes every firm's deployment and replaces our own names with blanks. A `RELEASE` file inside says which tag it is. It is also the container's build context. |
| `second-eye-vX.Y.Z-worker.tar.gz` | The Worker as wrangler bundles it, for you to read |
| `second-eye-vX.Y.Z-wrangler.template.jsonc` | The Cloudflare config your deployment will have, with placeholders for your names |
| `second-eye-vX.Y.Z-requirements.txt` | The Python dependencies as they resolved when the release was built |
| `second-eye-vX.Y.Z-sbom.cdx.json` | A CycloneDX software bill of materials (syft) |
| `release.json` | What is in the release, and what is not |
| `SHA256SUMS` | Checksums of everything above |

```bash
sha256sum -c SHA256SUMS          # every line must say OK
tar xzf second-eye-vX.Y.Z-source.tar.gz
cd second-eye-vX.Y.Z
python3.12 -m venv .venv && .venv/bin/pip install -e .
```

What the checksums prove, and what they do not:

- **They prove** that the files are the ones the release lists.
- **They do not prove who listed them.** That evidence is the tag and the
  GitHub release it is attached to.
- **The source archive is reproducible.** Anyone can rebuild it from the tag
  with `python -m lra.release build --version vX.Y.Z --out dist` and compare
  the checksum.
- **The container image is not shipped prebuilt.** It is built on your
  machine from the `Dockerfile`. Its Debian packages and Python dependencies
  are whatever resolve at that moment. `requirements.txt` is there so you
  can compare what you got with what we got.

## 2. Your tenant file

Your deployment is described by one file, `deployments/<firm>/tenant.jsonc`.
It holds names, ids and settings, and no secrets. We can prepare it with you,
or you can create it yourself:

```bash
.venv/bin/lra tenant new acme-llp --self-hosted \
  --account-id <your 32-character Cloudflare account id> \
  --anthropic-org <org id> --anthropic-workspace <workspace id> \
  --address review@legal.acme.com --domains acme.com \
  --contact gc@acme.com --admins partner@acme.com [--jurisdiction eu]
```

Read the settings it wrote, such as `ALLOWED_SENDERS`, `NO_AI_MATTERS`,
`MAX_EMAILS_PER_DAY` and the retention settings. Then run
`.venv/bin/lra tenant render acme-llp` and keep the `deployments/acme-llp/`
folder. You will copy it into each new release. Because the tenant is
marked self-hosted, our deployment pipeline never deploys it, even if the
file is in our repository.

Put your Anthropic key where only this machine sees it. Git ignores this file:

```bash
echo "ANTHROPIC_API_KEY=sk-ant-..." >> deployments/acme-llp/.env
chmod 600 deployments/acme-llp/.env
```

## 3. Create the resources (once)

```bash
.venv/bin/lra tenant provision acme-llp            # a dry run: every command, nothing created
export CLOUDFLARE_API_TOKEN=<your deploy token>
.venv/bin/lra tenant provision acme-llp --apply
```

`--apply` does the following, in order:

1. Creates the D1 database.
2. Creates the R2 bucket, with rules that delete unreviewed mail and job
   state after 7 days.
3. Deploys the Worker, without its container yet.
4. Generates `DATA_KEY` and `EDGE_SECRET` and puts them on the Worker.
5. Puts your Anthropic key on the Worker.

It then prints what is left to do by hand. In order:

1. **Back up `DATA_KEY` now.** It is in `deployments/acme-llp/.env` and on
   the Worker, and nowhere else. Put it in your password manager or KMS, and
   keep a second copy offline. Without it, every stored document is
   unreadable. We never have a copy.
2. **Email.** Enable sending on the subdomain, then create the routing rule
   from the agent address to the Worker. Check `dig MX` first: if the
   subdomain already receives mail, Email Routing takes that mail over.
3. **The webhook.** In your Anthropic Console, go to Manage -> Webhooks.
   Point it at `<Worker URL>/anthropic/webhook`, then store its signing
   secret on the Worker with the command printed.
4. **The agents.** Run `.venv/bin/lra live-check --tenant acme-llp --dry-run`,
   then the same command without `--dry-run`. This creates the skills, the
   agents, the environment and the memory store in your workspace, using
   your key, and writes their ids into your tenant file. It runs a scored
   evaluation within `--budget-usd`, and it is the step that spends money.

## 4. Deploy a release

```bash
.venv/bin/lra tenant deploy acme-llp --release vX.Y.Z --assets ../   # dry run
.venv/bin/lra tenant deploy acme-llp --release vX.Y.Z --assets ../ --apply
```

`--assets` points at the folder holding the downloaded release files. The
command checks them against `SHA256SUMS` again before doing anything else. It
refuses to run if any of these is true:

- The unpacked tree is a different release.
- The tenant is not marked self-hosted.
- `CLOUDFLARE_API_TOKEN` is not set.

It then runs these steps, using your token and your account id:

1. `npm ci`
2. `npx wrangler deploy --config ../deployments/acme-llp/wrangler.jsonc`.
   This builds the image, pushes it, and deploys the Worker, the Workflow
   and the container.
3. A check that `/health` answers.

The dry run prints each command, so you can attach it to a change request.

## Turning it off

You can do any of the following on your own, at any time. None of them
needs us.

| How | Effect | Time to take effect |
| --- | --- | --- |
| `.venv/bin/lra pause acme-llp --apply` (a dry run without `--apply`) | Writes one row in your D1 database. Mail from your lawyers is accepted and held sealed. Nothing is reviewed or sent. A review already under way waits before it sends | The next message. A review already under way stops before its next step |
| The same SQL in the dashboard, under Storage & Databases -> D1 -> your database -> Console. `lra pause` prints it | As above, with no command line | As above |
| Disable the Email Routing rule, or your own transport rule | No mail reaches it | Minutes |
| Delete the Anthropic API key in the Console | No review can run. A message that arrives fails, and the lawyer is told it could not be reviewed | Immediate |

`.venv/bin/lra resume acme-llp --apply` turns the service back on. Held mail
is released at the next daily run (04:17 UTC). To release it straight away,
run the `curl` command that `resume` prints. A review that was waiting picks
up within the hour.

The old switch still works as well. Setting `SERVICE_PAUSED=true` in the
tenant file and deploying it pauses the service, and it wins over `resume`.

## Support access, granted and revoked by you

When you want our help, run:

```bash
.venv/bin/lra tenant support acme-llp            # read-only, 3 days
.venv/bin/lra tenant support acme-llp --days 7 --write
```

It prints the exact token to create in your dashboard. The token has these
properties:

- **Account.** It is scoped to your account only.
- **Read-only permissions.** Workers Scripts, Workers Tail (the live log,
  which contains no document contents), Containers and Account Settings, all
  read-only.
- **`--write`.** This adds Workers Scripts, Containers and D1 write
  permission, so that we can deploy a fix or flip the switch. D1 holds some
  review text, so `--write` means we can read client material.
- **Expiry.** It expires after the number of days you choose, at most 14.
- **IP filtering.** You can restrict it to one IP address.
- **Never included.** It never includes R2 storage (the documents), anything
  at the zone level (your DNS and mail), or anything that could grant more
  access.

Send it to us over a channel you trust. Delete it when we are done. It also
expires by itself.

To see what we did with it, open Manage Account -> Audit Log. Cloudflare
describes the audit log as a history of changes, so a deploy or a changed
setting will show there. Reads, such as tailing a log, may not show. We have
not checked whether the log names the token.

We will ask for an Anthropic key only if the problem is on that side. In that
case, create one in Second Eye workspace named for the same dates, and
delete it afterwards. Console -> Usage can be filtered by key.

## Upgrading

1. Download the new release and check it (step 1).
2. Unpack it to a new folder and copy your `deployments/acme-llp/` folder
   into it, including `.env`.
3. Run `.venv/bin/lra tenant render acme-llp` and read the diff of
   `deployments/acme-llp/wrangler.jsonc`. It shows everything the new
   release changes in your configuration.
4. Run `lra tenant deploy ... --release vX.Y.Z`: first the dry run, then
   `--apply`.
5. If the release notes say that agents or skills changed, run
   `.venv/bin/lra live-check --tenant acme-llp --from skills`.

**To roll back,** deploy the previous release the same way. Our schema
changes so far have only added tables and columns, which an older release
ignores. That is a habit, not a guarantee, so check the release notes before
rolling back across a release.

## Leaving: offboarding and the deletion certificate

```bash
.venv/bin/lra tenant offboard acme-llp                   # lists everything it would delete
head -c 32 /dev/urandom > offboard.key                   # your signing key; keep it
.venv/bin/lra tenant offboard acme-llp --apply --key-file offboard.key
.venv/bin/lra tenant certificate deployments/acme-llp/offboarding/deletion-certificate-*.json \
  --key-file offboard.key
```

The dry run reads your database through your Worker, and Anthropic's lists
through your key, and shows every item it would delete. With `--apply` it
does the following, in order:

1. **Pauses the service.**
2. **Exports the audit trail** to `deployments/acme-llp/offboarding/`. This
   is your record of what the tool did, and you keep it.
3. **Purges every registered client.** This is `lra purge --client` run for
   each one.
4. **Deletes your copies at Anthropic.** This covers every session of your
   deployment's agents and every session the audit trail names, with their
   files. It also covers the uploads, the memory stores, the
   document-system vaults it created, the skills and the environment. The
   agents are archived instead, because Anthropic's API has no delete for
   them. It deletes only objects that are provably this deployment's, never
   everything in the workspace.
5. **Deletes it at Cloudflare.** This covers the Worker (and with it
   `DATA_KEY` and every other secret on it), the Workflow, the container
   application, the D1 database and the R2 bucket. R2 will not delete a
   bucket that still holds objects, so a lifecycle rule first empties the
   bucket within a day. Run the command again after that to delete the
   bucket itself. Until then, the remaining objects are sealed under a key
   that no longer exists anywhere you have not kept it.
6. **Removes local secrets.** It deletes `DATA_KEY`, `EDGE_SECRET` and the
   Anthropic key from this machine's `.env`.
7. **Writes the deletion certificate.** For each item it records what was
   deleted, its id, the count and the time. It contains no document, name
   or finding. It carries the SHA-256 of itself and an HMAC-SHA256 under
   your key. `lra tenant certificate` checks both.

If anything at Anthropic fails, nothing at Cloudflare is deleted, because
the database is the list of what remains. Run the same command again to
finish.

The certificate lists the steps that are still yours, because no command can
reach them:

- Remove the Email Routing rule and DNS records.
- Remove your transport rule.
- Remove the Anthropic webhook.
- Delete the Anthropic keys and the workspace.
- Destroy every other copy of `DATA_KEY` (your password manager or KMS, and
  the offline copy).
- Delete any Cloudflare token made for Second Eye.

## What has and has not been tested

Each item below is tested in our test suite against fakes:

- Self-hosted tenants are skipped by our pipeline.
- The release files are built reproducibly, and their checksums are checked.
- `deploy`, `pause`, `resume`, `support` and `offboard` print and run the
  right commands, in the right order, with your account id.
- The Worker reads the runtime switch (Cloudflare's own Workers test
  runtime).
- The certificate's format and signature are correct, and any change to it
  is detected.

Not yet run against the real services:

- A release built by the GitHub workflow.
- `wrangler deploy` from a release tree.
- The exact error text wrangler prints for a bucket that is not empty, or
  for a resource that is already gone. The offboarding command relies on
  that text to decide to run again or to count an item as deleted.
- `wrangler containers list --json`, and how it names the application.
- Anthropic's delete calls for skills, vaults and environments.
- The minimal Cloudflare permission set.
- The audit log's view of a support token.

Treat the first run of each as a supervised one.
