"""The demo's attachments and email, built the same way every time.

The documents come from the outbound eval's builders (evals/outbound.py), so
the demo shows what the eval measures. Office files get fixed timestamps
(demos/falcon/documents.stable) and the PDF is written invariant, so a
rebuild writes the same bytes as the copies committed under files/.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from demos.falcon.documents import stable
from evals import outbound as build

HERE = Path(__file__).resolve().parent
FILES = HERE / "files"

FROM = "Jim Baker <jim@firm.example>"
TO = "review@legal.example.com"  # send it to your own agent's address
SUBJECT = "Project Atlas - SPA execution version"
BODY = """\
Attached is the execution version of the SPA (v5) for Acme Holdings and Northgate \
Freight Ltd, with the disclosure letter and the board update.

I've capped liability at $2,000,000 and removed the exclusivity clause.

Jim
"""

SPA = "Atlas SPA v4 (execution version).docx"
DECK = "Atlas board update.pptx"
EXHIBIT = "Exhibit A - Permitted Advisers.pdf"

_AGREEMENT = [
    "Share Purchase Agreement",
    ("This Agreement is made as of March 3, 2026 between Acme Holdings, Inc., a Delaware "
    "corporation (“Buyer”), and Northgate Freight Ltd, a company incorporated in England "
    "and Wales (“Seller”)."),
    "1. Sale. Seller sells and Buyer purchases the Shares on the terms of this Agreement.",
    "2. Price. The price is one million dollars ($1,000,000), payable at Completion.",
    ("3. Liability. Seller's aggregate liability under this Agreement is capped at two "
    "million dollars ($2,000,000)."),
    "4. Completion. Completion shall take place at the offices of Buyer's counsel on [●].",
    ("5. Exclusivity. Until Completion Seller shall not solicit offers for the Shares, and "
    "this exclusivity clause survives any termination for three (3) months."),
    ("6. Advisers. Each party may disclose this Agreement to the advisers listed in "
    "Exhibit A."),
    "7. Governing Law. This Agreement is governed by the laws of the State of New York.",
    ("IN WITNESS WHEREOF, the parties have executed this Agreement as of the date first "
    "written above."),
    "ACME HOLDINGS, INC.",
    "By: ______________________",
    "Name: Jane Doe",
    "Title: Chief Executive Officer",
    "NORTHGATE FREIGHT LTD",
    "By: ______________________",
    "Name: John Smith",
    "Title: Director",
]


def agreement() -> bytes:
    """The execution version, with what an execution version should not carry:
    the draft header, two internal comments, a hidden fallback position, a
    highlight, the last client's name in its properties, an embedded fee
    workbook and a blank where the completion date goes."""
    return stable(build.docx(
        _AGREEMENT,
        header="DRAFT 4: March 1, 2026",
        title="Bluewater Shipping LLC - Share Purchase Agreement",
        custom={"ClientName": "Bluewater Shipping LLC", "MatterNumber": "40412-0003"},
        comments=[("Priya Associate", "Client will go to $3m on the cap if pushed."),
                  ("Bob Partner", "Agreed, but do not offer it yet.")],
        hidden="[Fallback: cap at $3,000,000 if Seller insists]",
        highlighted="Completion takes place on March 31, 2026.",
        embedded=True,
        author="Priya Associate",
    ))


def deck() -> bytes:
    return stable(build.deck(
        ["Project Atlas", "Timetable to signing", "Price and liability cap",
         "Fallback positions"],
        notes={3: "If they push, we can go to $3m on the cap. Do not say so first."},
        hidden=(4,),
    ))


def exhibit() -> bytes:
    return build.pdf(
        ["Exhibit A - Permitted Advisers",
         "Acme Holdings, Inc.: Smith & Jones LLP; Harbour Corporate Finance",
         "Northgate Freight Ltd: Carrow Lisle LLP"],
        sticky="Do we really have to list Harbour?", highlight=True,
        invariant=True,
    )


def message() -> bytes:
    """The email, as a .eml that `lra replay` reads and a mail client opens."""
    msg = EmailMessage()
    msg["From"] = FROM
    msg["To"] = TO
    msg["Subject"] = SUBJECT
    msg["Message-ID"] = "<outbound-demo-1@firm.example>"
    msg["Date"] = "Sun, 04 Oct 2026 09:00:00 +0000"
    msg.set_content(BODY)
    for name, content, kind in (
        (SPA, agreement(), ("application", ("vnd.openxmlformats-officedocument."
                                           "wordprocessingml.document"))),
        (DECK, deck(), ("application", ("vnd.openxmlformats-officedocument."
                                       "presentationml.presentation"))),
        (EXHIBIT, exhibit(), ("application", "pdf")),
    ):
        msg.add_attachment(content, maintype=kind[0], subtype=kind[1], filename=name)
    msg.set_boundary("==outbound-demo==")
    return bytes(msg)


def write(folder: Path = FILES) -> list[Path]:
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    for name, builder in ((SPA, agreement), (DECK, deck), (EXHIBIT, exhibit)):
        path = folder / name
        path.write_bytes(builder())
        written.append(path)
    (folder / "email.txt").write_text(f"To: {TO}\nSubject: {SUBJECT}\n\n{BODY}")
    (folder / "email.eml").write_bytes(message())
    written += [folder / "email.txt", folder / "email.eml"]
    return written
