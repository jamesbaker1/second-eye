# Delegated access

## The rule

The agent never holds a firm-wide service account for the document management
system. It holds one OAuth token per lawyer, granted by that lawyer, and every
DMS call is made as them.

## Why it is done this way

The hardest problem in a firm-wide document agent is the ethical wall. A lawyer
on the Acme deal must not be shown anything from a matter they are not on, and
the agent must not quietly pull a document from one client's matter into a
review it is writing for another.

You can try to solve that in application code: tag every document, check every
retrieval, audit every prompt. That approach fails the first time someone adds a
new retrieval path and forgets the check.

Delegated OAuth solves it structurally. If the lawyer cannot open the document,
their token cannot fetch it, so the agent cannot fetch it, so it cannot appear
in the review. The firm's existing access control, which is already correct and
already maintained, becomes the agent's access control. There is no second
permission model to keep in sync.

It also gives the firm a kill switch that does not depend on us. Revoke the
lawyer's grant in the identity provider and the agent goes blind immediately.

## What it costs

One click, once, per lawyer. The consent link arrives as a reply to a document
they already sent, so it lands in a moment when they are already engaged rather
than as cold onboarding. Until they click it, reviews still work, they are just
limited to the document in front of them.

That is the right failure mode. The product degrades to something useful rather
than refusing to work.

## Scopes

Read-only by default. The agent reads the firm's documents and does not file
anything back. Write access, for filing a redline into the matter, is a separate
scope requested separately, and only once there is a reason to want it.

| System | Default scopes |
| --- | --- |
| iManage Work | `user`, `documents:read`, `workspaces:read`, `matters:read` |
| NetDocuments | `read` |
| SharePoint / Graph | `Files.Read.All`, `Sites.Read.All`, `offline_access` |

## Lifecycle

1. A lawyer sends their first document. The agent reviews it standalone and
   offers the connection in one line at the end of the reply.
2. They reply `connect`. The agent emails a one-time link carrying a single-use
   state parameter.
3. They grant. The callback exchanges the code for a token stored against their
   address, and shows one plain page telling them to close the tab.
4. Every later review builds its tools from that token.
5. They reply `revoke` at any time, to any email, and the token is deleted.

Since `docs/migration.md` phases 4 and 5, step 3 stores the grant as a
credential in the lawyer's own Anthropic vault instead of our table,
Anthropic refreshes it, step 4 is our MCP server bound to one matter per
session, and step 5 archives the credential. A refresh Anthropic cannot
complete emails the lawyer to reply "connect". Our token table is used only
by the `DMS_MCP_BINDING=capability` fallback.

## Production requirements

These are not optional before live matters.

- Tokens encrypted at rest with firm-held keys. Done: the access and refresh
  tokens in our table are sealed with `DATA_KEY` (`crypto.seal_text`), and
  in the default `query` binding the grant is held in the lawyer's Anthropic
  vault, not our table. The key has to be the firm's.
- The callback served over TLS on a host the firm controls.
- State parameters expire, not just single-use.
- An audit log of every DMS call the agent makes, attributable to the lawyer it
  was made as.
- Token refresh failures degrade to a standalone review and a plain explanation,
  never a silent empty result.
