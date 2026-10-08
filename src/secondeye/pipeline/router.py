"""What does this reply actually want?

A reply to a review might be answering a question, asking for a change, telling
the agent to undo something, attaching a new version, complaining that a class
of finding is unwelcome, or disconnecting entirely. None of these are
distinguishable by keyword, and the product promise is plain English.

Two layers.

**Deterministic, for the unambiguous.** A reply whose entire body is one short
phrase is safe to act on directly. This layer exists mostly to fix a real bug:
the handler previously used `text_body.startswith("revoke")`, which would fire
on a forwarded message, an auto-reply, or any mail whose first word happened to
be "revoke". Matching now requires the whole message, once quoted history and
signature are stripped, to be that short phrase and nothing else.

**The model, for everything else.** Classification with the thread's open
questions and change ledger in front of it, because "make it 30" only means
something if you know what was asked.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum


class Intent(str, Enum):
    REVIEW = "review"              # a document to look at; the default
    ANSWER = "answer"              # answering a question the agent asked
    INSTRUCTION = "instruction"    # make this change
    UNDO = "undo"                  # revert something
    QUESTION = "question"          # asking the agent something
    NEW_VERSION = "new_version"    # a revised document to work from
    SUPPRESS = "suppress"          # stop flagging this kind of thing
    CONNECT = "connect"            # grant document-system access
    REVOKE = "revoke"              # disconnect
    STOP = "stop"                  # leave me alone on this thread
    ACK = "ack"                    # "thanks", "all accepted": nothing to do, nothing to say
    HELP = "help"                  # what can you do
    SETUP = "setup"                # how do I make this automatic
    REMEMBER = "remember"          # keep the note the agent proposed
    FORGET = "forget"              # drop it
    UNSUPPRESS = "unsuppress"      # "flag it again": what it last said it stopped


@dataclass
class Routed:
    intent: Intent
    confidence: float = 1.0
    reason: str = ""
    payload: dict = field(default_factory=dict)


# Whole-message phrases. The body must be ONLY this, after stripping quoted
# history and a signature block. Anything longer goes to the model.
_EXACT: dict[str, Intent] = {
    "revoke": Intent.REVOKE,
    "remember that": Intent.REMEMBER,
    "remember it": Intent.REMEMBER,
    "yes remember that": Intent.REMEMBER,
    "keep that": Intent.REMEMBER,
    "forget that": Intent.FORGET,
    "forget it": Intent.FORGET,
    "don't remember that": Intent.FORGET,
    # Answers the one line a review adds when it has stopped flagging
    # something ("Reply "flag it again" to have it back"). The named form,
    # "flag the Oxford comma again", is read by memory.unsuppression_request.
    "flag it again": Intent.UNSUPPRESS,
    "flag that again": Intent.UNSUPPRESS,
    "flag them again": Intent.UNSUPPRESS,
    "start flagging it again": Intent.UNSUPPRESS,
    "revoke access": Intent.REVOKE,
    "disconnect": Intent.REVOKE,
    "connect": Intent.CONNECT,
    "connect me": Intent.CONNECT,
    "yes connect": Intent.CONNECT,
    "stop": Intent.STOP,
    "unsubscribe": Intent.STOP,
    "no thanks": Intent.STOP,
    "undo": Intent.UNDO,
    "undo everything": Intent.UNDO,
    "undo it all": Intent.UNDO,
    "revert": Intent.UNDO,
    "revert everything": Intent.UNDO,
    "start over": Intent.UNDO,
    "help": Intent.HELP,
    "instructions": Intent.HELP,
    "what can you do": Intent.HELP,
    "how does this work": Intent.HELP,
    "what do you do": Intent.HELP,
    "setup": Intent.SETUP,
    "set up": Intent.SETUP,
    "set up the rule": Intent.SETUP,
    "mail rule": Intent.SETUP,
    "bcc rule": Intent.SETUP,
    "how do i set this up": Intent.SETUP,
}

_MAX_EXACT_WORDS = 6

# The table as a message is compared with it: punctuation gone. Written with
# it above for the reader; looked up without it, or "don't remember that",
# whose message loses its apostrophe before the lookup, never matched.
_EXACT_KEYS: dict[str, Intent] = {
    re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", phrase)).strip(): intent
    for phrase, intent in _EXACT.items()
}

# A whole message that closes the loop rather than opening one. "Thanks" used
# to reach the instruction agent, which ran the model over the contract and
# answered "Nothing to change." A lawyer who says thank you and gets a reply
# about it learns that every message costs them another. Checked only after
# the open questions, so "ok" answering "1x or 2x?" is still an answer.
_ACK = re.compile(
    r"^(?:(?:many|great|ok|okay|perfect|brilliant|lovely|super|cool|excellent|good|"
    r"got it|noted|received|will do|done|sent|all accepted|accepted all|accepted|"
    r"looks good|that works|thanks|thank you|thx|ta|cheers)[\s,]*){1,4}"
    r"(?:\s*(?:very|so)\s+much)?(?:\s+all)?(?:\s+again)?$"
)


def is_acknowledgement(body: str) -> bool:
    """Whether the whole message, quoted history and signature stripped, is a
    thank-you or a "done" and nothing else."""
    text = strip_reply(body)
    if _ack_phrase(text):
        return True
    # "Thanks, Jim" and "Thanks\nJim Baker\nPartner": a sign-off is up to three
    # capitalised words after the first comma or line break. Anything else
    # after the break is a message, and is read as one.
    m = re.match(r"(.+?)[,\n]\s*(\S.*)$", text, re.DOTALL)
    if not m:
        return False
    head, tail = m.group(1), m.group(2).split()
    return (len(tail) <= 3 and all(w[:1].isupper() for w in tail)
            and _ack_phrase(head))


def _ack_phrase(text: str) -> bool:
    normalised = re.sub(r"[^\w\s]", " ", text).strip().lower()
    normalised = re.sub(r"\s+", " ", normalised)
    return bool(normalised) and len(normalised.split()) <= 6 and bool(_ACK.match(normalised))


# Mail that must never be acted on at all.
_AUTO_HEADERS = {
    "auto-submitted": lambda v: v.lower() != "no",
    "x-autoreply": lambda v: True,
    "x-autorespond": lambda v: True,
    "precedence": lambda v: v.lower() in {"bulk", "junk", "list", "auto_reply"},
    "list-id": lambda v: True,
    "list-unsubscribe": lambda v: True,
    "x-auto-response-suppress": lambda v: True,
}

_BOUNCE_SENDERS = re.compile(
    r"^(mailer-daemon|postmaster|no-?reply|do-?not-?reply|bounce|notification)s?@",
    re.IGNORECASE,
)

_OOO = re.compile(
    r"\b(out of (the )?office|on annual leave|on holiday|automatic reply|"
    r"auto[- ]?reply|away from my desk|maternity leave|parental leave)\b",
    re.IGNORECASE,
)


def is_automated(from_address: str, subject: str, headers: dict[str, str]) -> str | None:
    """Why this message must not be answered, or None if it is a real person.

    Without this an auto-responder and this agent will talk to each other until
    somebody notices. It is the most embarrassing failure mode available to an
    email product and it is entirely preventable.
    """
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    for name, test in _AUTO_HEADERS.items():
        value = lowered.get(name)
        if value is not None and test(value):
            return f"automated mail ({name}: {value})"

    if _BOUNCE_SENDERS.match(from_address or ""):
        return f"automated sender ({from_address})"

    if _OOO.search(subject or ""):
        return "out-of-office reply"

    return None


# What the receiving gateway concluded about the sending domain. Postmark and
# AgentMail both pass the header through; a provider that does not leaves us
# unable to tell, which is unknown rather than failed.
_AUTH_HEADERS = (
    "authentication-results",
    "arc-authentication-results",
    "x-authentication-results",
)

# Only an explicit aggregate failure counts. `dmarc=none` means the domain
# publishes no policy, `permerror`/`temperror` mean the check could not be run,
# and treating either as a forgery would silently drop real mail from firms that
# have not finished their DNS work.
_DMARC_FAIL = re.compile(r"\bdmarc\s*=\s*fail\b", re.IGNORECASE)
_COMPAUTH_FAIL = re.compile(r"\bcompauth\s*=\s*fail\b", re.IGNORECASE)


def authentication_failed(headers: dict[str, str]) -> str | None:
    """Why this sender's domain did not authenticate, or None.

    Tri-state, and deliberately fail-open on silence: absent headers mean the
    gateway did not tell us, not that the mail is forged. The one thing this is
    for is the commands that change stored state. An allowlist checks the domain
    in the From header, which is precisely the field a spoofer controls, so
    "revoke" from a forged partner address passes the allowlist and deletes a
    real grant. PLAN.md's mitigation for sender spoofing is "never act on an
    unauthenticated sender"; this is the part of it we can see from here.
    """
    lowered = {k.lower(): v for k, v in (headers or {}).items()}
    for name in _AUTH_HEADERS:
        value = lowered.get(name)
        if not value:
            continue
        if _DMARC_FAIL.search(value):
            return f"DMARC failed ({name})"
        if _COMPAUTH_FAIL.search(value):
            return f"composite authentication failed ({name})"
    return None


# Where quoted history starts. Every marker is a whole line (or, for the
# attribution, up to three wrapped lines), never a phrase inside one: a
# lawyer who writes "From: the Buyer's perspective..." or "On Tuesday they
# wrote: ..." is writing, not quoting.
#
# The attribution, in the languages the clients localise it into. Gmail and
# Outlook wrap a long one, before "wrote:" or inside the address, which left
# "On ... <address>" and "wrote:" in the reply and made a bare "30" a
# twelve-word instruction. It has to carry a year, a time or an address to
# count: "On 3 March they wrote:" is the lawyer quoting someone.
_ATTRIBUTION = re.compile(
    r"^[ \t>]*(?:On|El|Le|Am|Op|Il|Em|Den|På|Dne)\s"
    r"(?:[^\n]|\n(?![ \t>]*\n)){0,400}?"
    r"(?:\bwrote|\bescribió|\ba\s+écrit|\bschrieb\b(?:[^\n]|\n(?![ \t>]*\n)){0,250}?|"
    r"\bschreef|\bha\s+scritto|\bescreveu|\bskrev|\bnapsal)"
    r"[ \t]*:[ \t]*$",
    re.IGNORECASE | re.MULTILINE,
)
_ATTRIBUTION_EVIDENCE = re.compile(r"\b\d{4}\b|\d{1,2}[:.]\d{2}|@")


def _is_attribution(text: str, m: re.Match) -> bool:
    """A client's attribution carries a year, a time or an address; one
    written by hand in a test or an old client at least has quoted lines, or
    nothing, under it."""
    if _ATTRIBUTION_EVIDENCE.search(m.group(0)):
        return True
    after = next((ln for ln in text[m.end():].splitlines() if ln.strip()), "")
    return not after or after.lstrip().startswith(">")

# Outlook's header block, in English and the other languages it ships in. A
# "From:" line counts only with a "Sent:" or "Date:" line under it.
_HEADER_BLOCK = re.compile(
    r"^[ \t]*\*?(?:From|De|Von|Da|Van|Från|Fra|Od)[ \t]?:\*?[ \t]+\S[^\n]*\n"
    r"(?:[ \t]*\*?(?:To|Cc|Subject|Reply-To|Objet|Betreff|An|À|Asunto|Para|Oggetto|Aan|A)"
    r"[ \t]?:[^\n]*\n){0,3}"
    r"[ \t]*\*?(?:Sent|Date|Envoyé|Gesendet|Datum|Enviado(?:\s+el)?|Data|Inviato|"
    r"Verzonden|Skickat|Sendt|Odesláno)[ \t]?:",
    re.IGNORECASE | re.MULTILINE,
)

_SEPARATOR = re.compile(
    r"^[ \t]*(?:"
    r"-{2,}\s*(?:Original Message|Message d'origine|Ursprüngliche Nachricht|Mensaje original)"
    r"|-{3,}\s*(?:Forwarded message|Mensaje reenviado|Message transféré|"
    r"Weitergeleitete Nachricht|Messaggio inoltrato)"
    r"|_{10,}[ \t]*$"                       # Outlook's rule, and <hr> in its HTML
    r"|Begin forwarded message:"            # Apple Mail
    # Mobile footers, as the whole line: "Sent from my review of the SPA"
    # is a sentence.
    r"|(?:Sent from my (?:iPhone|iPad|Android|mobile|phone|BlackBerry|Galaxy|Pixel|Samsung)"
    r"|Sent from (?:Outlook|Mail for Windows|Yahoo Mail|Proton Mail|Gmail)"
    r"|Get Outlook for (?:iOS|Android)"
    r"|Envoyé de mon (?:iPhone|iPad)|Von meinem (?:iPhone|iPad) gesendet"
    r"|Enviado desde mi (?:iPhone|iPad))[^\n]{0,60}$"
    r")",
    re.IGNORECASE | re.MULTILINE,
)

# The footer a firm's server adds to every message. Only as a paragraph of
# its own below something written, and only in a footer's words: "This
# email is the only instruction you need" is the lawyer.
_DISCLAIMER = re.compile(
    r"\n[ \t]*\n[ \t]*(?:CONFIDENTIALITY(?:\s+NOTICE)?\s*:|DISCLAIMER\s*:|IMPORTANT(?:\s+NOTICE)?\s*:|"
    r"This (?:e-?mail|message|communication)(?:\s*\(?(?:and|including) any [^\n]{0,40}?\)?)?"
    r"\s+(?:is|are|and|may|contains?|was)\b[^\n]{0,40}?"
    r"\b(?:confidential|privileged|intended (?:solely |only )?for)"
    r"|The information (?:contained )?in this (?:e-?mail|message)"
    r"|Privileged (?:and|&) Confidential)",
    re.IGNORECASE,
)

# A signature with no "--" above it: a sign-off, a name, a title, the firm,
# an address, telephone numbers, an email address, a website. A bare
# "Thanks" or "Cheers" is not taken as one here: it is also the whole of an
# acknowledgement, and is_acknowledgement and followup read "Thanks\nJim"
# themselves.
_VALEDICTION = re.compile(
    r"^(?:(?:(?:kind|best|warm|warmest|with (?:kind|best)|all the)\s+"
    r"(?:regards|wishes|thanks))|many thanks|"
    r"regards|best|sincerely|"
    r"yours(?:\s+(?:sincerely|faithfully|truly))?|"
    r"mit freundlichen grüßen|freundliche grüße|viele grüße|beste grüße|"
    r"(?:bien\s+)?cordialement|bien à vous|saludos(?:\s+cordiales)?|un saludo|atentamente|"
    r"met vriendelijke groet(?:en)?|cordiali saluti)[,.!]?$",
    re.IGNORECASE,
)
_CONTACT_TOKEN = re.compile(
    r"\S+@\S+\.\w+|(?:https?://|www\.)\S+"
    r"|\+\d[\d ()./-]{6,}\d|\(?\d{2,5}\)?[ .-]\d{3,4}[ .-]\d{3,4}\b",
)
_CONNECTORS = {"of", "and", "&", "at", "for", "the", "in", "de", "du", "|", "-", "–", "·", "•"}


def _is_contact(line: str) -> bool:
    rest = _CONTACT_TOKEN.sub(" ", line)
    return rest != line and len(re.findall(r"[^\W\d_]{2,}", rest)) <= 2


def _is_title(line: str) -> bool:
    words = line.split()
    return (0 < len(words) <= 8 and not re.search(r"[.?!:;]$", line)
            and all(w.lower() in _CONNECTORS or not w[:1].isalpha() or w[:1].isupper()
                    for w in words))


def _signature_start(paragraphs: list[list[str]]) -> int:
    """How many paragraphs are the message; the rest are the signature.

    From the bottom, a paragraph is signature when every line of it is a
    sign-off, a name or title, or contact details, and it has a sign-off or
    contact details in it, or it is a short name sitting on one that does.
    The first paragraph is always the message, and so is anything under a
    line ending in a colon: "Add a notice address:" then an address is an
    instruction.
    """
    keep = len(paragraphs)
    for i in range(len(paragraphs) - 1, 0, -1):
        lines = paragraphs[i]
        if not all(_VALEDICTION.match(ln) or _is_contact(ln) or _is_title(ln) for ln in lines):
            break
        anchored = any(_VALEDICTION.match(ln) or _is_contact(ln) for ln in lines)
        named = (keep < len(paragraphs) and len(lines) <= 2
                 and not any(ch.isdigit() for ln in lines for ch in ln))
        if not (anchored or named) or paragraphs[i - 1][-1].endswith(":"):
            break
        keep = i
    return keep


def _drop_signature(text: str) -> str:
    paragraphs = [[ln.strip() for ln in p.splitlines() if ln.strip()]
                  for p in re.split(r"\n[ \t]*\n", text.strip())]
    paragraphs = [p for p in paragraphs if p]
    if not paragraphs:
        return ""
    keep = _signature_start(paragraphs)
    # A sign-off in the last paragraph kept: "30\nKind regards,\nJim".
    last = paragraphs[keep - 1]
    for i, ln in enumerate(last[1:], 1):
        if _VALEDICTION.match(ln) and all(_is_title(x) or _is_contact(x) for x in last[i + 1:]):
            paragraphs[keep - 1] = last[:i]
            break
    return "\n\n".join("\n".join(p) for p in paragraphs[:keep])


def _quote_start(text: str) -> tuple[int, int, bool] | None:
    """Where the first quoted-history marker is: (start, end, is_attribution)."""
    found: list[tuple[int, int, bool]] = []
    for m in _ATTRIBUTION.finditer(text):
        if _is_attribution(text, m):
            found.append((m.start(), m.end(), True))
            break
    for pattern in (_HEADER_BLOCK, _SEPARATOR):
        m = pattern.search(text)
        if m:
            found.append((m.start(), m.end(), False))
    return min(found) if found else None


# Outlook, Apple Mail and phones type a curly apostrophe for "here's" and
# "don't"; every phrase and pattern that reads a lawyer's words is written
# with the straight one. Without this, "Here’s our playbook" was not a
# playbook and "they’ll renumber it" read as a request to renumber.
_APOSTROPHES = str.maketrans({"’": "'", "‘": "'", "ʼ": "'"})


def straight_apostrophes(text: str) -> str:
    """Curly and modifier apostrophes as the straight one."""
    return (text or "").translate(_APOSTROPHES)


def strip_reply(body: str, _depth: int = 0) -> str:
    """The words this person actually wrote, without quoted history or signature.

    Getting this wrong means reading an instruction out of a quoted thread, which
    is how an agent ends up making a change nobody asked for. Getting it wrong
    the other way cuts off what they asked for, so every marker is a whole
    line in the shape a mail client writes it. Apostrophes come back straight,
    so the phrases every command is matched against see one form.
    """
    text = straight_apostrophes(body).replace("\r\n", "\n").replace("\r", "\n")

    cut = _quote_start(text)
    if cut is not None:
        start, end, attribution = cut
        rest = text[end:]
        first = next((ln for ln in rest.splitlines() if ln.strip()), "")
        if (attribution and not text[:start].strip() and first.lstrip().startswith(">")
                and _depth == 0):
            # Answered inline, under the attribution, between quoted lines.
            return strip_reply("\n".join(ln for ln in rest.splitlines()
                                         if not ln.lstrip().startswith(">")), _depth + 1)
        text = text[:start]

    # Lines beginning with > are quoted.
    text = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith(">")
    )

    # Signature block: everything after a line that is exactly "--".
    text = re.split(r"\n--[ \t]*\n", "\n" + text, maxsplit=1)[0]

    # Confidentiality footers, which are long and appear on every firm email.
    text = _DISCLAIMER.split(text, maxsplit=1)[0]

    return _drop_signature(text).strip()


def _without_sign_off(text: str) -> str:
    """ "stop\n\nJim" is still "stop": trailing names, up to three capitalised
    words a line, with something above them."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    while len(lines) > 1:
        words = lines[-1].split()
        if not (len(words) <= 3 and not any(ch.isdigit() for ch in lines[-1])
                and all(w[:1].isupper() for w in words)):
            break
        lines.pop()
    return "\n".join(lines)


def route_deterministic(body: str, has_attachment: bool = False) -> Routed | None:
    """Route the cases that cannot be misread. Returns None to defer to the model."""
    text = strip_reply(body)
    normalised = re.sub(r"[^\w\s]", "", text).strip().lower()
    normalised = re.sub(r"\s+", " ", normalised)
    if normalised not in _EXACT_KEYS:
        signed = re.sub(r"[^\w\s]", "", _without_sign_off(text)).strip().lower()
        normalised = re.sub(r"\s+", " ", signed)

    if not normalised:
        return (
            Routed(Intent.NEW_VERSION, reason="attachment with no instructions")
            if has_attachment
            else Routed(Intent.REVIEW, reason="empty body")
        )

    # The whole message must be the phrase, not merely start with it. A
    # forwarded mail beginning "Revoke the licence..." is not a disconnect
    # request, and treating it as one would cut a lawyer off silently.
    if len(normalised.split()) <= _MAX_EXACT_WORDS and normalised in _EXACT_KEYS:
        return Routed(
            _EXACT_KEYS[normalised],
            reason=f"whole message is {normalised!r}",
        )

    return None


def _normalise_answer(text: str) -> str:
    """Lower-cased, with punctuation that carries meaning kept.

    Full stops and slashes are kept because an option may be a date, but a
    trailing one is sentence punctuation rather than part of the answer.
    """
    cleaned = re.sub(r"[^\w\s./-]", "", text).strip().lower()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" .,-")


def looks_like_an_answer(body: str, options: list[str]) -> str | None:
    """A short reply that matches an option the agent offered."""
    normalised = _normalise_answer(strip_reply(body))
    # "13 please", "30, thanks": the courtesy is not part of the answer.
    normalised = re.sub(r"^(?:please|pls)\s+|\s+(?:please|pls|thanks|thank you|thx|cheers)$",
                        "", normalised)
    if not normalised or len(normalised.split()) > 3:
        return None
    for option in options:
        if normalised == _normalise_answer(option):
            return option
    return None
