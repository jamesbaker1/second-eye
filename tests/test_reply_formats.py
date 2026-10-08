"""Every short reply command, through every mail client's reply format.

Production, 2026-09-29: Gmail wrapped "On <date>, Second Eye <review@...>
wrote:" before "wrote:", the stripper missed it, and a bare "30" answering
"30 or 13?" was read as a twelve-word instruction. Every short command the
reviews teach ("30", "undo 2", "B is fine", "clean copy", "sig pages",
"stop") depends on the lawyer's words being separated from the quoted
review, the signature and the disclaimer, in whatever shape their client
sends them.

tests/fixtures/replies/ holds raw replies as the clients send them: headers,
MIME structure, transfer encoding and line endings. `{{COMMAND}}` marks
where the lawyer typed. Each is parsed the way a real reply is
(ConsoleProvider.parse_eml -> InboundEmail.body -> router.strip_reply ->
followup.plan_reply / followup.classify, via triage.rules_plan, which calls
the rules path's predicates in its order) against a conversation shaped like
production's: numbered changes, lettered points and two open questions.
"""

from __future__ import annotations

import base64
import html
import re
from pathlib import Path

import pytest

from secondeye import crypto, thread, triage
from secondeye.mail.console import ConsoleProvider
from secondeye.pipeline import router
from tests.test_cloudflare import cloud  # noqa: F401  (the fixture)
from tests.test_workflow_steps import answered, flow, raw_review, step  # noqa: F401

DIR = Path(__file__).parent / "fixtures" / "replies"
FIXTURES = sorted(DIR.glob("*.eml"))
PLACEHOLDER = "{{COMMAND}}"
AGENT_ID = "<rd-review-1@legal.firm.com>"

# What each command must be read as: the rules plan's intents, then its
# answers (question id, value), undo numbers and dismissed letters.
COMMANDS: dict[str, tuple[list[str], list[tuple[int, str]], list[int], list[str]]] = {
    "30": (["answer"], [(1, "30")], [], []),
    "13 please": (["answer"], [(1, "13")], [], []),
    "undo 2": (["undo"], [], [2], []),
    "B is fine": (["dismiss"], [], [], ["B"]),
    "clean copy": (["clean_copy"], [], [], []),
    "30; 4; clean copy": (["answer", "clean_copy"], [(1, "30"), (2, "4")], [], []),
    "sig pages": (["sig_pages"], [], [], []),
    "renumber": (["renumber"], [], [], []),
    "remember that": (["remember"], [], [], []),
    "stop": (["stop"], [], [], []),
    "help": (["help"], [], [], []),
}

# Anything of these in the lawyer's words means quoted history, a signature,
# a disclaimer or a client footer leaked through.
LEAKS = ("Second Eye", "review@", "Should this be", "wrote", "écrit", "escribió",
         "schrieb", "From:", "Von:", "De :", "Sent", "Gesendet", "Envoyé", "Original Message",
         "Forwarded", "CONFIDENTIALITY", "+44", "+1 415", "Kind regards", "freundlichen",
         "LLP", "Partner", "Outlook", "iPhone", "Reply ", "undo 1")


def render(raw: bytes, command: str) -> bytes:
    """The fixture with the lawyer's words typed where `{{COMMAND}}` is, in
    that part's own encoding, markup and line endings."""
    assert not set("<>&=") & set(command)
    crlf = b"\r\n" in raw
    nl = "\r\n" if crlf else "\n"

    def typed(at: int) -> str:
        before = raw[:at].lower()
        start = before.rfind(b"content-type: text/")
        if before[start + 19:start + 23] == b"html":
            return "<br>".join(html.escape(line, quote=False) for line in command.split("\n"))
        return command.replace("\n", nl)

    def b64(m: re.Match) -> bytes:
        text = base64.b64decode(m.group(2)).decode("utf-8")
        if PLACEHOLDER not in text:
            return m.group(0)
        encoded = base64.encodebytes(
            text.replace(PLACEHOLDER, typed(m.start()).replace("\r\n", "\n")).encode()).decode()
        return m.group(1) + encoded.replace("\n", nl).encode()

    raw = re.sub(rb"(?i)(content-transfer-encoding: base64\r?\n(?:[^\r\n]+\r?\n)*\r?\n)"
                 rb"((?:[A-Za-z0-9+/=]+\r?\n)+)", b64, raw)
    out, last = [], 0
    for m in re.finditer(re.escape(PLACEHOLDER.encode()), raw):
        out += [raw[last:m.start()], typed(m.start()).encode()]
        last = m.end()
    out.append(raw[last:])
    return b"".join(out)


def parsed(fixture: Path, command: str):
    return ConsoleProvider().parse_eml(render(fixture.read_bytes(), command))


def conversation() -> thread.ThreadState:
    """What the quoted review in every fixture left behind."""
    C, Q, F = thread.Change, thread.Question, thread.Flagged
    return thread.ThreadState(
        id="t-acme", owner="jim@firm.com", filename="Acme NDA.docx",
        changes=[C(1, "Reciever", "Recipient", '"Reciever" is misspelled', "typo", "review", 1, True),
                 C(2, "Section 7", "Clause 7", "Section 7 should be Clause 7", "xref", "review", 1, True),
                 C(3, "its'", "its", 'Apostrophe in "its\'"', "typo", "review", 1, True)],
        questions=[Q(1, "Should this be 30 or 13?", "thirty (13) months", ["30", "13"], None, 1),
                   Q(2, "4 or 5 business days?", "within 4 business days", ["4", "5"], None, 1)],
        flagged=[F(1, "A", "substantive", "confidentiality",
                   "The confidentiality obligation has no time limit", "shall protect", None, True, False),
                 F(2, "B", "substantive", "indemnity", "The indemnity is uncapped", "indemnify",
                   None, True, False)],
    )


@pytest.fixture
def db(monkeypatch, tmp_path):
    from secondeye.config import settings

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/r.sqlite3")
    monkeypatch.setenv("MAIL_AGENT_ADDRESS", "review@legal.firm.com")
    monkeypatch.setenv("FIRM_DOMAINS", "firm.com")
    settings.cache_clear()
    yield
    settings.cache_clear()


def test_there_is_a_fixture_for_every_client_format():
    names = {f.stem for f in FIXTURES}
    assert len(names) >= 20
    for raw in (f.read_bytes() for f in FIXTURES):
        assert b"In-Reply-To: " + AGENT_ID.encode() in raw
        assert AGENT_ID.encode() in raw.split(b"References: ", 1)[1].split(b"\n", 1)[0]


@pytest.mark.parametrize("command", list(COMMANDS))
@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.stem)
def test_every_command_in_every_reply_format_is_read_as_that_command(db, fixture, command):
    email = parsed(fixture, command)
    assert email.in_reply_to == AGENT_ID and AGENT_ID in email.references

    words = router.strip_reply(email.body)
    assert command in words
    leaked = [x for x in LEAKS if x in words]
    assert not leaked, f"{leaked} left in {words!r}"

    intents, answers, undo, dismiss = COMMANDS[command]
    plan = triage.rules_plan(email, triage.Context(state=conversation()))
    assert [d.intent for d in plan.intents] == intents, words
    assert [(a.question_id, a.value) for a in plan.answers] == answers
    assert plan.undo == undo
    assert plan.dismiss == dismiss
    assert not plan.instruction


# --------------------------------------------------------------------------
# What must never be cut: the lawyer's own words that look like a marker
# --------------------------------------------------------------------------

KEPT = [
    "On Tuesday they wrote: the cap is 5m.\nPlease push back and make it 3m.",
    "Their counsel's note\nOn 3 March they wrote:\nwe will not accept a cap. Please add one at 2x fees.",
    "Make the cap 3m.\nFrom: the Buyer's perspective that is still generous, so hold firm.",
    "Please tighten clause 9.\nSent from my review of the SPA, the same point arises there.",
    "Add a notice address for the Seller:\n1 Fleet Street, London EC4Y 1AA\nT: +44 20 7946 0000",
    "Make it 30.\nThis email is the only instruction you need on that point.",
]


@pytest.mark.parametrize("fixture", ["gmail_web", "outlook_desktop", "outlook_web_html_only",
                                     "apple_mac"])
@pytest.mark.parametrize("instruction", KEPT)
def test_an_instruction_that_looks_like_a_marker_is_kept_whole(db, fixture, instruction):
    email = parsed(DIR / f"{fixture}.eml", instruction)
    words = " ".join(router.strip_reply(email.body).split())
    typed = " ".join(instruction.split())
    # Apple Mail's and Outlook's fixtures are signed "Jim" under the words.
    assert words in (typed, typed + " Jim")
    plan = triage.rules_plan(email, triage.Context(state=conversation()))
    assert [d.intent for d in plan.intents] == ["instruction"]


def test_a_signature_needs_contact_details_or_a_sign_off_to_be_one():
    """A short capitalised line under a request is not a signature on its own."""
    assert router.strip_reply("make it 30\n\nClean Copy") == "make it 30\n\nClean Copy"
    assert router.strip_reply("Thanks,\nJim") == "Thanks,\nJim"
    assert router.strip_reply("30\n\nKind regards,\nJim Baker\nPartner") == "30"


def test_an_attribution_as_the_first_line_is_not_the_reply():
    body = "On 29/09/2026 19:44, Second Eye wrote:\n> Should this be 30 or 13?\n\n30\n\n> Changes\n"
    assert router.strip_reply(body) == "30"


# --------------------------------------------------------------------------
# The whole Workflow, for a few: the reply's In-Reply-To finds the review
# --------------------------------------------------------------------------

FLOWED = ["gmail_web_wrapped_signature", "outlook_desktop", "outlook_web_html_only",
          "apple_iphone", "lawyer_signature_disclaimer", "gmail_web_fr"]


def reviewed(client) -> None:
    """The review the fixtures quote, sent as rd-review-1."""
    step(client, "/prepare", raw=crypto.seal(raw_review()))
    session = step(client, "/session/start").json()["sessionId"]
    step(client, "/session/status", sessionId=session)
    step(client, "/finish")
    step(client, "/finished", kind="review", messageId=AGENT_ID.strip("<>"))


@pytest.mark.parametrize("name", FLOWED)
def test_a_real_reply_is_answered_through_the_workflow(flow, name):  # noqa: F811
    client, _, worker = flow
    reviewed(client)
    email = parsed(DIR / f"{name}.eml", "30")
    assert thread.resolve("jim@firm.com", None, email.message_id, email.in_reply_to,
                          email.references) is not None

    raw = render((DIR / f"{name}.eml").read_bytes(), "30")
    assert answered(client, worker, raw).startswith("Noted: 30.")

    again = render((DIR / f"{name}.eml").read_bytes(), "undo 1").replace(
        b"Message-ID: <", b"Message-ID: <second-")
    assert answered(client, worker, again, job="rf-the-second-reply").startswith("I reversed")
