"""Checks that are only possible because the agent lives in the email.

A Word add-in sees the document. We see the document *and* the message it is
about to travel in: who it is addressed to, what the sender said it contains,
and what they said they had done to it. The gap between those is where a
specific and dangerous class of mistake lives, and nothing else in this market
is positioned to catch it.

These are the checks worth building first, because they are the ones a
competitor with a desktop add-in cannot copy without becoming a different
product.
"""

from __future__ import annotations

import re

from lra.models import Attachment, Finding, InboundEmail, Severity
from lra.pipeline import checks
from lra.pipeline.extract import ExtractedDoc

_ENTITY_IN_DOC = re.compile(
    r"\b([A-Z][A-Za-z&.\-]{2,}(?:\s+[A-Z][A-Za-z&.\-]{1,}){0,3})[, ]+"
    r"(?:LLC|L\.L\.C\.|Inc\.?|Ltd\.?|Limited|LP|LLP|PLC|Corp\.?|Corporation|GmbH)\b"
)

_FREE_MAIL = {
    "gmail.com", "outlook.com", "hotmail.com", "yahoo.com", "icloud.com",
    "proton.me", "protonmail.com", "aol.com",
}

# Things people claim in a covering email that the document has to actually do.
# Each of these fails when the substance of the claim is ABSENT from the file.
_CLAIM_PATTERNS = [
    (r"\bcapp?ed (?:the )?liability at ([^;\n]{1,40})", "liability cap"),
    (r"\b(?:changed|moved|updated) the (?:term|deadline|date) to ([^;\n]{1,40})",
     "changed date or term"),
    (r"\b(?:added|inserted) (?:a |an |the )?([^;\n]{1,40})", "addition"),
    (r"\bmade (?:it|the term) ([0-9]+ ?(?:days?|months?|years?))", "duration change"),
    (r"\bclean (?:copy|version)\b", "clean copy claim"),
    (r"\bno (?:tracked )?changes\b", "no-changes claim"),
    (r"\baccepted all (?:the |my |your |of the )?(?:tracked )?changes\b", "no-changes claim"),
    # "Final version" is tested by final_but_draft, against the markers a
    # draft carries, not by looking for the words in the document.
]

# A removal is the mirror image and cannot share that loop. It used to, so the
# lawyer who correctly struck the exclusivity clause was told the thing they
# had deliberately deleted was missing, while the one who said they removed it
# and attached the wrong draft — the mistake this check exists to catch — was
# told nothing at all.
_REMOVAL_CLAIM = re.compile(
    r"\b(?:removed|deleted|struck|took out) (?:the )?([^;\n]{1,40})", re.IGNORECASE
)


def recipient_mismatch(
    email: InboundEmail, doc: ExtractedDoc, external: list[str]
) -> list[Finding]:
    """The document names one company and the message is addressed to another.

    This is the wrong-attachment mistake, and it is the single worst thing a
    lawyer can do by email. It is also mechanically detectable, but only by
    something that can see both the document and the To line at once.
    """
    if not external:
        return []

    doc_text = "\n".join(b.text for b in doc.blocks[:60])
    entities = {_entity_name(m.group(1)) for m in _ENTITY_IN_DOC.finditer(doc_text)}
    entities = {e for e in entities if len(e) > 2}
    if not entities:
        return []

    domains = {
        a.split("@")[-1].lower()
        for a in external
        if a.split("@")[-1].lower() not in _FREE_MAIL
    }
    # The whole document, not just the parties clause. The notices clause names
    # the other side's solicitors, and a recipient who appears anywhere in the
    # document is somebody this document already expects to be read by.
    whole = "\n".join(b.text for b in doc.blocks).lower()

    def named_in_the_document(domain: str) -> bool:
        stem = domain.split(".")[0]
        return len(stem) > 2 and stem in whole

    domains = {d for d in domains if not named_in_the_document(d)}
    if not domains:
        return []

    def matches(entity: str, domain: str) -> bool:
        stem = domain.split(".")[0]
        words = [w for w in re.split(r"[^A-Za-z]+", entity) if len(w) > 2]
        return any(w.lower() in stem or stem in w.lower() for w in words)

    if any(matches(e, d) for e in entities for d in domains):
        return []

    named = ", ".join(sorted(entities)[:3])
    to = ", ".join(sorted(domains))
    return [
        Finding(
            # Worth a look, not "do not send". A draft going to the counterparty's
            # law firm matches no party in the document either, and that is the
            # normal way a draft travels: at blocker severity this told a lawyer
            # doing the ordinary thing that their attachment was wrong, which is
            # the false positive that gets the whole tool filtered into a folder.
            severity=Severity.SUBSTANTIVE,
            category="recipient",
            title=f"The document names {named} but this is addressed to {to}",
            explanation=(
                "No company named in the document matches the recipient's domain. "
                "That is often fine — drafts go to the other side's lawyers all the "
                "time — but it is also what a wrong attachment looks like, so it is "
                "worth one glance before this goes out."
            ),
            anchor=min(entities),
            confidence=0.55,
        )
    ]


# Words a run of capitals swallows at the start of a sentence. "Between Acme
# Holdings LLC" is not a company called "Between Acme Holdings", and an email
# that says so reads as though the reviewer cannot read.
_NOT_PART_OF_A_NAME = {
    "between", "and", "with", "by", "for", "among", "the", "this", "that",
    "dated", "made", "to", "from", "of", "is", "are",
}


def _entity_name(raw: str) -> str:
    words = raw.strip().split()
    while words and words[0].lower().strip(".,") in _NOT_PART_OF_A_NAME:
        words.pop(0)
    return " ".join(words).strip(" ,")


def claim_mismatch(email: InboundEmail, doc: ExtractedDoc,
                   content: bytes | None = None) -> list[Finding]:
    """The covering email says what was done. Check the document actually did it.

    'I've capped liability at $1M' is a testable claim. So is 'clean copy' when
    the file still carries tracked changes. Nothing outside the email can test
    either one.
    """
    body = (email.body or "").lower()
    doc_text = "\n".join(b.text for b in doc.blocks).lower()
    out: list[Finding] = _removal_still_in_the_document(body, doc_text)
    clean_claimed = False

    for pattern, label in _CLAIM_PATTERNS:
        m = re.search(pattern, body)
        if not m:
            continue

        if label in ("clean copy claim", "no-changes claim"):
            if content and not clean_claimed:
                clean_claimed = True
                out += _clean_but_marked_up(
                    content, "You called this a clean copy" if label == "clean copy claim"
                    else "Your email says the changes are done")
            continue

        claimed = _trim_claim(m.group(1) if m.lastindex else "")
        if not claimed or len(claimed) < 3:
            continue
        # Look for the substance of the claim in the document.
        tokens = [t for t in re.split(r"[^\w$%.,]+", claimed) if len(t) > 2]
        if tokens and not any(t in doc_text for t in tokens):
            out.append(
                Finding(
                    severity=Severity.SUBSTANTIVE,
                    category="claim",
                    title=f'Your email mentions "{claimed}" but I cannot find it in the document',
                    explanation=(
                        f"You described a {label} that I could not locate. Either it did "
                        "not make it into this version, or you attached the wrong draft."
                    ),
                    anchor="",
                    confidence=0.45,
                )
            )
    return out


def _removal_still_in_the_document(body: str, doc_text: str) -> list[Finding]:
    """"I took out the exclusivity clause" — and it is still there.

    Tested as a whole phrase, and only from two words up. A one-word claim
    ("I deleted the indemnity") is worthless as evidence: the word survives in
    a heading, a definition or a cross-reference in almost every document that
    ever contained the clause, and a finding built on that would fire on the
    lawyer who did exactly what they said.
    """
    m = _REMOVAL_CLAIM.search(body)
    if not m:
        return []
    claimed = _trim_claim(m.group(1))
    tidy = re.sub(r"\s+", " ", claimed).strip()
    if len(tidy.split()) < 2 or len(tidy) < 6:
        return []
    if tidy not in re.sub(r"\s+", " ", doc_text):
        return []
    return [
        Finding(
            severity=Severity.SUBSTANTIVE,
            category="claim",
            title=f'You said you removed "{tidy}", but it is still in the document',
            explanation=(
                "The covering email says it came out and I can still find it. Either "
                "the deletion did not make it into this version, or this is the draft "
                "from before the change."
            ),
            anchor="",
            confidence=0.6,
        )
    ]


def attachment_mismatch(email: InboundEmail, att: Attachment) -> list[Finding]:
    """The email describes one kind of document and a different one is attached.

    Read from the body only, and raised as a point to check rather than a
    reason not to send. A subject line in a live thread is whatever the first
    email said: "Re: NDA" three weeks later carries the MSA, and reading the
    subject told the lawyer, at blocker severity, that they had attached the
    wrong file when they had not.
    """
    body = (email.body or "").lower()
    kinds = {
        "nda": ("nda", "non-disclosure", "nondisclosure", "confidentiality agreement"),
        "engagement letter": ("engagement letter",),
        "spa": ("spa", "share purchase", "stock purchase", "purchase agreement"),
        "loi": ("loi", "letter of intent", "term sheet"),
        "msa": ("msa", "master services",),
        "lease": ("lease",),
        "employment agreement": ("employment agreement", "offer letter"),
    }
    # Whole words only. Substring matching read "Please take a look" as the
    # word "lease" and told the lawyer they had attached the wrong document.
    def mentions(haystack: str, words: tuple) -> bool:
        return any(
            re.search(rf"\b{re.escape(w)}\b", haystack, re.IGNORECASE) for w in words
        )

    mentioned = {k for k, words in kinds.items() if mentions(body, words)}
    named = {k for k, words in kinds.items() if mentions(att.filename.lower(), words)}
    if mentioned and named and not (mentioned & named):
        return [
            Finding(
                severity=Severity.SUBSTANTIVE,
                category="attachment",
                title=(
                    f"Your email talks about {', '.join(sorted(mentioned))} but the "
                    f"attachment is called {att.filename}"
                ),
                explanation="This may be the wrong file. Worth checking before sending.",
                anchor="",
                confidence=0.6,
            )
        ]
    return []


# "Schedule 2 of the Facility Agreement" and "Schedule 1 to the lease dated..."
# belong to another document. "Schedule 2 of this Agreement" and "Schedule 2 to
# the Agreement" belong to this one.
_ANNEX_ABBREVIATION = {"schedule": "sch", "exhibit": "exh", "annex": "annex", "appendix": "app"}
_OTHER_INSTRUMENT = re.compile(
    r"^\s+(?:to|of|attached\s+to)\s+"
    r"(?:the\s+(?!agreement\b)\w|(?!the\b|this\b|agreement\b)\w)",
    re.IGNORECASE,
)
# A heading that is just the word, its number supplied by Word's automatic
# numbering, which the extracted text does not carry. Every "Schedule 2" in the
# body would then look unattached.
_BARE_ANNEX_HEADING = re.compile(r"^\s*(SCHEDULE|EXHIBIT|ANNEX|APPENDIX)\s*$", re.IGNORECASE)


def _annexes_named(text: str, word: str) -> set[str]:
    """Every schedule number the text names, singly, in a list or as a range:
    "Schedule 2", "Schedules 1 and 2", "Sch 1-3", "Exhibits A, B and C"."""
    short = _ANNEX_ABBREVIATION[word]
    out: set[str] = set()
    for m in re.finditer(
        rf"\b(?:{word}s?|{short})\.?\s*([0-9]+|[A-Z])"
        rf"((?:\s*(?:,|and|&|-|–|to|through)\s*(?:[0-9]+|[A-Z]))*)\b",
        text, re.IGNORECASE,
    ):
        items = [m.group(1).upper()] + re.findall(r"\b(?:[0-9]+|[A-Z])\b", m.group(2))
        out.update(items)
        if re.search(r"-|–|\bto\b|\bthrough\b", m.group(2)) and len(items) == 2:
            a, b = items
            if a.isdigit() and b.isdigit():
                out.update(str(n) for n in range(int(a), int(b) + 1))
            elif len(a) == 1 and len(b) == 1:
                out.update(chr(c) for c in range(ord(a), ord(b) + 1))
    return out


def missing_annexes(email: InboundEmail, doc: ExtractedDoc, att: Attachment) -> list[Finding]:
    """A schedule or exhibit the document refers to that is nowhere in the email.

    Only for a document on its way to signature. A working draft says "Schedule
    2 to follow" and everyone knows; an execution version that promises a
    schedule and carries none is going out incomplete. Judged against the whole
    message, not the file: a schedule sent as its own attachment, or one the
    covering note says is coming separately, is not missing. That is the part
    a Word add-in cannot see, and it is what keeps this from firing on every
    document whose schedules are bound separately.

    Where the document carries some schedules and refers to one it lacks,
    `checks.cross_references` already reports it; this check speaks only when
    the document has none of that kind at all.
    """
    if not checks.looks_like_execution_version(doc):
        return []
    present: set[str] = set()
    for block in doc.blocks:
        # The display text, so a schedule numbered by Word ("Schedule %1")
        # is seen with its number.
        title = (checks._ANNEX_TITLE.match(block.display)
                 or _BARE_ANNEX_HEADING.match(block.display))
        if title:
            present.add(title.group(1).lower())

    elsewhere = " ".join(
        a.filename for a in email.attachments if a is not att
    ) + " " + f"{email.subject} {email.body}"
    named_elsewhere = {word: _annexes_named(elsewhere, word) for word in _ANNEX_ABBREVIATION}

    out: list[Finding] = []
    seen: set[tuple[str, str]] = set()
    for block in doc.blocks:
        for m in checks._SECTION_REF.finditer(block.text):
            word, target = m.group(1).lower(), m.group(2).upper()
            if word in checks._BODY_REF_WORDS or word in present or (word, target) in seen:
                continue
            if "." in target:
                # "Schedule 3.2" is the part of an American disclosure schedule
                # answering section 3.2, a document delivered on its own as a
                # matter of course. It told a lawyer sending a correct stock
                # purchase agreement that a schedule was missing.
                continue
            if _OTHER_INSTRUMENT.match(block.text[m.end():]):
                continue
            if target in named_elsewhere[word]:
                continue
            seen.add((word, target))
            label = m.group(0)
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="annex",
                title=f"{label} is referred to, but is not attached",
                explanation=(
                    f"The document relies on {label} and it is not in the file, not "
                    "among the other attachments, and not mentioned in your email. "
                    "On an execution version that is a document going out "
                    "incomplete."
                ),
                anchor=label,
                question=f"Is {label} being sent separately, or should it be in the document?",
                confidence=0.7,
            ))
    return out


def own_personal_address(email: InboundEmail, external: list[str]) -> list[Finding]:
    """The document is also going to what looks like the sender's own webmail.

    Mailing work home is the commonest way a client file ends up outside the
    firm's systems, and it is the one recipient mistake a gateway product
    catches that the document-against-recipient check above cannot: nothing in
    the document is wrong, the address is.

    Deliberately narrow. A client on gmail.com is normal, and flagging every
    free-mail recipient would fire on a large share of private-client work. So
    this needs the address to look like the sender: the same mailbox name, or
    both of their names in it.
    """
    sender_local = email.from_address.split("@")[0].lower()
    name_parts = {
        part for part in re.split(r"[^a-z]+", (email.from_name or "").lower())
        if len(part) >= 3
    }
    for address in external:
        local, _, domain = address.lower().partition("@")
        if domain not in _FREE_MAIL:
            continue
        squashed = re.sub(r"[^a-z]", "", local)
        same_mailbox = re.sub(r"[^a-z]", "", sender_local) == squashed and len(squashed) >= 4
        both_names = len(name_parts) >= 2 and all(part in squashed for part in name_parts)
        if same_mailbox or both_names:
            return [
                Finding(
                    severity=Severity.SUBSTANTIVE,
                    category="recipient",
                    title=f"This is also going to {address}, which looks like your "
                          "own personal address",
                    explanation=(
                        "A copy in a personal mailbox is outside the firm's "
                        "retention and security, and most client engagement terms "
                        "do not allow it. If that was deliberate, ignore this."
                    ),
                    anchor="",
                    confidence=0.8,
                )
            ]
    return []


# --------------------------------------------------------------------------
# What leaves the firm: the note, the file's name and the file, read together
# --------------------------------------------------------------------------


def _own_words(email: InboundEmail) -> str:
    """The sender's own words, without the quoted thread or the signature.
    A quoted email from the other side names their companies and their
    versions, and none of that is a claim about this attachment."""
    from lra.pipeline.router import strip_reply

    return strip_reply(email.body or "")


def _sentences(text: str) -> list[str]:
    """Sentences, with a mail client's line wrapping undone: Gmail and Outlook
    break a long plain-text line in two, and "Attached is the SPA and the" /
    "disclosure letter." is one sentence. A blank line still ends one."""
    text = re.sub(r"[ \t]*(?<!\n)\n(?!\s*\n)[ \t]*", " ", text)
    return [s for s in re.split(r"(?<=[.!?])\s+|\n\s*\n", text) if s.strip()]


# A sentence that hands something over now. Without one, "the disclosure
# letter" or "v4" is talk about a document, not a statement of what is attached.
_HANDING_OVER = re.compile(
    r"\battach(?:ed|ing)?\b|\benclos(?:e|ed|ing)\b|\bplease find\b|\bherewith\b|"
    r"\bhere (?:is|are)\b|\bhere's\b|\bsending (?:you |over )?(?:the|our|a)\b|"
    r"\bthis is (?:the|our)\b",
    re.IGNORECASE,
)
# ...and the sentences that look like that but are about some other moment or
# somebody else's file.
_NOT_NOW = re.compile(
    r"\b(?:will|to follow|follows? separately|separately|shortly|later|tomorrow|"
    r"next week|previous(?:ly)?|earlier|yesterday|last (?:week|time|turn|draft)|"
    r"once|until)\b|'ll\b|\byou (?:sent|attached|provided|shared|circulated)\b|"
    r"\b(?:they|he|she) (?:sent|attached|provided|shared|circulated)\b",
    re.IGNORECASE,
)


def _handed_over(text: str) -> list[str]:
    return [s for s in _sentences(text) if _HANDING_OVER.search(s) and not _NOT_NOW.search(s)]


# What a document on its way to be signed is called. "Final draft" is not
# one: a final draft is still a draft and may say so.
_FINAL_WORDS = re.compile(
    r"\b(?:final|execution|signing|engrossment|engrossed|agreed[- ]form)\s+"
    r"(?:version|copy|form|documents?|agreement)\b|"
    r"\bfor (?:signature|signing|execution)\b|\bready (?:to|for) (?:sign|signature|"
    r"signing|execution)\b",
    re.IGNORECASE,
)
_FINAL_NAME = re.compile(
    r"(?<![A-Za-z])(?:final|execution|exec|engrossment|engrossed|signing|"
    r"for[ _-]sig(?:nature)?)(?![A-Za-z])",
    re.IGNORECASE,
)
# A draft's own markings: "DRAFT", "Draft 3: 12 March 2026", "Draft for
# discussion", "Subject to contract". Read in the headers, footers and at the
# top of the document, where they are put.
_DRAFT_MARK = re.compile(
    r"(?:^|\s)(?:DRAFT\b|Draft\s*(?:\d|\(|:|[-–—]|for\b|subject\b|only\b|$)|"
    r"SUBJECT TO CONTRACT|Subject to contract)",
)


def sent_as_final(email: InboundEmail, att: Attachment) -> str:
    """Why this attachment reads as the signing version, in words to quote back,
    or "" if nothing says it is."""
    for sentence in _handed_over(_own_words(email)):
        m = _FINAL_WORDS.search(sentence)
        if m:
            return f'your note calls it the "{m.group(0).strip()}"'
    stem = att.filename.rsplit(".", 1)[0]
    if _FINAL_NAME.search(stem) and not re.search(r"(?<![A-Za-z])draft(?![A-Za-z])", stem,
                                                  re.IGNORECASE):
        return f"it is named {att.filename}"
    return ""


def final_but_draft(email: InboundEmail, doc: ExtractedDoc, att: Attachment) -> list[Finding]:
    """A version sent as final that still says it is a draft.

    A DRAFT watermark on a draft is correct, which is why checks.leftovers
    reports it as an observation and nothing more. On the copy the note calls
    the execution version, or that is named "final", it is the thing that
    makes the other side ask whether they have the right document, and a
    highlight left on it is a point somebody meant to come back to.
    """
    why = sent_as_final(email, att)
    if not why:
        return []
    out: list[Finding] = []
    where = _draft_marks(doc, att.content)
    if where:
        shown = checks._join([f'{w} ("{t}")' if t != "DRAFT watermark" else w
                              for w, t in where])
        out.append(Finding(
            severity=Severity.BLOCKER,
            category="draft",
            title="This is going out as final, but it is still marked as a draft",
            explanation=(
                f"{why[0].upper()}{why[1:]}, and the draft marking is still in {shown}. "
                "Take it off before the document is sent for signature."
            ),
            anchor=where[0][1] if where[0][0] == "the body" else "",
            confidence=0.9,
        ))
    highlighted = _highlighted(att.content)
    if highlighted:
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="draft",
            title=f'Text is still highlighted in a version going out as final: "{highlighted[0]}"',
            explanation=(
                f"{why[0].upper()}{why[1:]}, and {len(highlighted)} passage"
                f"{'s are' if len(highlighted) != 1 else ' is'} still highlighted. "
                "A highlight is usually a point someone meant to come back to."
            ),
            anchor=highlighted[0],
            question=(f'"{highlighted[0]}" is still highlighted in a version going out '
                      "as final. Has that point been dealt with?"),
            confidence=0.75,
        ))
    return out


def _draft_marks(doc: ExtractedDoc, content: bytes | None) -> list[tuple[str, str]]:
    """(where, the marking) for each place the document calls itself a draft."""
    found: list[tuple[str, str]] = []
    for block in doc.blocks[:6]:
        m = _DRAFT_MARK.search(block.text)
        if m and len(block.text) <= 80:
            found.append(("the body", block.text.strip()))
            break
    parts = _parts(content)
    for name, xml in parts.items():
        if not re.match(r"^word/(header|footer)\d*\.xml$", name):
            continue
        text = " ".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))
        m = _DRAFT_MARK.search(text)
        watermark = re.search(r"<w:pict|<v:shape[^>]*PowerPlusWaterMark|watermark", xml,
                              re.IGNORECASE) and re.search(r'string="[^"]*DRAFT', xml,
                                                           re.IGNORECASE)
        label = checks._part_label(name)
        if watermark:
            found.append((f"{label} (as a watermark)", "DRAFT watermark"))
        elif m:
            snippet = text[m.start():m.start() + 40].strip()
            found.append((label, snippet.split("  ")[0]))
    # One of each place is enough to make the point.
    seen: set[str] = set()
    return [(w, t) for w, t in found if not (w in seen or seen.add(w))]


def _parts(content: bytes | None) -> dict[str, str]:
    """The Word text parts of a package, or nothing if it is not one."""
    if not content or content[:2] != b"PK":
        return {}
    import zipfile
    from io import BytesIO

    try:
        z = zipfile.ZipFile(BytesIO(content))
        return {n: z.read(n).decode("utf-8", errors="replace")
                for n in z.namelist() if checks._TEXT_PART.match(n)}
    except Exception:  # noqa: BLE001 - an unreadable package is reported elsewhere
        return {}


def _highlighted(content: bytes | None) -> list[str]:
    """The highlighted passages in the body, each as its words."""
    xml = _parts(content).get("word/document.xml", "")
    if "w:highlight" not in xml:
        return []
    passages: list[str] = []
    for para in re.findall(r"<w:p[ >].*?</w:p>", xml, re.DOTALL):
        current: list[str] = []
        for run in re.findall(r"<w:r[ >].*?</w:r>", para, re.DOTALL) + [""]:
            if run and re.search(r'<w:highlight w:val="(?!none")', run):
                current.append("".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", run)))
                continue
            text = "".join(current).strip()
            if text:
                passages.append(text[:60])
            current = []
    return passages


def _clean_but_marked_up(content: bytes, said: str) -> list[Finding]:
    """A file described as clean that carries tracked changes or comments."""
    parts = _parts(content)
    revised = any(checks._REVISION_MARK.search(x) for x in parts.values())
    comments = 0
    if parts:
        import zipfile
        from io import BytesIO

        try:
            z = zipfile.ZipFile(BytesIO(content))
            if "word/comments.xml" in z.namelist():
                comments = z.read("word/comments.xml").decode(
                    "utf-8", errors="replace").count("<w:comment ")
        except Exception:  # noqa: BLE001
            comments = 0
    if not revised and not comments:
        return []
    carries = checks._join(
        (["tracked changes"] if revised else [])
        + ([f"{comments} comment{'s' if comments != 1 else ''}"] if comments else [])
    )
    return [Finding(
        severity=Severity.BLOCKER,
        category="claim",
        title=f"{said}, but it still has {carries}",
        explanation=(
            f"The recipient will see the {carries} the moment they open it. Accept the "
            "changes and delete the comments, or reply \"clean copy\" and I will."
        ),
        anchor="",
        confidence=0.95,
    )]


def clean_by_name(email: InboundEmail, att: Attachment) -> list[Finding]:
    """"Acme SPA v4 (clean).docx" with the markup still in it.

    Only when the email itself does not already call it clean: one finding
    about one claim."""
    if re.search(r"\bclean (?:copy|version)\b|\bno (?:tracked )?changes\b|\baccepted all\b",
                 email.body or "", re.IGNORECASE):
        return []
    stem = att.filename.rsplit(".", 1)[0]
    if not re.search(r"(?<![A-Za-z])clean(?![A-Za-z])", stem, re.IGNORECASE):
        return []
    return _clean_but_marked_up(att.content, f"The file is called {att.filename}")


_ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
                  "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10}
_VERSION_IN_WORDS = re.compile(
    r"(?<![A-Za-z0-9])(?:v|ver\.?|version|draft|turn)\s?(\d{1,2})(?![\d])"
    r"|\b(" + "|".join(_ORDINAL_WORDS) + r")\s+(?:draft|version|turn)\b",
    re.IGNORECASE,
)
_VERSION_IN_NAME = re.compile(
    r"(?<![A-Za-z])(?:v|ver|version|draft|turn|rev)[ ._-]?(\d{1,3})(?!\d)|\d{4,}v(\d{1,3})(?!\d)",
    re.IGNORECASE,
)


def _versions(text: str) -> set[int]:
    out: set[int] = set()
    for m in _VERSION_IN_WORDS.finditer(text):
        out.add(int(m.group(1)) if m.group(1) else _ORDINAL_WORDS[m.group(2).lower()])
    return out


def version_mismatch(email: InboundEmail, doc: ExtractedDoc, att: Attachment) -> list[Finding]:
    """The note says v4 and the file is v3.

    The commonest wrong attachment of all: the right document, the version
    before. Only when the note names exactly one version in a sentence that
    hands the file over ("attached is v4"), so "v4 takes in your comments on
    v3" says nothing, and only against a version the file states: in its name,
    or failing that in its footer, where a document system stamps it.
    """
    said: set[int] = set()
    for sentence in _handed_over(_own_words(email)):
        said |= _versions(sentence)
    if len(said) != 1:
        return []
    (claimed,) = said
    m = _VERSION_IN_NAME.search(att.filename.rsplit(".", 1)[0])
    if m:
        actual, where = int(m.group(1) or m.group(2)), f"the file attached is {att.filename}"
    else:
        footers = " ".join(
            " ".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", xml))
            for name, xml in _parts(att.content).items()
            if re.match(r"^word/footer\d*\.xml$", name)
        )
        stamps = {int(a or b) for a, b in _VERSION_IN_NAME.findall(footers)}
        if len(stamps) != 1:
            return []
        (actual,) = stamps
        where = f"its footer says version {actual}"
    if actual == claimed:
        return []
    return [Finding(
        severity=Severity.SUBSTANTIVE,
        category="attachment",
        title=f"Your note says version {claimed}, but {where}",
        explanation=(
            "The note and the file disagree about which version this is. Either the "
            "note is out of date, or this is the version before (or after) the one "
            "you meant to send."
        ),
        anchor="",
        question=f"Your note says version {claimed}, but {where}. Is this the version "
                 "you meant to send?",
        confidence=0.75,
    )]


def party_in_note_not_in_document(email: InboundEmail, doc: ExtractedDoc,
                                  att: Attachment) -> list[Finding]:
    """The note names a company the attached document never mentions.

    "Attached is the Bluewater NDA" with the Acme NDA attached. Read from the
    sender's own words only. A law firm (an LLP) is left out, because the note
    names the other side's lawyers all the time; so is the sender's own firm.
    """
    from lra.pipeline.outbound import _named_in

    # Only in a sentence that hands the file over: "attached is the NDA with
    # Bluewater". A bank or an adviser named anywhere else in the note is
    # somebody the note is talking about, not what the attachment is.
    words = "\n".join(_handed_over(_own_words(email)))
    if not words.strip():
        return []
    doc_text = "\n".join(b.text for b in doc.blocks)
    if not any(checks._ENTITY_ANY_CASE.finditer(doc_text)):
        return []        # a document naming no company gives nothing to compare
    haystack = doc_text.lower() + "\n" + att.filename.lower()
    own = email.from_address.split("@")[-1].split(".")[0].lower()
    out: list[Finding] = []
    seen: set[str] = set()
    for m in checks._ENTITY_ANY_CASE.finditer(words):
        base = checks._entity_base(m.group(1))
        name = f"{base} {m.group(2)}".strip()
        if (not base or m.group(2).upper() == "LLP" or name.lower() in seen
                or own in base.lower().replace(" ", "") or _named_in(base, haystack)):
            continue
        seen.add(name.lower())
        parties = [p for p, _ in checks._parties(doc)[0]][:3]
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="attachment",
            title=f"Your note mentions {name}, but the attached document never does",
            explanation=(
                ("The document is between " + checks._join(parties) + ". "
                 if len(parties) >= 2 else "")
                + "If the note is right, this may be the wrong attachment, or a "
                "document started from another deal's."
            ),
            anchor="",
            question=f"Your note mentions {name}, but the attached document never does. "
                     "Is this the right document?",
            confidence=0.65,
        ))
    return out


# Documents that travel beside the main one, by the words a note uses for
# them. The main document's own kind is attachment_mismatch's business.
_COMPANIONS = {
    "the blackline": ("blackline", "black-line", "comparison", "redline", "mark-up",
                      "markup", "marked-up", "marked up", "compare"),
    "the disclosure letter": ("disclosure letter",),
    "the side letter": ("side letter",),
    "the signature pages": ("signature pages", "signature page", "sig pages", "sig page"),
    "the board minutes": ("board minutes",),
    "the disclosure schedules": ("disclosure schedule",),
    "the funds flow": ("funds flow",),
}


def promised_attachment_missing(email: InboundEmail, doc: ExtractedDoc,
                                att: Attachment) -> list[Finding]:
    """The note says something is attached that is not.

    "Attached are a clean copy and a blackline", with one file. Only in a
    sentence that hands the file over now, and only for documents that travel
    beside the main one; a document said to be "to follow" or "sent
    separately" is not missing.
    """
    from lra.pipeline import intake

    sentences = _handed_over(_own_words(email))
    if not sentences:
        return []
    names = " ".join(re.sub(r"[_\-.]+", " ", a.filename.lower()) for a in email.attachments)
    opening = " ".join(b.text for b in doc.blocks[:15]).lower()
    out: list[Finding] = []
    for label, words in _COMPANIONS.items():
        said = next((s for s in sentences if any(
            re.search(rf"\b{re.escape(w)}s?\b", s, re.IGNORECASE) for w in words)), None)
        if said is None:
            continue
        if any(re.search(rf"\b{re.escape(w)}", names) for w in words):
            continue
        if label != "the blackline" and any(w in opening for w in words):
            continue
        if label == "the blackline" and any(
                intake._looks_like_a_comparison(a) for a in email.attachments):
            continue
        out.append(Finding(
            severity=Severity.SUBSTANTIVE,
            category="attachment",
            title=f"Your note says {label} is attached, but it is not",
            explanation=(
                f'You wrote "{said.strip()[:120]}", and none of the '
                f"{len(email.attachments)} attachment"
                f"{'s' if len(email.attachments) != 1 else ''} is {label[4:]}."
            ),
            anchor="",
            question=f"Your note says {label} is attached, but it is not. Should it be?",
            confidence=0.7,
        ))
    return out


# A marking that says the document is for the firm only. Not "privileged and
# confidential", which is on half of what goes to a client, and not "not for
# circulation", which drafts between the parties carry.
_INTERNAL_MARK = re.compile(
    r"\binternal (?:use only|only|draft|discussion (?:draft|only)|working draft|"
    r"comments? only)\b|\bfor internal (?:use|discussion|review)(?: only)?\b|"
    r"\bfirm internal\b|\bnot to be sent to (?:the )?(?:client|other side|counterparty)\b",
    re.IGNORECASE,
)


def internal_marking_going_out(email: InboundEmail, doc: ExtractedDoc, att: Attachment,
                               external: list[str]) -> list[Finding]:
    """A document marked for internal use, on a message to someone outside."""
    if not external:
        return []
    places = [("the top of the document", " ".join(b.text for b in doc.blocks[:8]))]
    places += [(checks._part_label(n),
                " ".join(re.findall(r"<w:t(?:\s[^>]*)?>([^<]*)</w:t>", x)))
               for n, x in _parts(att.content).items()
               if re.match(r"^word/(header|footer)\d*\.xml$", n)]
    for where, text in places:
        m = _INTERNAL_MARK.search(text)
        if m:
            return [Finding(
                severity=Severity.SUBSTANTIVE,
                category="recipient",
                title=f'The document is marked "{m.group(0)}" and this is going to '
                      f"{', '.join(sorted(external)[:2])}",
                explanation=(
                    f"The marking is in {where}. If this version is meant to go "
                    "outside the firm, take the marking off; if it is not, this is "
                    "the wrong file or the wrong recipient."
                ),
                anchor=m.group(0) if where == "the top of the document" else "",
                question=(f'The document is marked "{m.group(0)}" and this is going '
                          "outside the firm. Is this version meant to go?"),
                confidence=0.75,
            )]
    return []


def other_attachments(email: InboundEmail, att: Attachment) -> list[Finding]:
    """Every other file on the message, checked for what travels with it.

    The review reads one document. What leaves the firm is every attachment:
    the PDF exhibit with the partner's sticky notes, the deck with speaker
    notes, the workbook with a hidden sheet.
    """
    from lra.pipeline import intake, outbound

    out: list[Finding] = []
    for other in email.attachments:
        if other is att or intake._is_noise(other):
            continue
        out += outbound.other_files(other.content, other.filename)
    return out


def run_all(
    email: InboundEmail,
    doc: ExtractedDoc,
    att: Attachment,
    external: list[str],
) -> list[Finding]:
    out: list[Finding] = []
    for fn, args in (
        (recipient_mismatch, (email, doc, external)),
        (claim_mismatch, (email, doc, att.content)),
        (clean_by_name, (email, att)),
        (attachment_mismatch, (email, att)),
        (own_personal_address, (email, external)),
        (missing_annexes, (email, doc, att)),
        (final_but_draft, (email, doc, att)),
        (version_mismatch, (email, doc, att)),
        (party_in_note_not_in_document, (email, doc, att)),
        (promised_attachment_missing, (email, doc, att)),
        (internal_marking_going_out, (email, doc, att, external)),
        (other_attachments, (email, att)),
    ):
        try:
            out.extend(fn(*args))
        except Exception:
            import logging

            logging.getLogger(__name__).exception("email check %s failed", fn.__name__)
    return out


# Trailing clauses ("as discussed", "which we agreed") are not part of the claim.
_CLAIM_TAIL = re.compile(
    r"\s+(?:as|which|that|per|and|so|since|because|but|before|after|when)\b.*$",
    re.IGNORECASE,
)


def _trim_claim(raw: str) -> str:
    """Cut a captured claim back to the thing actually being claimed."""
    claimed = raw.split(".")[0] if not re.match(r"^\s*\$?[\d,.]+$", raw.strip()) else raw
    claimed = _CLAIM_TAIL.sub("", claimed)
    return claimed.strip().rstrip(".,;")
