"""Who is who in Project Falcon, and the firm's positions. All invented.

One place for every name, address and number the documents, the emails and
the rehearsal share, so a name changed here changes everywhere.
"""

from __future__ import annotations

from demos.identity import load

# --- the firm and the product ------------------------------------------------

# Whoever runs the demo: the live agent's address, the firm's domain and the
# one allowlisted sender, from the environment or the primary deployment's
# private demo.env (demos/identity.py); invented ones on example domains
# when neither has them. Every email in the story is sent from LAWYER,
# whoever the story says wrote it.
IDENTITY = load()

FIRM = "Carrow Lisle LLP"
AGENT = IDENTITY.agent
AGENT_NAME = "Second Eye"
FIRM_DOMAIN = IDENTITY.firm_domain
LAWYER = IDENTITY.lawyer
LAWYER_NAME = IDENTITY.lawyer_name
PARTNER = "J. Partner"          # the comment author the demo shows in Word
ASSOCIATE = "A. Associate"      # whose tracked changes our markup carries

# --- the other side ----------------------------------------------------------

OC_FIRM = "Harrowgate LLP"
OC_DOMAIN = "harrowgate.example"
OC_NAME = "Dana Whitlock"
OC = f"dana.whitlock@{OC_DOMAIN}"
OC_TRAINEE = f"theo.marsh@{OC_DOMAIN}"

# --- the deal ------------------------------------------------------------------

CLIENT = "Northwind Holdings Limited"
CLIENT_NUMBER = "08812240"
CLIENT_DOMAIN = "northwind.example"
MATTER = "1042"
BUYER_OLD = "Falcon Holdings Limited"
BUYER = "Falcon Topco Limited"
TARGET = "Falcon Logistics Limited"
SELLER = "Beta Trading Limited"
DEAL = "Project Falcon"
PRICE = "£12,500,000"

# Signatories at completion.
SELLER_DIRECTOR = "Martin Hale"
BUYER_DIRECTOR = "Sarah Lindqvist"
CLIENT_DIRECTOR = "Owen Pryce"

# --- the firm's positions memo ---------------------------------------------------
#
# Each position is written in the firm's voice. `position`, `fallback` and
# `walk_away` are sentences that appear word for word in the memo, which is
# what lets the playbook agent (and the rehearsal's scripted stand-in) cite
# them as quotes the host can find.

POSITIONS: list[dict] = [
    {
        "clause": "Purchase price and completion accounts",
        "family": "payment-terms",
        "intro": "We act for buyers, and the price is where a buyer is most often "
                 "quietly overcharged.",
        "position": "Any completion accounts adjustment must operate pound for pound in both "
                    "directions, and the schedule must add up to the price in clause 3.",
        "fallback": "A one-way adjustment is acceptable only with a collar of no more than "
                    "2 per cent. of the price.",
        "walk_away": "We do not accept a locked box without a leakage covenant.",
        "watch": "Check every table against the price clause; sellers restate schedules late.",
    },
    {
        "clause": "Deposit",
        "family": "payment-terms",
        "intro": "Sellers ask for deposits to buy exclusivity cheaply.",
        "position": "A deposit is refundable to the buyer in full if completion does not "
                    "occur, save where the buyer is in material breach.",
        "fallback": "We can agree that half of the deposit is forfeited if the buyer walks "
                    "away without cause.",
        "walk_away": "A deposit that is non-refundable in all circumstances is not "
                     "acceptable.",
        "watch": "",
    },
    {
        "clause": "Escrow and retention",
        "family": "payment-terms",
        "intro": "The escrow is the only security most warranty claims ever have.",
        "position": "At least 10 per cent. of the price is held in escrow for not less than "
                    "six months after completion.",
        "fallback": "We will accept a three month escrow if a warranty and indemnity policy "
                    "is in place.",
        "walk_away": "No escrow and no insurance is a walk-away point.",
        "watch": "The window for claims against the escrow must stay open until the escrow "
                 "is released.",
    },
    {
        "clause": "Warranty claims periods",
        "family": "limitation-of-liability",
        "intro": "Time limits decide more claims than caps do.",
        "position": "General warranty claims may be notified for 24 months after "
                    "completion, and tax claims for seven years.",
        "fallback": "We can accept 18 months for general warranties if the tax period is "
                    "untouched.",
        "walk_away": "Anything under 12 months for general warranties goes to the partner.",
        "watch": "",
    },
    {
        "clause": "De minimis and basket",
        "family": "limitation-of-liability",
        "intro": "Thresholds should screen out trivia, not real losses.",
        "position": "The de minimis is no more than 0.1 per cent. of the price and the "
                    "basket no more than 0.5 per cent., and the basket is a tipping basket.",
        "fallback": "A deductible basket is acceptable at 0.25 per cent. of the price.",
        "walk_away": "",
        "watch": "",
    },
    {
        "clause": "Limitation of liability",
        "family": "limitation-of-liability",
        "intro": "The cap is the number the client will ask about first.",
        "position": "The seller's aggregate liability for warranty claims is capped at not "
                    "less than 50 per cent. of the price, and at 100 per cent. for title "
                    "and capacity.",
        "fallback": "We can go to 30 per cent. of the price where a warranty and indemnity "
                    "policy sits above the cap.",
        "walk_away": "A cap below 15 per cent. of the price is a walk-away point.",
        "watch": "",
    },
    {
        "clause": "Fraud carve-out",
        "family": "limitation-of-liability",
        "intro": "This one is not negotiable, and it is the one that goes missing.",
        "position": "No limitation on the seller's liability applies to a claim arising "
                    "from fraud, dishonesty or wilful concealment.",
        "fallback": "",
        "walk_away": "We never sign an agreement without the fraud carve-out.",
        "watch": "Compare every turn: a carve-out deleted without comment is still deleted.",
    },
    {
        "clause": "Tax covenant",
        "family": "indemnity",
        "intro": "Warranties tell you about tax; only a covenant pays for it.",
        "position": "The seller gives a full tax covenant for pre-completion tax, pound for "
                    "pound and not subject to the warranty limitations.",
        "fallback": "We can accept the tax covenant being subject to the overall cap.",
        "walk_away": "No tax covenant on a share purchase is a walk-away point.",
        "watch": "",
    },
    {
        "clause": "Restrictive covenants",
        "family": "non-solicitation",
        "intro": "The goodwill is what the client is paying for.",
        "position": "The seller does not compete with the business or solicit its customers "
                    "or senior employees for 24 months after completion.",
        "fallback": "We can accept 18 months where the seller keeps a related business "
                    "outside the United Kingdom.",
        "walk_away": "Less than 12 months is not acceptable.",
        "watch": "",
    },
    {
        "clause": "Long-stop date and conditions",
        "family": "termination",
        "intro": "Sellers shorten the long-stop date to create leverage late in the deal.",
        "position": "The long-stop date allows at least five months from signing to "
                    "satisfy the conditions.",
        "fallback": "We can accept four months if the seller must cooperate with every "
                    "regulatory filing.",
        "walk_away": "Any change to the long-stop date after heads of terms goes to the "
                     "partner.",
        "watch": "",
    },
    {
        "clause": "Governing law and disputes",
        "family": "governing-law-and-disputes",
        "intro": "We are an English firm and our clients litigate in London.",
        "position": "The agreement is governed by English law, with the exclusive "
                    "jurisdiction of the courts of England and Wales.",
        "fallback": "LCIA arbitration seated in London is acceptable where the seller is "
                    "outside the United Kingdom.",
        "walk_away": "",
        "watch": "",
    },
    {
        "clause": "Confidentiality",
        "family": "confidentiality",
        "intro": "Most of our deals start with the other side's NDA.",
        "position": "Confidentiality obligations are mutual and last at least three years "
                    "after the discussions end.",
        "fallback": "Two years is acceptable for an NDA with no data room.",
        "walk_away": "We do not accept a one-way NDA when our client is disclosing too.",
        "watch": "An NDA must not restrict the client from hiring anyone who applies to a "
                 "public advertisement.",
    },
]


def position_sentences() -> list[str]:
    """Every sentence the memo quotes, in order: what the playbook cites."""
    out = []
    for p in POSITIONS:
        out += [s for s in (p["position"], p["fallback"], p["walk_away"]) if s]
    return out
