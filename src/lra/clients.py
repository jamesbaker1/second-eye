"""Which client a matter belongs to: the key memory is walled by.

ABA Formal Opinion 512 asks a firm using a self-learning tool to make sure
what it learns from one client's documents is not used on another's. Memory
is therefore walled by client (docs/memory.md): anything learned from a
client's documents is kept under that client and read only on that client's
matters. That needs one answer this module gives, conservatively: whose
matter is this?

The firm registers its clients (`lra clients add`): the client number its
matter numbers begin with, the names the client appears under in documents,
and the email domains its people write from. A matter is then resolved to a
client, in order of confidence:

  1. a matter already linked to a client (by an admin, or by 2 or 3 below);
  2. the matter number itself, when it begins with a registered client
     number: "10234-0007" is client 10234's;
  3. failing both, the parties: the registered clients whose domains are on
     the email or whose names appear in the document. Exactly one, or none.

Two candidates is none. So is an unregistered client. An unknown client means
no memory crosses matters at all, which is the safe failure: a review that
does not know whose document it is reads nothing learned elsewhere.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime

from lra.store import connect

SCHEMA = """
CREATE TABLE IF NOT EXISTS clients (
    id TEXT PRIMARY KEY,      -- the firm's client number, lower case
    name TEXT NOT NULL,
    aliases TEXT,             -- json: other names the client appears under
    domains TEXT,             -- json: email domains the client writes from
    created_at TEXT
);
CREATE TABLE IF NOT EXISTS client_matters (
    matter_id TEXT PRIMARY KEY,   -- lower case
    client_id TEXT NOT NULL,
    source TEXT,                  -- admin | number | parties
    linked_at TEXT
);
"""

# A matter number's client part: everything before the first separator.
_MATTER_NUMBER = re.compile(r"^([A-Za-z0-9]+)[-./:_]\S+$")

# A name shorter than this matches too much text to wall anything by.
_MIN_NAME = 4


@dataclass
class Client:
    id: str
    name: str
    aliases: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)

    @property
    def names(self) -> list[str]:
        return [n for n in [self.name, *self.aliases] if len(n.strip()) >= _MIN_NAME]


def init() -> None:
    with connect() as c:
        c.executescript(SCHEMA)


def _client(r) -> Client:
    return Client(id=r["id"], name=r["name"], aliases=json.loads(r["aliases"] or "[]"),
                  domains=json.loads(r["domains"] or "[]"))


def register(client_id: str, name: str, aliases: list[str] | None = None,
             domains: list[str] | None = None) -> Client:
    """Add or replace a client. Re-applies the memory walls afterwards: a
    lawyer's personal note that names this client, written before the firm
    said it was one, is client content and must leave the personal scope."""
    init()
    client = Client(id=client_id.strip().lower(), name=name.strip(),
                    aliases=[a.strip() for a in aliases or [] if a.strip()],
                    domains=[d.strip().lower().lstrip("@") for d in domains or [] if d.strip()])
    if not client.id or not client.name:
        raise ValueError("a client needs a number and a name")
    with connect() as c:
        c.execute("INSERT OR REPLACE INTO clients (id, name, aliases, domains, created_at) "
                  "VALUES (?, ?, ?, ?, ?)",
                  (client.id, client.name, json.dumps(client.aliases),
                   json.dumps(client.domains), datetime.now(UTC).isoformat()))
    from lra import memory

    memory.init()
    memory.enforce_walls()
    return client


def all_clients() -> list[Client]:
    init()
    with connect() as c:
        c.row_factory = sqlite3.Row
        rows = c.execute("SELECT * FROM clients ORDER BY id").fetchall()
    return [_client(r) for r in rows]


def get(client_id: str) -> Client | None:
    init()
    with connect() as c:
        c.row_factory = sqlite3.Row
        row = c.execute("SELECT * FROM clients WHERE id = ?",
                        (client_id.strip().lower(),)).fetchone()
    return _client(row) if row else None


def link(matter_id: str, client_id: str, source: str = "admin") -> None:
    """Record whose a matter is. An admin's link replaces any other; an
    inferred one never replaces an existing link."""
    init()
    now = datetime.now(UTC).isoformat()
    verb = "INSERT OR REPLACE" if source == "admin" else "INSERT OR IGNORE"
    with connect() as c:
        c.execute(f"{verb} INTO client_matters (matter_id, client_id, source, linked_at) "
                  "VALUES (?, ?, ?, ?)",
                  (matter_id.strip().lower(), client_id.strip().lower(), source, now))


def for_matter(matter_id: str | None) -> str | None:
    """The client a matter belongs to, from the link or the matter number.
    None when neither says: the caller then walls memory to the matter."""
    if not matter_id or not matter_id.strip():
        return None
    init()
    key = matter_id.strip().lower()
    with connect() as c:
        row = c.execute("SELECT client_id FROM client_matters WHERE matter_id = ?",
                        (key,)).fetchone()
    if row:
        return row[0]
    m = _MATTER_NUMBER.match(key)
    if m and get(m.group(1)):
        link(key, m.group(1), source="number")
        return m.group(1)
    return None


def _domain(address: str) -> str:
    return address.strip().lower().rpartition("@")[2].strip(">")


def _named_in(name: str, text: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(name)}(?![\w])", text, re.IGNORECASE))


def from_parties(addresses: list[str], text: str) -> str | None:
    """The one registered client whose domain is on the email or whose name
    is in the document, or None. Two is none: a document naming two of the
    firm's clients is not safely either's."""
    found: set[str] = set()
    domains = {_domain(a) for a in addresses if "@" in a}
    for client in all_clients():
        by_domain = any(d == cd or d.endswith("." + cd)
                        for d in domains for cd in client.domains)
        if by_domain or (text and any(_named_in(n, text) for n in client.names)):
            found.add(client.id)
    return found.pop() if len(found) == 1 else None


def resolve(matter_id: str | None, addresses: list[str] | None = None,
            text: str = "") -> str | None:
    """The client, by the order in this module's docstring. A client found
    from the parties is linked to the matter, when there is one, so the
    replies on that matter find it without the document."""
    client = for_matter(matter_id)
    if client:
        return client
    client = from_parties(addresses or [], text)
    if client and matter_id:
        link(matter_id, client, source="parties")
    return client


def markers() -> list[str]:
    """Every string that identifies a registered client: numbers, names,
    domains. A personal note containing one is client content."""
    out: list[str] = []
    for client in all_clients():
        out += [client.id, *client.names, *client.domains]
    return [m for m in out if len(m) >= 3]


__all__ = ["Client", "all_clients", "for_matter", "from_parties", "get", "init", "link",
           "markers", "register", "resolve"]
