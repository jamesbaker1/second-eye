"""`lra tenant support <firm>`: what a self-hosted firm grants us, and for how long.

A self-hosted firm gives us no standing access (docs/it/self-hosted.md).
When it wants help, its IT creates a Cloudflare API token for us, scoped to
its account, read-only unless it chooses otherwise, and expiring in days,
and deletes it when we are done. This prints that token's recipe with the
firm's account and expiry filled in, so the request and the grant are the
same text. It changes nothing and calls nothing.

There is deliberately no other way in: no admin endpoint, no key of ours on
the Worker, no account of ours in the firm's Cloudflare or Anthropic
organisation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from lra import tenant as tenants

MAX_DAYS = 14

# Names as Cloudflare's permissions reference gives them; the dashboard calls
# "Write" "Edit" (developers.cloudflare.com/fundamentals/api/reference/permissions/).
READ = [
    ("Account", "Workers Scripts", "Read", "see the Worker, its settings and versions"),
    ("Account", "Workers Tail", "Read", "read its live log (no document contents are logged)"),
    ("Account", "Containers", "Read", "see the container application and its instances"),
    ("Account", "Account Settings", "Read", "wrangler needs it to find the account"),
]
WRITE = [
    ("Account", "Workers Scripts", "Write", "deploy a release or a fix, change a var"),
    ("Account", "Containers", "Write", "roll the container out with a deploy"),
    ("Account", "D1", "Write", ("flip the kill switch, run a fix; D1 rows include some review "
                               "text (findings), so this is reading client material")),
]
NEVER = [
    ("Workers R2 Storage (the documents: sealed under DATA_KEY, which we never hold, but there "
    "is no reason to give it)"),
    "anything at the zone level (DNS, Email Routing): the firm's mail is the firm's",
    "Account Settings Write, Billing, Members, API Tokens: nothing that could grant more",
]


def recipe(t: tenants.Tenant, *, days: int = 3, write: bool = False,
           today: date | None = None) -> str:
    days = max(1, min(days, MAX_DAYS))
    start = today or datetime.now(UTC).date()
    end = start + timedelta(days=days)
    perms = READ + (WRITE if write else [])
    lines = [
        (f"Support access for {t.name}: a Cloudflare API token the firm creates for us, and "
        "deletes when we are done."),
        "",
        ("In the firm's Cloudflare dashboard: My Profile -> API Tokens -> Create Token -> "
        "Create Custom Token."),
        f"  Token name:       Second Eye support {start.isoformat()} (expires {end.isoformat()})",
        "  Permissions:",
    ]
    lines += [f"    {scope} | {name} | {level:5}  {why}" for scope, name, level, why in perms]
    lines += [
        f"  Account resources: Include | {t.account_id or '<the firm account>'} only",
        (f"  TTL:              start {start.isoformat()}, end {end.isoformat()} "
        f"({days} day{'s' if days != 1 else ''}, at most {MAX_DAYS})"),
        "  Client IP Address Filtering: the address we give you for the session, if you want it",
        "",
        "Never in it:",
        *[f"  - {n}" for n in NEVER],
        "",
        ("Send it to us over a channel you trust (not the ticket that asked for it). We use it "
        "with `CLOUDFLARE_API_TOKEN=... CLOUDFLARE_ACCOUNT_ID="
        f"{t.account_id or '<account>'}` and nothing else, and tell you what we ran."),
        "",
        ("To revoke it at any time: My Profile -> API Tokens -> ... -> Delete. It also stops "
        f"working on {end.isoformat()} by itself."),
        ("To see what was done with it: Manage Account -> Audit Log records changes made in the "
        "account (a deploy, a var, a token); Cloudflare documents it as a history of changes, "
        "so reads (a tail, a list) may not appear."),
        "",
        ("The Anthropic side: only if the problem is there. Create an API key in the Redline "
        "Desk workspace named for the same dates, give it to us the same way, and delete it in "
        "Console -> API keys when we are done; Console -> Usage can be filtered by key. Most "
        "support needs only the read-only Cloudflare token above."),
    ]
    return "\n".join(lines)


def main(t: tenants.Tenant, *, days: int = 3, write: bool = False) -> int:
    if not t.self_hosted:
        print(f"{t.name} is hosted by us: we already run it, so there is nothing for the firm "
              "to grant. This is for a self-hosted firm.")
        return 2
    if days > MAX_DAYS:
        print(f"At most {MAX_DAYS} days: ask again for longer, so the firm decides again.")
        return 2
    print(recipe(t, days=days, write=write))
    return 0
