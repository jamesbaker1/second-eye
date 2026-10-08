"""Who the demos write as, and to: the operator's own addresses, kept out of
the repository.

A demo that ends in a real email (Project Falcon's .eml files, sent from the
one allowlisted mailbox to the live agent) needs addresses that belong to
whoever runs it. They come from the environment, or else from `demo.env` in
the primary deployment's private folder (deployments/<name>/demo.env, see
`second-eye tenant`), one KEY=value a line:

  DEMO_LAWYER       "Name <address>": the allowlisted sender the demo writes as
  DEMO_AGENT        the agent's address (the deployment's MAIL_AGENT_ADDRESS)
  DEMO_FIRM_DOMAIN  the firm's domain (its FIRM_DOMAINS)

DEMO_ENV_FILE names another file. Without any of them the demos use invented
addresses on reserved example domains, which is all the tests and the stub
rehearsal need, and a command that writes mail meant to be sent refuses
(`require`) and says what to set. DEMO_IDENTITY=example forces the invented
ones whatever is configured: the test suite sets it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from email.utils import parseaddr
from pathlib import Path

KEYS = ("DEMO_LAWYER", "DEMO_AGENT", "DEMO_FIRM_DOMAIN")

# Invented. The lawyer's mailbox is outside the firm's domain on purpose:
# production's one allowlisted sender writes from a personal webmail address,
# and the story depends on that shape.
EXAMPLE = {
    "DEMO_LAWYER": "Alex Morgan <alex.morgan@mail.example>",
    "DEMO_AGENT": "review@legal.example.com",
    "DEMO_FIRM_DOMAIN": "example.com",
}


@dataclass(frozen=True)
class Identity:
    lawyer_name: str
    lawyer: str
    agent: str
    firm_domain: str
    # Where the addresses came from; empty for the invented ones.
    source: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.source)

    @property
    def lawyer_first(self) -> str:
        return self.lawyer_name.split()[0]


def _file() -> Path | None:
    explicit = os.environ.get("DEMO_ENV_FILE", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    try:
        from secondeye import tenant

        primary = tenant.primary_tenant()
    except Exception:  # noqa: BLE001 - no tenant to read is no file, not an error
        return None
    return primary.dir / "demo.env" if primary else None


def _read(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip().removeprefix("export ").strip()] = value.strip().strip("'\"")
    return out


def load() -> Identity:
    """The configured addresses, or the invented ones when none are."""
    values: dict[str, str] = {}
    sources: list[str] = []
    if os.environ.get("DEMO_IDENTITY", "").strip().lower() != "example":
        path = _file()
        if path is not None and path.is_file():
            found = {k: v for k, v in _read(path).items() if k in KEYS and v}
            if found:
                values.update(found)
                sources.append(str(path))
        env = {k: os.environ[k].strip() for k in KEYS if os.environ.get(k, "").strip()}
        if env:
            values.update(env)
            sources.append("the environment")
    source = " and ".join(sources)
    if values:
        missing = [k for k in KEYS if not values.get(k)]
        if missing:
            raise SystemExit(f"The demo's addresses from {source} are missing "
                             f"{', '.join(missing)} (see demos/identity.py).")
    else:
        values = dict(EXAMPLE)
    name, address = parseaddr(values["DEMO_LAWYER"])
    if "@" not in address:
        raise SystemExit(f"DEMO_LAWYER must read 'Name <address>', not {values['DEMO_LAWYER']!r}.")
    return Identity(lawyer_name=name or address.split("@")[0], lawyer=address.lower(),
                    agent=values["DEMO_AGENT"].lower(),
                    firm_domain=values["DEMO_FIRM_DOMAIN"].lower(), source=source)


def require(who: Identity, what: str) -> None:
    """Stop, saying why, when mail meant to be sent would carry invented
    addresses: production would drop it unread."""
    if who.configured:
        return
    raise SystemExit(
        f"{what} writes email to send from your allowlisted mailbox to your agent, so it "
        f"needs your addresses, not the invented ones. Set {', '.join(KEYS)} in the "
        "environment, or put them in demo.env in your primary deployment's folder "
        "(deployments/<name>/demo.env), e.g.\n"
        "  DEMO_LAWYER=Your Name <you@example.com>\n"
        "  DEMO_AGENT=review@legal.yourfirm.com\n"
        "  DEMO_FIRM_DOMAIN=yourfirm.com\n"
        "Or pass --example to write it with the invented addresses, to look at.")
