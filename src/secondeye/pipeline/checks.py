"""Deterministic document checks.

These are the checks Litera Check, Contract Companion and DocXtools have taught
every firm to expect: defined terms, cross-references, numbering, amounts,
dates, names, placeholders, and metadata leftovers. See docs/litera-parity.md.

They belong in code, not in the model. They are exact, they are free, they run
in milliseconds, and they never hallucinate. Running them first also makes the
agent better: it is told what the deterministic pass already found, so it stops
spending attention on mechanical errors and spends it on the judgment calls
nothing but a model can make.

Every check here returns findings with verbatim anchors, so the redliner can
locate them.
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as _field
from io import BytesIO
from itertools import pairwise

from secondeye.models import Finding, Severity
from secondeye.pipeline.extract import ExtractedDoc

# --------------------------------------------------------------------------
# Placeholders: the single most embarrassing thing to leave in a sent document
# --------------------------------------------------------------------------

PLACEHOLDER_PATTERNS = [
    (r"«[^»\n]{0,60}»", "merge-field placeholder"),
    (r"\bTBD\b", "TBD"),
    (r"\bTK\b", "TK"),
    (r"\bT\.?B\.?C\.?\b", "TBC"),
    # XX only where it stands for a figure: after a currency, grouped like a
    # number, as a year, or before a unit. A bare "XX" is also a Roman
    # numeral, and "Title XX of the Social Security Act" is a statute.
    (r"(?:[£$€]|\b(?:US\$|USD|GBP|EUR)\s?)X{2,}(?:[,.]X+)*\b", "XX placeholder"),
    (r"\bX{2,}(?:[,.]X{2,})+\b", "XX placeholder"),
    (r"\b(?:19|20)XX\b", "XX placeholder"),
    (r"\bX{2,}\s+(?:days?|weeks?|months?|years?|business\s+days?|per\s*cent\.?|percent|%)",
     "XX placeholder"),
    (r"\bLOREM IPSUM\b", "lorem ipsum"),
    (r"\bINSERT\b", "INSERT marker"),
]

# A note from one drafter to another: "[Note to draft: Buyer to confirm]",
# "[NTD: check with tax]". Never meant for the other side, and longer than a
# blank, so it has a pattern of its own.
_DRAFTING_NOTE = re.compile(
    r"\[\s*(?:note\s+to\s+(?:draft|drafter|client|[A-Z][\w&]*(?:\s+[A-Z][\w&]*){0,3})|"
    r"NTD|drafting\s+note|DN)\s*:[^\[\]]{0,400}\]",
    re.IGNORECASE,
)

# Bracketed text is the hard case. `[COUNTERPARTY NAME]` is a placeholder;
# `[Compl. ¶ 14]`, `[id. ¶ 17]`, `[see Ex. A]`, `[emphasis added]` and `[sic]`
# are ordinary legal citation practice and appear throughout any brief.
#
# Detection is POSITIVE rather than exclusionary. An earlier version listed the
# benign forms and flagged everything else, which raised three blockers on a
# correct litigation memorandum: the worst possible failure, because it tells a
# lawyer not to send a document that is fine.
# The innermost brackets, so "[within [x] days]" yields the blank "[x]".
_BRACKET = re.compile(r"\[([^\[\]\n]{0,80})\]")

_PLACEHOLDER_WORDS = re.compile(
    r"\b(insert|tbd|tbc|todo|placeholder|xxx+|lorem)\b", re.IGNORECASE
)
# Underscores, dots and the bullets a template uses for a blank: [●], [•], [_].
_FILLER_ONLY = re.compile(r"^[\s_\-•·●○◆◼■▪⚫]{1,}$")
# [x], [xx], [X]: the blank a UK drafter types when the figure is not agreed.
_X_ONLY = re.compile(r"^x{1,3}$", re.IGNORECASE)
# A description of what goes here, in lower case: [date], [name of bank],
# [address for service]. Only the nouns a blank is named after, so that
# bracketed optional wording ("[or such later date]") is left alone.
_DESCRIBED_BLANK = re.compile(
    r"^(?:the\s+|insert\s+)?(?:date|name|names|address|amount|figure|sum|number|"
    r"price|percentage|details|email|e-mail|telephone|phone|title|jurisdiction|"
    r"company\s+number|registered\s+number|account\s+number|sort\s+code|signatory|"
    r"party|entity|counterparty|time|period|rate)"
    r"(?:\s+(?:of|for)\s+[^\]]{1,60})?$"
)
# Deliberate and correct: a clause kept for numbering, a redaction, an
# ellipsis in a quotation. Each is ordinary drafting and none is a blank.
_DELIBERATE = re.compile(
    r"^(?:reserved|intentionally\s+(?:omitted|left\s+blank|deleted)|redacted|"
    r"omitted|deleted|not\s+used|\*+|\.{3,}|…|(?:\.\s*){3,}|"
    r"confidential(?:\s+treatment\s+requested)?|text\s+omitted)\.?$",
    re.IGNORECASE,
)


def _bracket_is_placeholder(inner: str) -> bool:
    """True only when bracketed text is unmistakably an unfilled blank."""
    text = inner.strip()
    if not text:
        return True
    if _DELIBERATE.match(text):
        return False
    if _FILLER_ONLY.match(text) or _X_ONLY.match(text):
        return True
    if _DESCRIBED_BLANK.match(text):
        return True
    # A currency with a blank for the figure: [£●], [$[x]] is caught by [x].
    if re.fullmatch(r"[£$€]\s*[_●•x.]+", text, re.IGNORECASE):
        return True
    if _PLACEHOLDER_WORDS.search(text):
        return True
    # ALL CAPS with no lower-case letters: [COUNTERPARTY NAME], [DATE], [AMOUNT].
    letters = [c for c in text if c.isalpha()]
    return len(letters) >= 2 and all(c.isupper() for c in letters)


# A run of underscores is a line to sign on far more often than it is an
# unfilled blank. Reporting them made every unsigned contract come back with
# three blockers, on precisely the documents this product exists to check.
_UNDERSCORES = re.compile(r"_{3,}")
_SIGNATURE_LINE = re.compile(
    r"^\s*(?:by|name|title|its|date|dated|signed|signature|witness|print(?:ed)?"
    r"\s+name|position|for\s+and\s+on\s+behalf|director|secretary|authori[sz]ed"
    r"\s+signator(?:y|ies)|per\s*:|/s/|(?:designated\s+)?member|partner|trustee|"
    r"attorney)\b",
    re.IGNORECASE,
)
_EXECUTION_CONTEXT = re.compile(
    r"in\s+witness\s+whereof|executed\s+(?:as\s+a\s+deed|and\s+delivered)|"
    r"signature\s+page|signed\s+for\s+and\s+on\s+behalf",
    re.IGNORECASE,
)
# How far below an execution line its labelled rules can run. An English deed
# block is a line for each director and witness, their names and addresses.
_EXECUTION_REACH = 10
# "Member: ______", "Authorised Signatory ____": a short label and a rule.
_LABELLED_RULE = re.compile(r"^\s*[A-Za-z][A-Za-z .'()/-]{0,40}:?\s*_{3,}[\s_.]*$")


def _is_signature_line(text: str, nearby: str, after_execution: bool = False) -> bool:
    """True when underscores here are a place to sign, not a blank to fill."""
    stripped = text.strip()
    if _SIGNATURE_LINE.match(stripped):
        return True
    if _EXECUTION_CONTEXT.search(nearby):
        return True
    if after_execution and _LABELLED_RULE.match(stripped):
        return True
    # A line that is only underscores and punctuation is a rule to sign on.
    return bool(re.fullmatch(r"[\s_\-.:/]*", stripped))


def placeholders(doc: ExtractedDoc) -> list[Finding]:
    out: list[Finding] = []
    texts = [b.text for b in doc.blocks]
    last_execution = -_EXECUTION_REACH - 1
    for position, block in enumerate(doc.blocks):
        # "SIGNED by John Brown for and on behalf of BLUEWATER CAPITAL LLP"
        # opens a block as surely as "IN WITNESS WHEREOF" does.
        if _EXECUTION_CONTEXT.search(block.text) or _SIGNATURE_START.match(block.text):
            last_execution = position
        nearby = " ".join(texts[max(0, position - 3): position + 1])
        after_execution = position - last_execution <= _EXECUTION_REACH
        for m in _UNDERSCORES.finditer(block.text):
            if _is_signature_line(block.text, nearby, after_execution):
                continue
            out.append(_placeholder_finding(m.group(0), "blank"))
    for block in doc.blocks:
        # A drafting note, then bracketed blanks, then bare markers; each
        # reported once, by the widest thing that contains it, so that
        # "[INSERT AMOUNT]" is not also an "INSERT marker".
        claimed: list[tuple[int, int]] = []

        def inside(start: int, claimed=claimed) -> bool:
            return any(a <= start < b for a, b in claimed)

        for m in _DRAFTING_NOTE.finditer(block.text):
            out.append(_drafting_note_finding(m.group(0)))
            claimed.append((m.start(), m.end()))
        for m in _BRACKET.finditer(block.text):
            if inside(m.start()):
                continue
            if _bracket_is_placeholder(m.group(1)):
                out.append(_placeholder_finding(m.group(0).strip(),
                                                "square-bracket placeholder"))
                claimed.append((m.start(), m.end()))
        for pattern, label in PLACEHOLDER_PATTERNS:
            for m in re.finditer(pattern, block.text):
                if not inside(m.start()):
                    out.append(_placeholder_finding(m.group(0).strip(), label))
                    claimed.append((m.start(), m.end()))
    return _dedupe(out)


def _placeholder_finding(shown: str, label: str) -> Finding:
    # "Unfilled TBD: TBD" said the same thing twice.
    title = (f"Unfilled {label}" if shown.strip("[]").lower() == label.lower()
             else f"Unfilled {label}: {shown}")
    return Finding(
        severity=Severity.BLOCKER,
        category="placeholder",
        title=title,
        explanation=(
            "This was never filled in. Sending it tells the recipient the document "
            "was not finished. Tell me what belongs here and I will fill it in "
            "everywhere it appears."
        ),
        # The blank itself, so a reply carrying the value can replace it exactly.
        anchor=shown,
        question=f"What should {shown} be?",
        confidence=0.95,
    )


def _drafting_note_finding(note: str) -> Finding:
    shown = note if len(note) <= 70 else note[:66].rstrip() + " …]"
    return Finding(
        severity=Severity.BLOCKER,
        category="placeholder",
        title=f"Drafting note left in: {shown}",
        explanation=(
            "A note between drafters is still in the text. The other side will read "
            "it. Deal with the point it raises and take the note out."
        ),
        anchor=note,
        question="Has the point in this note been dealt with, so it can come out?",
        confidence=0.95,
    )


# --------------------------------------------------------------------------
# Defined terms
# --------------------------------------------------------------------------

# A capitalised phrase in quotes. Whether it is a definition depends on what is
# around it: see _definitions.
_DEFINITION = re.compile(r'[“"]([A-Z][A-Za-z0-9 \-\'’]{1,60})[”"]')
# What follows a term in a definitions clause. "or “Group Companies”" lets the
# first of two alternatives share the second's connector.
_MEANS_AFTER = re.compile(
    r"\s*(?:(?:or|and|/)\s*[“\"][^”\"\n]{1,60}[”\"]\s*)?,?\s*"
    r"(?:means|shall\s+mean|has\s+the\s+meanings?|shall\s+have\s+the\s+meanings?|"
    r"have\s+the\s+meanings?|includes?|shall\s+include|refers?\s+to|is\s+defined)\b",
    re.IGNORECASE,
)
# "each a “Consideration Share”", "hereinafter referred to as the “Lender”".
_LABEL_BEFORE = re.compile(
    r"(?:\b(?:each|together|collectively|jointly|individually|severally|hereinafter|"
    r"herein)\b,?\s+(?:(?:referred\s+to\s+)?as\s+)?(?:a|an|the)?\s*|"
    r"\b(?:referred\s+to\s+as|called|known\s+as|defined\s+as)\s+(?:the\s+|a\s+|an\s+)?)$",
    re.IGNORECASE,
)
# A definitions list or table: the term opens the paragraph or is the cell.
_TERM_OPENS_BLOCK = re.compile(r"^\s*(?:\(?[A-Za-z0-9]{1,4}[.)]\s*)?$")
_AFTER_OPENING_TERM = re.compile(r"^\s*(?:[:\-–—,]|$)")
# "“Completion” has the meaning given in clause 5" points at a definition
# rather than making one.
_POINTER = re.compile(r"\s*(?:shall\s+)?ha(?:s|ve)\s+the\s+meanings?\s+(?:given|set|assigned|"
                      r"ascribed|attributed|specified)", re.IGNORECASE)


def _definitions(text: str) -> list[tuple[str, int, int, str]]:
    """The terms this text defines, as (term, start, end, how).

    A quoted capitalised phrase is not a definition by itself. 'information
    marked “Confidential”' is a legend, and reading it as a definition reported
    "Confidential" as defined and never used. A definition has a connector
    after it (means, includes), sits in brackets (the “Seller”), follows a
    label (each a “Share”), or opens a definitions paragraph or table cell.
    """
    out: list[tuple[str, int, int, str]] = []
    for m in _DEFINITION.finditer(text):
        term = m.group(1).strip()
        if not term or not term[0].isupper():
            continue
        before, after = text[: m.start()], text[m.end():]
        if _POINTER.match(after):
            how = "pointer"
        elif _MEANS_AFTER.match(after):
            how = "means"
        elif before.rfind("(") > before.rfind(")"):
            how = "parenthetical"
        elif _LABEL_BEFORE.search(before[-60:]):
            how = "label"
        elif _TERM_OPENS_BLOCK.match(before) and _AFTER_OPENING_TERM.match(after):
            how = "list"
        else:
            continue
        out.append((term, m.start(), m.end(), how))
    return out


def _defined_terms(doc: ExtractedDoc) -> dict[str, list[int]]:
    """Every defined term, with the blocks that define it, in document order."""
    out: dict[str, list[int]] = {}
    for block in doc.blocks:
        for term, *_ in _definitions(block.text):
            out.setdefault(term, [])
            if block.index not in out[term]:
                out[term].append(block.index)
    return out


def _same_term(a: str, b: str) -> bool:
    """One term and its plural: "Consideration Share" and "Consideration Shares"."""
    short, long_ = sorted((a, b), key=len)
    return long_ in (short, short + "s", short + "es")


def _use_pattern(term: str, flags: int = 0) -> re.Pattern:
    """A use of the term, singular or plural, as a whole word.

    "each a “Consideration Share”" is used throughout as "Consideration
    Shares", and counting only the exact form reported it as never used.
    """
    stem = term[:-1] if term.endswith("s") and not term.endswith("ss") else term
    return re.compile(rf"\b{re.escape(stem)}(?:s|es|'s|’s)?\b", flags)


def _mask(text: str, patterns: list[re.Pattern]) -> str:
    """Blank out the longer terms that contain this one, keeping positions.

    "Consideration" is a word inside "Consideration Shares". Without masking,
    every use of the longer term counted as a use of the shorter one, so
    "Consideration" was reported as used in clause 1, two clauses before its
    definition, when clause 1 only ever says "Consideration Shares".
    """
    for pattern in patterns:
        text = pattern.sub(lambda m: "\x00" * len(m.group(0)), text)
    return text


@dataclass
class TermUse:
    term: str
    first_defined: int | None
    first_used: int | None
    uses: int
    uses_elsewhere: int = 0  # outside the paragraph that defines it
    first_use_text: str = ""
    # Same, ignoring case. A party short name defined as ("Northbridge") and
    # then appearing only in the ALL-CAPS signature block counted as never
    # used, which is two style findings on an NDA with nothing wrong with it.
    loose_uses_elsewhere: int = 0


_CLAUSE_NUMBER = re.compile(r"^\s*(\d+)(?:\.\d+)*[.)]?\s+\S")
_FORWARD_REF = re.compile(
    r"as defined (?:in|below|above)|has the meaning|defined in (?:Section|Clause)",
    re.IGNORECASE,
)


def _clause_index(doc: ExtractedDoc) -> dict[int, int]:
    """Block index -> the numbered clause it belongs to.

    Use-before-definition has to be judged in clauses, not paragraphs. Almost
    every agreement says "This Agreement" in its title and preamble before the
    definitions clause, and reporting that is wrong on every document ever
    drafted.
    """
    mapping: dict[int, int] = {}
    current = 0
    for block in doc.blocks:
        if block.kind == "heading":
            continue
        m = _CLAUSE_NUMBER.match(block.display)
        if m:
            current = int(m.group(1))
        mapping[block.index] = current
    return mapping


def _clause_headed_with_term(block_text: str, term: str) -> bool:
    """True when a clause's own heading is the term it defines."""
    return bool(
        re.match(rf"^\s*\d+(?:\.\d+)*[.)]?\s*{re.escape(term)}\b",
                 block_text, re.IGNORECASE)
    )


def defined_terms(doc: ExtractedDoc) -> list[Finding]:
    """Two failure modes: a term used before it is defined, and a term defined
    and then never used.

    A third, a capitalised phrase that looks defined but never was, is
    undefined_terms below, kept apart because it has to be far more cautious.
    """
    defined = _defined_terms(doc)
    terms: dict[str, TermUse] = {
        t: TermUse(t, blocks[0], None, 0) for t, blocks in defined.items()
    }
    clause_of = _clause_index(doc)

    # The longer terms that contain each term, other than its own plural.
    longer: dict[str, tuple[list[re.Pattern], list[re.Pattern]]] = {}
    for t in terms:
        inside = [
            other for other in terms
            if other != t and len(other) > len(t) and not _same_term(t, other)
            and re.search(rf"\b{re.escape(t)}", other)
        ]
        longer[t] = (
            [_use_pattern(o) for o in inside],
            [_use_pattern(o, re.IGNORECASE) for o in inside],
        )
    strict = {t: _use_pattern(t) for t in terms}
    loose = {t: _use_pattern(t, re.IGNORECASE) for t in terms}

    for block in doc.blocks:
        # A document's own title is not a substantive use of a term.
        is_heading = block.kind == "heading"
        for t, use in terms.items():
            exact_mask, loose_mask = longer[t]
            if block.index != use.first_defined:
                use.loose_uses_elsewhere += len(
                    loose[t].findall(_mask(block.text, loose_mask))
                )
            text = _mask(block.text, exact_mask)
            for m in strict[t].finditer(text):
                if block.index == use.first_defined and _in_quotes(block.text, m.start()):
                    continue
                use.uses += 1
                if block.index != use.first_defined:
                    use.uses_elsewhere += 1
                if use.first_used is None and not is_heading:
                    use.first_used = block.index
                    use.first_use_text = block.text

    out: list[Finding] = []
    for t, use in terms.items():
        if use.first_defined is not None and use.first_used is not None:
            used_in = clause_of.get(use.first_used, 0)
            defined_in = clause_of.get(use.first_defined, 0)
            # Judged in clauses, with a gap of at least two. Defining a term in
            # the very next clause is ordinary drafting, especially in leases,
            # and an explicit forward reference is deliberate rather than a slip.
            defining_text = next(
                (b.display for b in doc.blocks if b.index == use.first_defined), ""
            )
            if (
                used_in
                and defined_in
                and defined_in - used_in >= 2
                and not _FORWARD_REF.search(use.first_use_text)
                # A clause headed with the term itself ("3. Term. The “Term”
                # means...") is a deliberate structure, not a slip. Leases in
                # particular open with an operative clause that uses terms the
                # following clauses define, and a reader finds them immediately.
                and not _clause_headed_with_term(defining_text, t)
            ):
                word = _clause_word(doc)
                out.append(
                    Finding(
                        severity=Severity.STYLE,
                        category="defined-term",
                        title=(f'"{t}" is used in {word} {used_in} but defined in '
                               f"{word} {defined_in}"),
                        explanation=(
                            "A reader meets the term several clauses before learning "
                            "what it covers."
                        ),
                        anchor=t,
                        confidence=0.7,
                    )
                )
        # A term restated inside its own defining sentence is not "unused".
        # Only a term that never appears outside its definition is worth a flag,
        # and only in a document long enough that it genuinely should recur. On
        # a one-page letter a term used once is normal, and flagging it is the
        # kind of noise that gets the whole tool filtered into a folder.
        # The case-insensitive count is what keeps this quiet on a term that is
        # used, just not in the case it was defined in: an execution block in
        # capitals, a heading in title case, a schedule title in small caps.
        if (
            use.first_defined is not None
            and use.uses_elsewhere == 0
            and use.loose_uses_elsewhere == 0
            and len([b for b in doc.blocks if b.text.strip()]) >= 12
        ):
            out.append(
                Finding(
                    severity=Severity.STYLE,
                    category="defined-term",
                    title=f'"{t}" is defined but never used',
                    explanation=(
                        "A defined term that appears once is usually left over from a "
                        "prior draft, or a sign that a clause was deleted."
                    ),
                    anchor=t,
                    confidence=0.7,
                )
            )
    return out


def duplicate_definitions(doc: ExtractedDoc) -> list[Finding]:
    """A term defined twice, and worse, defined twice differently.

    '“Completion Date” means 10 April 2026' in clause 1 and '“Completion Date”
    means the fifth Business Day after...' in clause 6 is a contract that
    fixes one date and then another; which one governs is a dispute. The same
    definition repeated is only untidy.

    Judged within one part of the file. A form of escrow agreement attached as
    an exhibit defines its own terms, and that is not the main agreement
    defining them again. A pointer ('“Completion” has the meaning given in
    clause 5') is not a second definition.
    """
    seen: dict[tuple[int, str], tuple[int, str, str]] = {}
    reported: set[str] = set()
    out: list[Finding] = []
    part = 0
    for block in doc.blocks:
        if _ANNEX_TITLE.match(block.display):
            part += 1
        for term, start, end, how in _definitions(block.text):
            if how == "pointer":
                continue
            meaning = ""
            if how == "means":
                connector = _MEANS_AFTER.match(block.text, end)
                tail = block.text[connector.end():] if connector else ""
                meaning = re.sub(r"\W+", " ", tail[:80]).strip().lower()
            key = (part, term)
            if key not in seen:
                seen[key] = (block.index, how, meaning)
                continue
            first_block, first_how, first_meaning = seen[key]
            if first_block == block.index or term in reported:
                continue
            reported.add(term)
            differs = (how == "means" and first_how == "means"
                       and meaning and first_meaning and meaning != first_meaning)
            anchor = _anchor_right(block.text, start, end, 30)
            out.append(Finding(
                severity=Severity.SUBSTANTIVE if differs else Severity.STYLE,
                category="defined-term",
                title=(f'"{term}" is defined twice, differently' if differs
                       else f'"{term}" is defined twice'),
                explanation=(
                    "The two definitions say different things, so the document "
                    "contradicts itself on what the term means. Tell me which one is "
                    "right and I will take the other out."
                    if differs else
                    "The same term is defined in two places. One definition is enough; "
                    "a second invites the two to drift apart in a later draft."
                ),
                anchor=anchor,
                question=(f'Which definition of "{term}" is right?' if differs else None),
                confidence=0.8 if differs else 0.6,
            ))
    return out


# Words a capitalised phrase is made of when it is a name rather than a term:
# places, institutions, statutes, dates, titles, the parts of a document.
_PROPER_NOUN_WORDS = {
    "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "monday", "tuesday",
    "wednesday", "thursday", "friday", "saturday", "sunday",
    "state", "states", "city", "county", "court", "courts", "street", "road",
    "avenue", "lane", "square", "house", "place", "act", "code", "regulations",
    "regulation", "rules", "rule", "directive", "order", "kingdom", "republic",
    "union", "commission", "authority", "agency", "department", "ministry",
    "office", "government", "council", "exchange", "service", "revenue",
    "customs", "treasury", "parliament", "congress", "crown", "bank", "university",
    "limited", "ltd", "inc", "incorporated", "corporation", "llc", "llp", "plc",
    "holdings", "schedule", "section", "clause", "article", "exhibit", "annex",
    "appendix", "part", "paragraph", "chapter", "title", "mr", "mrs", "ms", "dr",
    "sir", "lord", "lady", "law", "laws", "island", "islands", "north", "south",
    "east", "west", "new", "united", "federal", "national", "royal", "high",
    "supreme", "district", "circuit", "tribunal", "board", "registrar", "england",
    "wales", "scotland", "ireland", "london", "york", "delaware", "america",
    "european", "english", "british", "american", "internal", "financial",
    "securities", "stock", "companies", "contracts", "rights", "gdpr", "hmrc",
}
_CAPITALISED_RUN = re.compile(r"\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,3}\b")
# The phrase is used as a term: "the Escrow Agent", "any Permitted Transferee".
# A name is not preceded by an article, and a sentence start is not by one.
_DETERMINER_BEFORE = re.compile(
    r"\b(?:the|a|an|any|each|every|such|all|no|that|this|its|their|our|your|"
    r"relevant|applicable|other)\s+$"
)
_SENTENCE_DETERMINERS = {"the", "a", "an", "any", "each", "every", "such", "no", "this",
                         "that", "all"}
_UNDEFINED_SHOWN = 5


def undefined_terms(doc: ExtractedDoc) -> list[Finding]:
    """A capitalised phrase used as a defined term that is never defined.

    Conservative on purpose. Legal documents are full of capitalised phrases
    that are correctly undefined: statutes, courts, places, job titles, party
    names, the counterparty's product. So only a phrase of two to four words,
    used at least twice after an article ("the Escrow Agent") and never at the
    start of a sentence only, with none of its words the kind names are made
    of, and not itself defined, a plural of a defined term, or made up
    entirely of defined terms. Single words are left to the model: "Claim"
    and "Board" are as often correct as not.
    """
    defined = set(_defined_terms(doc))
    defined_lower = {t.lower() for t in defined}
    single_words = {w.lower() for t in defined for w in t.split()}
    entity_stems = set()
    for block in doc.blocks:
        for m in _ENTITY_ANY_CASE.finditer(block.text):
            entity_stems.add(_entity_base(m.group(1)).lower())

    counts: Counter = Counter()
    first_anchor: dict[str, str] = {}
    for block in doc.blocks:
        if block.kind == "heading":
            continue
        text = block.text
        for m in _CAPITALISED_RUN.finditer(text):
            run = m.group(0)
            first, _, rest = run.partition(" ")
            if first.lower() in _SENTENCE_DETERMINERS and " " in rest:
                run = rest          # "The Escrow Agent shall": the article opens it
            elif first.lower() in _SENTENCE_DETERMINERS or not _DETERMINER_BEFORE.search(
                    text[max(0, m.start() - 20): m.start()]):
                continue
            if _in_quotes(text, m.start()):
                continue
            counts[run] += 1
            first_anchor.setdefault(run, run)

    out: list[Finding] = []
    for run, n in counts.items():
        if n < 2:
            continue
        low = run.lower()
        words = re.split(r"[ -]", low)
        if any(w in _PROPER_NOUN_WORDS for w in words):
            continue
        if any(_same_term(low, d) for d in defined_lower):
            continue
        if all(w in single_words for w in words):
            continue
        if any(low in stem for stem in entity_stems):
            continue
        out.append(Finding(
            severity=Severity.STYLE,
            category="defined-term",
            title=f'"{run}" is capitalised like a defined term but is never defined',
            explanation=(
                "The capitals tell the reader this is a defined term, and there is no "
                "definition to look up. Either add one or write it in lower case."
            ),
            anchor=run,
            confidence=0.5,
        ))
        if len(out) >= _UNDEFINED_SHOWN:
            break
    return out


def term_misspellings(doc: ExtractedDoc) -> list[Finding]:
    """A word that is one edit away from a defined term, and is not a real word.

    "Recipent" where "Recipient" is defined. Offered, never applied: "Patent"
    is one edit from "Parent" and a real word, and applying that correction
    automatically rewrote a patent representation into one about the parent
    company. The lawyer answers in one word and the fix is then applied.

    It is also the easiest check in this file to get badly wrong. "Disclosure"
    is two edits from "Discloser" and appears in every NDA ever written, so a
    naive edit-distance rule would flag a correct document as broken. Four
    guards, all required:

      - distance of exactly one, because a two-edit gap is usually a different
        word rather than a typo
      - not ordinary legal vocabulary or a common English word, per the lists
        below, and not a word the document also uses in lower case
      - appears at most twice, because a typo is rare and a word the drafter
        actually means recurs throughout the document
      - not the plural or possessive of the term

    When those disagree, the answer is to stay quiet. A missed typo costs
    nothing. A false blocker on a clean contract costs the habit.
    """
    defined = set(_defined_terms(doc))
    single = {t for t in defined if " " not in t and len(t) >= 5}
    if not single:
        return []

    counts = Counter()
    lower_words: set[str] = set()
    for block in doc.blocks:
        counts.update(re.findall(r"\b[A-Z][a-z]{4,}\b", block.text))
        lower_words.update(re.findall(r"\b[a-z]{5,}\b", block.text))

    out: list[Finding] = []
    for block in doc.blocks:
        for m in re.finditer(r"\b[A-Z][a-z]{4,}\b", block.text):
            word = m.group(0)
            low = word.lower()
            if word in defined or low in LEGAL_VOCABULARY or low in COMMON_WORDS:
                continue
            # The document writes it in lower case elsewhere: a real word.
            if low in lower_words:
                continue
            if counts[word] > 2:
                continue
            for term in single:
                if _edit_distance(low, term.lower(), cap=1) != 1:
                    continue
                if _is_inflection(word, term):
                    continue
                out.append(
                    Finding(
                        severity=Severity.SUBSTANTIVE,
                        category="defined-term",
                        title=f'"{word}" looks like a misspelling of "{term}"',
                        explanation=(
                            f'"{term}" is a defined term here and "{word}" is not '
                            "defined anywhere, so as drafted this may attach to nobody."
                        ),
                        anchor=word,
                        suggested_text=term,
                        auto_apply=False,
                        options=[term, word],
                        question=f'Should "{word}" be "{term}"?',
                        confidence=0.8,
                    )
                )
                break
    return _dedupe(out)


# Ordinary drafting vocabulary. A word here is never a typo, however close it
# sits to a defined term. "Disclosure" next to "Discloser" is the case that
# makes this list necessary.
LEGAL_VOCABULARY = {
    "disclosure", "disclosures", "disclosing", "disclosed", "discloser",
    "recipient", "recipients", "receiver", "receipt", "receiving",
    "agreement", "agreements", "agreed", "party", "parties", "person",
    "persons", "purchaser", "purchase", "seller", "sells", "vendor",
    "licensee", "licensor", "license", "licence", "lessee", "lessor", "lease",
    "employer", "employee", "employment", "consultant", "contractor",
    "company", "companies", "corporation", "holdings", "limited", "partner",
    "partners", "partnership", "guarantor", "guarantee", "guaranty",
    "indemnity", "indemnities", "indemnitee", "indemnitor", "indemnify",
    "affiliate", "affiliates", "subsidiary", "subsidiaries", "assignee",
    "assignor", "assignment", "successor", "successors", "transferee",
    "transferor", "transfer", "borrower", "lender", "trustee", "escrow",
    "confidential", "confidentiality", "information", "material", "materials",
    "service", "services", "product", "products", "premises", "property",
    "obligation", "obligations", "covenant", "covenants", "warranty",
    "warranties", "representation", "representations", "termination",
    "terminate", "notice", "notices", "effective", "closing", "clause",
    "section", "schedule", "schedules", "exhibit", "exhibits", "annex",
    "appendix", "договор",
}

# Real words that sit one edit from a common defined term. Each of these has
# been, or would be, "corrected" into the term by an edit-distance rule:
# Patent into Parent, Tender into Lender, Leader into Lender, Office into
# Officer, Contact into Contract. A spelling dictionary would be the general
# answer; this list covers the neighbours of the terms agreements actually
# define, and the lower-case test in term_misspellings covers the rest.
COMMON_WORDS = {
    "patent", "patents", "latent", "patient", "potent", "parent", "parents",
    "teller", "speller", "sealer", "smeller", "feller", "buyer", "bayer",
    "bender", "blender", "fender", "gender", "lander", "leader", "leaders",
    "mender", "render", "sender", "slender", "tender", "tenders", "lendee",
    "tenant", "tenants", "tenet", "license", "licensed", "licenser", "purchase",
    "truster", "trusted", "colder", "folder", "holden", "holler", "molder",
    "older", "solder", "noted", "voted", "votes", "nodes", "downer", "manage",
    "manger", "member", "ember", "contact", "contacts", "contactor", "assess",
    "employed", "office", "offices", "directory", "inventor", "inventors",
    "flounder", "founder", "bounder", "funder", "funders", "rounder", "sounder",
    "lessen", "lesser", "lesson", "issue", "issues", "arrange", "blank",
    "granter", "grandee", "assigned", "assigner", "disclose", "provide",
    "provided", "produce", "produced", "dealer", "healer", "dialer", "protect",
    "contrast", "prime", "prince", "prize", "pride", "merge", "meager",
    "mercer", "losing", "complexion", "busyness", "consent", "contest",
    "context", "convent", "found", "unite", "unity", "unfit", "cease", "leash",
    "least", "leave", "please", "tease", "ensure", "insure", "insured",
    "injured", "police", "polity", "count", "counts", "properly", "sharer",
    "shares", "assets", "goods", "notes", "units", "funds", "works", "target",
    "trustees", "courts", "deposit", "reposit", "vessel", "project", "prospect",
    "products", "producer", "provider", "clients", "client", "agent", "agents",
    "argent", "owner", "owners", "sponsor", "debtor", "debtors", "creditor",
    "editor", "editors", "holder", "holders", "hoarder", "loader", "border",
    "order", "orders", "lodger", "ledger", "letter", "better", "setter",
    "seller", "cellar", "sealed", "filler", "miller", "tiller", "killer",
    "biller", "waiver", "waiter", "water", "later", "latter", "matter",
    "hatter", "bidder", "binder", "finder", "minder", "winder", "grantor",
}


def _is_inflection(word: str, term: str) -> bool:
    """True when one word is just the plural or possessive of the other.

    "Tenants" is one edit from "Tenant" and is not a typo. Auto-applying that
    correction turned "other Tenants of the Building" into "other Tenant of the
    Building", which is ungrammatical and quietly re-points a reference to the
    other occupiers at the counterparty instead.
    """
    a, b = word.lower(), term.lower()
    short, long_ = sorted((a, b), key=len)
    return long_ in (short + "s", short + "es", short + "'s", short + "d", short + "n")


def _edit_distance(a: str, b: str, cap: int = 3) -> int:
    """Levenshtein distance, abandoned early once it exceeds `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,
                current[j - 1] + 1,
                previous[j - 1] + (ca != cb),
            ))
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


# The connectors that introduce a definition. A lower-case restatement on the
# far side of one of these is the definition explaining itself, not a mistake.
_MEANS_CONNECTOR = re.compile(
    r"\b(?:means|shall mean|meaning|refers? to|is defined as)\b", re.IGNORECASE
)
# A paragraph that continues the definition above it: "(a) has, or is
# reasonably likely to have...", "(ii) any ...".
_CONTINUATION = re.compile(r"^\s*\(?(?:[a-z]{1,3}|[ivx]{1,5}|\d{1,2})\)\s")


def _inside_a_definition(text: str, pos: int) -> bool:
    """True when `pos` sits in the part of a sentence that defines a term.

    '"Purchase Price" means the purchase price payable for the Shares' is how
    every definitions clause is written, and the lower-case restatement is the
    definition doing its job. Flagging it gave one "worth your judgment" item
    per definition on documents with no defects at all.
    """
    sentence = re.split(r"(?<=[.;:])\s", text[:pos])[-1]
    return bool(_MEANS_CONNECTOR.search(sentence))


def _definition_extent(doc: ExtractedDoc, defining: list[int]) -> set[int]:
    """The blocks a definition occupies: the one that defines the term, the
    table cell beside it that holds the meaning, and the (a), (b) paragraphs
    that continue it."""
    position = {b.index: i for i, b in enumerate(doc.blocks)}
    out: set[int] = set()
    for index in defining:
        out.add(index)
        i = position.get(index)
        if i is None:
            continue
        block = doc.blocks[i]
        following = doc.blocks[i + 1:]
        if block.kind == "table-cell" and following and following[0].kind == "table-cell":
            out.add(following[0].index)
            continue
        for nxt in following:
            if not _CONTINUATION.match(nxt.display):
                break
            out.add(nxt.index)
    return out


def term_capitalization(doc: ExtractedDoc) -> list[Finding]:
    """A defined term written in lower case means something different.

    Restricted hard. Single-word defined terms are almost always ordinary nouns
    ("Agreement", "Party", "Services"), and their lower-case form is correct
    English used constantly in the same document. Flagging those produced three
    false positives per contract. Only a distinctive multi-word term, where at
    least one word is not ordinary drafting vocabulary, is worth reporting.
    """
    defined = _defined_terms(doc)

    out: list[Finding] = []
    for term, defining_blocks in defined.items():
        words = term.split()
        if len(words) < 2:
            continue
        if all(w.lower() in LEGAL_VOCABULARY for w in words):
            continue
        lower = term.lower()
        # The definition restates the term in lower case as a matter of
        # course: in its own sentence, in the meaning cell of a definitions
        # table, and in the (a) and (b) that complete it.
        extent = _definition_extent(doc, defining_blocks)
        for block in doc.blocks:
            if block.index in extent:
                continue
            for m in re.finditer(rf"\b{re.escape(lower)}\b", block.text):
                if _inside_a_definition(block.text, m.start()):
                    continue
                out.append(
                    Finding(
                        severity=Severity.STYLE,
                        category="defined-term",
                        title=f'"{term}" appears in lower case as "{lower}"',
                        explanation=(
                            "A defined term in lower case is the undefined ordinary "
                            "phrase, which usually means something broader than intended."
                        ),
                        # Exactly the lower-case phrase. See the invariant note
                        # in date_consistency: a padded anchor would delete the
                        # words around it when applied.
                        anchor=block.text[m.start():m.end()],
                        suggested_text=term,
                        auto_apply=False,
                        confidence=0.6,
                    )
                )
    return _dedupe(out)[:20]


# --------------------------------------------------------------------------
# Cross-references
# --------------------------------------------------------------------------

# Group 3 is a hyphenated tail, "Section 1.1502-6", which is how a statute or
# regulation is numbered and never how this document numbers a clause. An
# English draft writes its references in lower case ("clause 12"), so the body
# words are matched either way; the schedule words only capitalised, as a
# lower-case "schedule" is usually the ordinary noun.
_SECTION_REF = re.compile(
    r"\b(Section|Clause|Article|section|clause|article|Schedule|Exhibit|Annex|Appendix)\s+"
    r"([0-9]+(?:\.[0-9]+)*|[A-Z])\b"
    r"((?:[-–][0-9A-Za-z]+)+)?"
)
# "Sections 7.2 and 7.3", "clauses 3.2 to 3.4": each number is a reference.
_PLURAL_REF = re.compile(
    r"\b(Sections|Clauses|Articles|sections|clauses|articles)\s+"
    r"([0-9]+(?:\.[0-9]+)*(?:\s*(?:,|and|or|to|through|-|–|&)\s*[0-9]+(?:\.[0-9]+)*)+)\b"
)
_SECTION_HEADING = re.compile(
    r"^\s*(?:(?:Section|Clause|Article|SECTION|CLAUSE|ARTICLE)\s+)?"
    r"([0-9]+(?:\.[0-9]+)*)[.)]?\s+\S"
)

# A schedule or exhibit announces itself by its title line, and that title
# carries no Word heading style in most drafts and none at all in a .txt. The
# numbered-clause regex above cannot see "EXHIBIT A", so every reference to a
# lettered exhibit read as a reference to something that was not in the file.
_ANNEX_TITLE = re.compile(
    r"^\s*(SCHEDULE|EXHIBIT|ANNEX|APPENDIX)\s+([0-9]+|[A-Z])\b",
    re.IGNORECASE,
)
# A schedule is often in parts, and each part numbers its paragraphs from 1.
_PART_TITLE = re.compile(r"^\s*PART\s+(?:[0-9]+|[A-Z]|[IVX]+)\b", re.IGNORECASE)

# Section 5 of the Companies Act 2006 is not a reference to this document's
# own clause 5, and flagging it is a false positive on most English documents.
# Nor is section 4.1 of the Disclosure Schedule, paragraph 3 of Schedule 4, or
# section 2 of the Escrow Agreement: a named document other than this one.
_EXTERNAL_INSTRUMENT = re.compile(
    r"^(?:\([0-9A-Za-z]{1,4}\))*\s*of\s+(?:the\s+)?(?:"
    r"[^.;,]{0,60}?\b(?:Act|Code|Regulations?|Rules?|Directive|Treaty|Constitution|"
    r"Convention|GDPR|Order|Ordinance|Statute|U\.S\.C|C\.F\.R)\b|"
    r"(?:Schedule|Part|Annex|Exhibit|Appendix)\b|"
    r"(?:[A-Z][\w'’&-]*\s+)+(?:Agreement|Deed|Plan|Instrument|Charter|Articles|Bylaws|"
    r"By-laws|Memorandum|Indenture|Lease|Policy|Letter|Schedule|Certificate|Note)\b)"
)

# Section, Clause and Article all number the body of the document; the rest
# each number a separate namespace, so "Schedule 9" is not satisfied by a
# clause 9.
_BODY_REF_WORDS = {"section", "clause", "article"}


def _clause_word(doc: ExtractedDoc) -> str:
    """What this document calls its numbered provisions: clause, section or
    article. "Section 5 appears 2 times" reads oddly in an English agreement
    that has only ever said "clause"."""
    counts: Counter = Counter()
    for block in doc.blocks:
        counts.update(w.lower() for w in re.findall(
            r"\b(clause|section|article)s?\s+\d", block.text, re.IGNORECASE))
    return counts.most_common(1)[0][0] if counts else "clause"


def _nearest_sections(target: str, present: set[str], limit: int = 3) -> list[str]:
    """The existing section numbers closest to a broken reference."""
    try:
        wanted = float(target)
    except ValueError:
        return sorted(present)[:limit]
    numeric = []
    for p in present:
        try:
            numeric.append((abs(float(p) - wanted), p))
        except ValueError:
            continue
    return [p for _, p in sorted(numeric)[:limit]]


def _structure(doc: ExtractedDoc) -> tuple[set[str], dict[str, set[str]]]:
    """The clause numbers and schedule titles the document actually has,
    counting the numbers Word draws as well as the ones typed."""
    present: set[str] = set()
    annexes: dict[str, set[str]] = {}
    for block in doc.blocks:
        text = block.display
        m = _SECTION_HEADING.match(text)
        if m:
            present.add(m.group(1))
        if block.kind == "heading":
            m2 = re.match(r"^\s*([0-9]+(?:\.[0-9]+)*|[A-Z])\b", text)
            if m2:
                present.add(m2.group(1))
        title = _ANNEX_TITLE.match(text)
        if title:
            annexes.setdefault(title.group(1).lower(), set()).add(title.group(2).upper())
    return present, annexes


def cross_references(doc: ExtractedDoc) -> list[Finding]:
    present, annexes = _structure(doc)
    if not present and not annexes:
        return []  # No numbered structure to check against; say nothing.
    # Clause references are judged only against clause numbers we can see.
    # A document numbered by Word through a list this reader could not follow,
    # or one whose only visible structure is an "EXHIBIT A" title, has clause
    # numbers we do not know, and judging "Section 2.2" against an empty set
    # reported every clause reference in a correct agreement as broken.
    body_known = bool(present) and not getattr(doc, "numbering_unresolved", False)

    out: list[Finding] = []
    for block in doc.blocks:
        text = block.text
        refs: list[tuple[str, str, re.Match, str]] = []
        for m in _SECTION_REF.finditer(text):
            if m.group(3):
                continue            # "Section 1.1502-6": a regulation, not a clause
            refs.append((m.group(1).lower(), m.group(2), m, m.group(0)))
        for m in _PLURAL_REF.finditer(text):
            singular = m.group(1)[:-1]
            for n in re.findall(r"[0-9]+(?:\.[0-9]+)*", m.group(2)):
                refs.append((singular.lower(), n, m, f"{singular} {n}"))
        for word, target, m, label in refs:
            if word in _BODY_REF_WORDS:
                if not body_known:
                    continue
                known = present
            else:
                # Only judge a schedule reference against schedule titles we can
                # actually see. A document whose schedules are bound separately
                # has none, and inventing "Schedule 2 does not exist" there is
                # exactly the false blocker that gets the tool switched off.
                known = annexes.get(word, set())
                if not known:
                    continue
            if target.upper() in {k.upper() for k in known}:
                continue
            if any(p.startswith(target + ".") for p in known):
                continue
            # "Section 5 of the Companies Act 2006" points outside this document.
            if _EXTERNAL_INSTRUMENT.match(text[m.end():]):
                continue
            nearby = _nearest_sections(target, known)
            out.append(
                Finding(
                    severity=Severity.SUBSTANTIVE,
                    category="cross-reference",
                    title=f"Reference to {label}, which does not exist",
                    explanation=(
                        "The document points at a section that is not in it. Either "
                        "the section was renumbered or it was deleted."
                        + (f" The closest that do exist are {', '.join(nearby)}."
                           if nearby else "")
                        + " Tell me which one you meant and I will correct it."
                    ),
                    anchor=m.group(0),
                    options=nearby,
                    question=f"{label} does not exist. Which did you mean?",
                    confidence=0.85,
                )
            )
    return _dedupe(out)


# A clause heading is a number, a full stop or bracket, and then the text. The
# punctuation is what separates it from a postal address ("1600 Pennsylvania
# Avenue") or a day-first date ("12 March 2026"), both of which used to enter
# the sequence and produce "numbering jumps from 88 to 1200" on an engagement
# letter written on firm letterhead.
_NUMBERED_CLAUSE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)*)[.)]\s+\S")

# Clause numbering does not run past this in practice. Anything higher is a
# year, a street number or a quantity that happened to start a line.
_MAX_CLAUSE_NUMBER = 99

# Enough of a gap to be actionable. The list used to be materialised in full,
# so one stray number generated kilobytes of explanation into an email.
_MISSING_SHOWN = 5


def numbering(doc: ExtractedDoc) -> list[Finding]:
    """Top-level numbering that skips or repeats, judged one part at a time.

    Schedules and exhibits restart at 1, and so does each Part of a schedule.
    Reading them as a continuation of the body reported three duplicated
    clauses on a lease that was numbered perfectly correctly, and "Section 1
    appears 2 times" on a schedule of limitations in two parts.
    """
    parts: list[list[tuple[int, str]]] = [[]]
    for block in doc.blocks:
        text = block.display
        if _ANNEX_TITLE.match(text) or _PART_TITLE.match(text):
            parts.append([])
            continue
        m = _NUMBERED_CLAUSE.match(text)
        if not m or "." in m.group(1):
            continue
        n = int(m.group(1))
        if n > _MAX_CLAUSE_NUMBER:
            continue
        # The anchor is the typed text: an automatic number is not in it.
        parts[-1].append((n, (block.text if block.number else text)[:60]))

    word = _clause_word(doc)
    out: list[Finding] = []
    for part in parts:
        out.extend(_numbering_within_a_part(part, word))
    return out


def _numbering_within_a_part(clauses: list[tuple[int, str]],
                             word: str = "clause") -> list[Finding]:
    anchors: dict[int, str] = {}
    for n, text in clauses:
        anchors.setdefault(n, text)

    out: list[Finding] = []
    for n, c in Counter(n for n, _ in clauses).items():
        if c > 1:
            out.append(
                Finding(
                    severity=Severity.FORMATTING,
                    category="numbering",
                    title=f"{word.capitalize()} {n} appears {c} times",
                    explanation="Duplicate numbering breaks every cross-reference to it.",
                    anchor=anchors[n],
                    confidence=0.9,
                )
            )
    for prev, nxt in pairwise(sorted(anchors)):
        if nxt - prev > 1:
            gap = list(range(prev + 1, nxt))
            listed = ", ".join(str(i) for i in gap[:_MISSING_SHOWN])
            if len(gap) > _MISSING_SHOWN:
                listed += f" and {len(gap) - _MISSING_SHOWN} more"
            out.append(
                Finding(
                    severity=Severity.FORMATTING,
                    category="numbering",
                    title=f"Numbering jumps from {prev} to {nxt}",
                    explanation=(
                        f"{word.capitalize()}{'s' if len(gap) != 1 else ''} {listed} "
                        f"{'are' if len(gap) != 1 else 'is'} missing. Either it was "
                        "deleted without renumbering, or one is genuinely absent."
                    ),
                    anchor=anchors[nxt],
                    confidence=0.9,
                )
            )
    return out


# --------------------------------------------------------------------------
# Amounts and dates
# --------------------------------------------------------------------------

_UNITS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16,
    "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fourty": 40, "fifty": 50,
    "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90,
}
_SCALES = {"hundred": 100, "thousand": 1_000, "million": 1_000_000,
           "billion": 1_000_000_000}

# Words that sit between the spelled-out number and its numeral: "one million
# dollars ($1,500,000)", "forty-five (45) days".
_UNIT_WORDS = {
    "dollars", "dollar", "pounds", "pound", "euros", "euro", "cents", "pence",
    "days", "day", "months", "month", "years", "year", "weeks", "week",
    "hours", "hour", "percent", "per", "cent", "business", "calendar",
    "shares", "units", "copies", "counterparts", "sterling", "usd", "gbp", "eur",
}

_NUMBER_TOKEN = re.compile(r"[A-Za-z]+")


def _parse_number_words(phrase: str) -> int | None:
    """Parse a spelled-out number. Returns None if the phrase is not one.

    Handles hyphenated compounds ("forty-five"), scales ("one million"),
    and the "and" of British usage ("one hundred and fifty").

    An earlier version matched a bare alternation of number words, which read
    "forty-five (45)" as "five (45)" and reported a correct document as
    self-contradictory. That is the kind of blocker-severity false positive
    that gets the whole tool switched off.
    """
    tokens = [t.lower() for t in _NUMBER_TOKEN.findall(phrase.replace("-", " "))]
    if not tokens:
        return None

    total, current, seen = 0, 0, False
    for token in tokens:
        if token == "and":
            continue
        if token in _UNITS:
            current += _UNITS[token]
            seen = True
        elif token in _TENS:
            current += _TENS[token]
            seen = True
        elif token in _SCALES:
            if not seen:
                return None
            scale = _SCALES[token]
            if scale == 100:
                current *= scale
            else:
                total += (current or 1) * scale
                current = 0
        else:
            return None
    return total + current if seen else None


# A parenthesised numeral, optionally with a currency symbol or percent sign.
_NUMERAL_IN_PARENS = re.compile(r"\(\s*[\$£€]?\s*([\d,]+)\s*%?\s*\)")

# The English way round: the figure, then the words. "£2,500,000 (two million
# five hundred thousand pounds)", "10 (ten) Business Days".
_FIGURE_THEN_WORDS = re.compile(
    r"(?<![\w.,/])((?:US\$|[£$€]|\b(?:USD|GBP|EUR)\s?)?\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?)"
    r"\s*\(\s*([A-Za-z][A-Za-z\s\-,]*?)\s*\)"
)
# Words that name the unit or the currency inside the brackets, not the number.
_AMOUNT_NOISE = _UNIT_WORDS | {"us", "united", "states", "only", "of", "america",
                               "british", "lawful", "money", "currency"}


def amounts(doc: ExtractedDoc) -> list[Finding]:
    """The classic: "thirty (13) days". Words and numerals disagreeing.

    Both halves are read properly rather than pattern-matched, so compound
    numbers and scales work in both directions, and in both orders: the
    American "thirty (30) days" and the English "£2,500,000 (two million five
    hundred thousand pounds)".
    """
    out: list[Finding] = []
    for block in doc.blocks:
        text = block.text
        for m in _NUMERAL_IN_PARENS.finditer(text):
            try:
                numeral = int(m.group(1).replace(",", ""))
            except ValueError:
                continue

            # Look back over the words preceding the parenthesis, dropping unit
            # words, and see whether what remains spells a number.
            tokens = list(_NUMBER_TOKEN.finditer(text[: m.start()]))[-6:]
            while tokens and tokens[-1].group(0).lower() in _UNIT_WORDS:
                tokens.pop()
            if not tokens:
                continue

            spelled, start = None, m.start()
            for take in range(1, len(tokens) + 1):
                candidate = " ".join(t.group(0) for t in tokens[-take:])
                value = _parse_number_words(candidate)
                if value is not None:
                    spelled, start = value, tokens[-take].start()
            if spelled is None or spelled == numeral:
                continue
            out.append(_amount_finding(text, start, m.end(), spelled, numeral))

        for m in _FIGURE_THEN_WORDS.finditer(text):
            if m.group(3) and int(m.group(3)) != 0:
                continue            # pence and cents are not spelled out alike
            words = [w for w in _NUMBER_TOKEN.findall(m.group(4).replace("-", " "))]
            if any(w.lower() in ("cents", "pence", "point") for w in words):
                continue
            while words and words[-1].lower() in _AMOUNT_NOISE:
                words.pop()
            while words and words[0].lower() in _AMOUNT_NOISE:
                words.pop(0)
            if not words:
                continue
            spelled = _parse_number_words(" ".join(words))
            if spelled is None:
                continue
            numeral = int(m.group(2).replace(",", ""))
            if spelled == numeral:
                continue
            out.append(_amount_finding(text, m.start(1) + (len(m.group(1)) - len(m.group(1).lstrip())),
                                       m.end(), spelled, numeral))
    return _dedupe(out)


def _amount_finding(text: str, start: int, end: int, spelled: int, numeral: int) -> Finding:
    shown = text[start:end].strip()
    return Finding(
        severity=Severity.BLOCKER,
        category="amount",
        title=f"'{shown}' disagrees with itself",
        explanation=(
            f"The words say {spelled:,} and the numerals say {numeral:,}. "
            "Which one governs is now a dispute. Tell me which is right "
            "and I will fix it throughout."
        ),
        anchor=_anchor(text, start, end, pad=12),
        options=[f"{spelled:,}", f"{numeral:,}"],
        question=f"Should this be {spelled:,} or {numeral:,}?",
        confidence=0.97,
    )


# One currency written more than one way: "US$50,000", "$ 20,000", "USD 10,000".
_CURRENCY_MARK = re.compile(r"(?<![A-Za-z$])(US\$|USD|\$|£|GBP|€|EUR)(\s?)(?=\d)")
_CURRENCY_OF = {"US$": "Dollar", "USD": "Dollar", "$": "Dollar", "£": "Sterling",
                "GBP": "Sterling", "€": "Euro", "EUR": "Euro"}


def currency_notation(doc: ExtractedDoc) -> list[Finding]:
    """The same currency written in several notations.

    Low stakes, and reported once per currency with an example of each form:
    a house-style slip that reads as several hands at work, not a defect in
    what anyone owes.
    """
    forms: dict[str, dict[str, str]] = {}
    for block in doc.blocks:
        for m in _CURRENCY_MARK.finditer(block.text):
            notation = m.group(1) + m.group(2)
            amount = re.match(r"[\d,.]+", block.text[m.end():])
            example = block.text[m.start(): m.end() + (amount.end() if amount else 0)]
            forms.setdefault(_CURRENCY_OF[m.group(1)], {}).setdefault(notation, example)

    out: list[Finding] = []
    for currency, notations in forms.items():
        if len(notations) < 2:
            continue
        examples = list(notations.values())
        listed = ", ".join(f'"{e}"' for e in examples)
        out.append(Finding(
            severity=Severity.FORMATTING,
            category="amount",
            title=f"{currency} amounts are written {len(examples)} ways: {listed}",
            explanation=(
                "One notation, used throughout, reads as one careful hand. Pick one "
                "and I will bring the others into line."
            ),
            anchor=examples[1],
            confidence=0.7,
        ))
    return out


_DATE_FORMATS = [
    (re.compile(r"\b[A-Z][a-z]+ \d{1,2}, \d{4}\b"), "January 1, 2026"),
    (re.compile(r"\b\d{1,2} [A-Z][a-z]+ \d{4}\b"), "1 January 2026"),
    (re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"), "01/01/2026"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "2026-01-01"),
]


_MONTHS = {m.lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], start=1)}
_MONTH_NAMES = {v: k.capitalize() for k, v in _MONTHS.items()}
_WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def _slash_order(doc: ExtractedDoc) -> str | None:
    """Whether "03/04/2026" means 3 April ("dmy") or March 4 ("mdy") here.

    Read from the document itself: a slashed date that can only be one way
    round, then the way its written-out dates are ordered. Assuming the
    American order turned an English completion date of 3 April into 4 March,
    and applied it as a tracked change.
    """
    votes: Counter = Counter()
    for block in doc.blocks:
        for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/\d{2,4}\b", block.text):
            a, b = int(m.group(1)), int(m.group(2))
            if a > 12 >= b:
                votes["dmy"] += 3
            elif b > 12 >= a:
                votes["mdy"] += 3
        votes["dmy"] += len(_DATE_FORMATS[1][0].findall(block.text))
        votes["mdy"] += len(_DATE_FORMATS[0][0].findall(block.text))
    if not votes or votes["dmy"] == votes["mdy"]:
        return None
    return votes.most_common(1)[0][0]


def _slash_is_ambiguous(text: str) -> bool:
    a, b, _ = text.split("/")
    return int(a) <= 12 and int(b) <= 12 and int(a) != int(b)


def _parse_date(text: str, style: str, order: str | None = "mdy") -> tuple[int, int, int] | None:
    """(year, month, day) from a date in a known style, or None."""
    try:
        if style == "January 1, 2026":
            month, rest = text.split(" ", 1)
            day, year = rest.split(",")
            return int(year), _MONTHS[month.lower()], int(day)
        if style == "1 January 2026":
            day, month, year = text.split()
            return int(year), _MONTHS[month.lower()], int(day)
        if style == "2026-01-01":
            y, m, d = text.split("-")
            return int(y), int(m), int(d)
        if style == "01/01/2026":
            a, b, c = text.split("/")
            year = int(c) + (2000 if len(c) == 2 else 0)
            if int(a) > 12:
                return year, int(b), int(a)
            if int(b) > 12:
                return year, int(a), int(b)
            if order is None and int(a) != int(b):
                return None
            return (year, int(b), int(a)) if order == "dmy" else (year, int(a), int(b))
    except (ValueError, KeyError):
        return None
    return None


def _render_date(parts: tuple[int, int, int], style: str, order: str | None = "mdy") -> str | None:
    year, month, day = parts
    name = _MONTH_NAMES.get(month)
    if not name:
        return None
    if style == "January 1, 2026":
        return f"{name} {day}, {year}"
    if style == "1 January 2026":
        return f"{day} {name} {year}"
    if style == "2026-01-01":
        return f"{year:04d}-{month:02d}-{day:02d}"
    if style == "01/01/2026":
        if order == "dmy":
            return f"{day:02d}/{month:02d}/{year}"
        return f"{month:02d}/{day:02d}/{year}"
    return None


def date_consistency(doc: ExtractedDoc) -> list[Finding]:
    """Mixed date formats in one document.

    A house-style problem that reads as carelessness to the recipient, and one
    of the few defects where the right answer is knowable: convert the minority
    formats to whichever the document uses most. That makes it an edit rather
    than a note, which is the difference between saving time and creating work.

    Except for a slashed date that could be read either way round. "03/04/2026"
    is converted by the order the document's other dates use, and offered as a
    question rather than applied: a wrong completion date written in as a
    tracked change is the worst edit this file could make.
    """
    occurrences: list[tuple[str, str, int, int, int]] = []   # style, text, block, start, end
    for block in doc.blocks:
        for pattern, label in _DATE_FORMATS:
            for m in pattern.finditer(block.text):
                occurrences.append((label, m.group(0), block.index, m.start(), m.end()))

    styles = Counter(style for style, *_ in occurrences)
    if len(styles) <= 1:
        return []

    dominant = styles.most_common(1)[0][0]
    order = _slash_order(doc)
    text_of = {b.index: b.text for b in doc.blocks}

    out: list[Finding] = []
    listed = ", ".join(f"{s} ({n})" for s, n in styles.most_common())
    for style, text, block_index, start, end in occurrences:
        if style == dominant:
            continue
        parts = _parse_date(text, style, order)
        converted = _render_date(parts, dominant, order) if parts else None
        ambiguous = style == "01/01/2026" and _slash_is_ambiguous(text)
        options: list[str] = []
        question = None
        if ambiguous and parts:
            year, month, day = parts
            other = _render_date((year, day, month), dominant, order)
            options = [o for o in (converted, other) if o]
            question = f"Is {text} {converted} or {other}?"
        out.append(
            Finding(
                severity=Severity.FORMATTING,
                category="date",
                title=(
                    f'"{text}" is not in the format the rest of the document uses'
                    if converted else
                    f"{len(styles)} different date formats in one document"
                ),
                explanation=(
                    f"The document mixes {listed}. The dominant style is "
                    f"{dominant}. This date could be read either way round, so tell "
                    "me which it is and I will write it out."
                    if ambiguous and converted else
                    f"The document mixes {listed}. The dominant style is "
                    f"{dominant}, so I have brought this one into line."
                    if converted else
                    f"The document mixes {listed}. Pick one and use it throughout."
                ),
                # Exactly the date, not a padded span: this anchor is what the
                # writer replaces, so any extra context would be deleted.
                anchor=text if converted else _anchor(text_of.get(block_index, ""), start, end),
                suggested_text=converted,
                auto_apply=bool(converted) and not ambiguous,
                options=options,
                question=question,
                confidence=0.85,
            )
        )
    return _dedupe(out)


_ORDINAL = r"(?:st|nd|rd|th)?"
_LOOSE_DATE = (
    rf"(?:\d{{1,2}}{_ORDINAL}\s+(?:day\s+of\s+|of\s+)?[A-Z][a-z]+,?\s+\d{{4}}|"
    rf"[A-Z][a-z]+\s+\d{{1,2}}{_ORDINAL},?\s+\d{{4}})"
)
_WEEKDAY = "|".join(_WEEKDAYS)
_WEEKDAY_THEN_DATE = re.compile(rf"\b({_WEEKDAY}),?\s+(?:the\s+)?({_LOOSE_DATE})")
_DATE_THEN_WEEKDAY = re.compile(
    rf"({_LOOSE_DATE})\s*,?\s*\(?\s*(?:being|which\s+is)\s+(?:a\s+)?({_WEEKDAY})\b"
)


def _parse_loose_date(text: str):
    """A datetime.date from "3 March 2026", "3rd of March, 2026" or "March 3, 2026"."""
    import datetime

    words = re.findall(r"[A-Za-z]+|\d+", text)
    day = month = year = None
    for w in words:
        if w.lower() in _MONTHS:
            month = _MONTHS[w.lower()]
        elif w.isdigit() and len(w) == 4:
            year = int(w)
        elif w.isdigit() and day is None:
            day = int(w)
    if not (day and month and year):
        return None
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def weekday_mismatch(doc: ExtractedDoc) -> list[Finding]:
    """"Monday, 3 March 2026", when 3 March 2026 is a Tuesday.

    Both halves were typed, so one of them is wrong, and a completion or
    notice date that disagrees with its own weekday is the kind of point the
    other side takes. Which half is right is a question, not an edit.
    """
    out: list[Finding] = []
    for block in doc.blocks:
        text = block.text
        found = [(m, m.group(1), m.group(2)) for m in _WEEKDAY_THEN_DATE.finditer(text)]
        found += [(m, m.group(2), m.group(1)) for m in _DATE_THEN_WEEKDAY.finditer(text)]
        for m, weekday, date_text in found:
            date = _parse_loose_date(date_text)
            if date is None:
                continue
            actual = _WEEKDAYS[date.weekday()]
            if actual == weekday:
                continue
            shown = m.group(0).strip()
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="date",
                title=f'"{shown}": {date_text.strip(" ,")} is a {actual}, not a {weekday}',
                explanation=(
                    "The day of the week and the date disagree, so one of them is "
                    "wrong. Tell me which, and I will correct the other."
                ),
                anchor=shown,
                question=(f"Is the date {date_text.strip(' ,')} right (a {actual}), or "
                          f"was a {weekday} meant?"),
                confidence=0.9,
            ))
    return _dedupe(out)


# A date tied to the opening date by the words themselves: "the date first
# written above, being March 5, 2026", "March 5, 2026 (the date of this
# Agreement)". Anything looser is not a claim about the opening date. "As of
# the date hereof and since December 31, 2024" is a representation about two
# moments, and reading the second as a restatement of the first told a lawyer
# a correct US merger agreement contradicted itself, at blocker severity.
_OPENING_DATE_WORDS = (
    r"(?:date\s+first\s+(?:written|stated|set\s+out)\s+above|"
    r"date\s+of\s+this\s+(?:agreement|deed|letter|instrument)|date\s+hereof)"
)
_ANY_DATE = (
    r"(?:[A-Z][a-z]+ \d{1,2}, \d{4}|\d{1,2} [A-Z][a-z]+ \d{4}|"
    r"\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2})"
)
_TIED_DATE = re.compile(
    rf"{_OPENING_DATE_WORDS}\s*(?:,\s*|\(\s*)(?:(?:being|namely|i\.e\.,?|that\s+is,?)\s+)?"
    rf"(?:the\s+)?({_ANY_DATE})|"
    rf"({_ANY_DATE})\s*\(\s*(?:being\s+)?the\s+{_OPENING_DATE_WORDS}\s*\)",
    re.IGNORECASE,
)


def _date_value(text: str, order: str | None):
    for pattern, style in _DATE_FORMATS:
        if pattern.fullmatch(text):
            return _parse_date(text, style, order)
    return None


def contradictory_dates(doc: ExtractedDoc) -> list[Finding]:
    """A clause that ties a date to 'the date first written above' while that
    date differs from the one the document opens with."""
    order = _slash_order(doc)
    opening: str | None = None
    out: list[Finding] = []
    for block in doc.blocks:
        tied = [(m.group(1) or m.group(2)) for m in _TIED_DATE.finditer(block.text)]
        if opening is not None:
            for date_text in tied:
                a, b = _date_value(date_text, order), _date_value(opening, order)
                same = (a == b) if (a and b) else date_text == opening
                if same:
                    continue
                out.append(
                    Finding(
                        severity=Severity.BLOCKER,
                        category="date",
                        title=f"'{date_text}' contradicts the date first written above",
                        explanation=(
                            f"This clause refers back to the opening date but names "
                            f"{date_text}, while the document opens with {opening}."
                        ),
                        anchor=date_text,
                        options=[date_text, opening],
                        question=f"Is the effective date {opening} or {date_text}?",
                        confidence=0.85,
                    )
                )
        if opening is None:
            for pattern, _ in _DATE_FORMATS:
                m = pattern.search(block.text)
                if m and (opening is None or block.text.find(m.group(0)) < block.text.find(opening)):
                    opening = m.group(0)
    return out


# --------------------------------------------------------------------------
# Party names
# --------------------------------------------------------------------------

# A company name is a run of capitalised words immediately before a corporate
# suffix. Allowing arbitrary characters in the name swallowed sentence
# fragments ("This deed is made with Northgate Freight Limited") and clause
# headings ("3. Warranties. Northgate Freight Ltd"), so the same company read
# as two different ones and the drift went unreported.
_ENTITY = re.compile(
    r"((?:[A-Z][\w&'.\-]*\s+){0,4}[A-Z][\w&'\-]*)[,\s]+"
    r"(LLC|L\.L\.C\.|Inc\.?|Incorporated|Ltd\.?|Limited|LLP|LP|L\.P\.|PLC|plc|"
    r"Corp\.?|Corporation|Co\.|GmbH|N\.V\.|S\.A\.|S\.p\.A\.|AG|AB|BV|B\.V\.)"
    r"(?=\W|$)"
)

# Words that are never part of a company name, even capitalised. These begin
# sentences and clause headings constantly.
_NOT_NAME_WORDS = {
    "the", "this", "that", "these", "those", "and", "or", "between", "with",
    "by", "for", "to", "of", "in", "on", "at", "from", "each", "any", "all",
    "it", "is", "are", "was", "were", "made", "entered", "dated", "deed",
    "agreement", "parties", "party", "warranties", "warranty", "indemnity",
    "indemnities", "definitions", "sale", "purchase", "consideration",
    "completion", "governing", "law", "notices", "term", "termination",
    "whereas", "now", "therefore", "recitals", "background", "schedule",
    "clause", "section", "article", "supplier", "customer", "company",
    "seller", "buyer", "purchaser", "vendor", "landlord", "tenant",
    "executive", "employee", "employer", "consultant", "contractor",
}


def _entity_base(raw: str) -> str:
    """The company name, with any sentence or clause prefix removed.

    The match for "3. Warranties. Northgate Freight Ltd" otherwise carries the
    clause heading into the name, so "Northgate Freight Limited" and
    "Northgate Freight Ltd" look like different companies and the drift goes
    unreported.
    """
    text = raw.strip().rstrip(",")
    if ". " in text:                      # drop everything up to the last sentence break
        text = text.rsplit(". ", 1)[-1]
    text = re.sub(r"^\s*\d+(?:\.\d+)*[.)]?\s*", "", text)
    # Trim leading words that begin sentences rather than company names.
    words = text.split()
    while words and words[0].lower().strip(".,") in _NOT_NAME_WORDS:
        words.pop(0)
    return " ".join(words).strip()


# Ltd, Ltd. and Limited are one company written three ways. Ltd and LLP are two
# different companies. Only the second is "do not send" material, and only the
# first is safe to correct without being asked.
_SUFFIX_FAMILY = {
    "llc": "llc", "l.l.c.": "llc",
    "inc": "inc", "inc.": "inc", "incorporated": "inc",
    "ltd": "ltd", "ltd.": "ltd", "limited": "ltd",
    "llp": "llp",
    "lp": "lp", "l.p.": "lp",
    "plc": "plc",
    "corp": "corp", "corp.": "corp", "corporation": "corp",
    "co.": "co",
    "gmbh": "gmbh",
    "n.v.": "nv", "s.a.": "sa", "s.p.a.": "spa",
    "ag": "ag", "ab": "ab", "bv": "bv", "b.v.": "bv",
}


def _suffix_family(suffix: str) -> str:
    return _SUFFIX_FAMILY.get(suffix.lower(), suffix.lower())


def party_names(doc: ExtractedDoc) -> list[Finding]:
    """The same entity written two ways. Often a copy-paste from the wrong deal."""
    forms: dict[str, set[str]] = {}
    suffix_of: dict[str, str] = {}
    counts: Counter = Counter()
    first_seen: dict[str, int] = {}
    # One form per spelling, whatever its case. "ACME MERGER SUB LLC" on the
    # signature page is the same name as "Acme Merger Sub LLC" in the parties
    # clause, set in capitals, and "correcting" it to title case rewrote every
    # signature block in a correct agreement.
    spelled: dict[str, str] = {}
    for block in doc.blocks:
        for m in _ENTITY.finditer(block.text):
            base = _entity_base(m.group(1)).lower()
            if len(base) < 3:
                continue
            full = f"{_entity_base(m.group(1))} {m.group(2)}"
            key = full.casefold()
            if key in spelled:
                known = spelled[key]
                if known.isupper() and not full.isupper():
                    # Prefer the form in ordinary case as the one to show.
                    for table in (counts, first_seen):
                        if known in table:
                            table[full] = table.pop(known)
                    suffix_of[full] = suffix_of.pop(known)
                    forms[base].discard(known)
                    spelled[key] = full
                full = spelled[key]
            else:
                spelled[key] = full
            forms.setdefault(base, set()).add(full)
            suffix_of.setdefault(full, m.group(2))
            counts[full] += 1
            first_seen.setdefault(full, block.index)

    out: list[Finding] = []
    for base, variants in forms.items():
        if len(variants) <= 1:
            continue
        listed = " / ".join(sorted(variants))
        # The form used most often, and earliest, is the one the drafter meant.
        # A party is introduced by its legal name in the parties clause and then
        # repeated; a stray variant later is the slip.
        canonical = max(variants, key=lambda v: (counts[v], -first_seen[v]))
        strays = sorted(v for v in variants if v != canonical)
        one_company = len({_suffix_family(suffix_of[v]) for v in variants}) == 1

        for stray in strays:
            out.append(
                Finding(
                    severity=Severity.STYLE if one_company else Severity.BLOCKER,
                    category="party-name",
                    title=f'"{stray}" should probably be "{canonical}"',
                    explanation=(
                        f"The document calls this party {listed}. That is the same "
                        f"company written two ways. {canonical} is used most, so I "
                        "have brought the others into line."
                        if one_company else
                        f"The document calls this party {listed}. Those are different "
                        "corporate entities, and naming the wrong one is a live risk. "
                        "Tell me which is right and I will change the others to match."
                    ),
                    anchor=stray,
                    suggested_text=canonical,
                    # A different corporate form may be a genuinely different
                    # company — a parent and its subsidiary often share a stem —
                    # so merging them on our own guess is the kind of misplaced
                    # edit in a client's contract this product cannot afford. The
                    # lawyer answers in one word and then we apply it.
                    auto_apply=one_company,
                    options=[] if one_company else sorted(variants),
                    question=(
                        None if one_company else
                        f"Which is the correct legal name for {base.title()}?"
                    ),
                    confidence=0.75,
                )
            )
    return out


# --------------------------------------------------------------------------
# Punctuation and spacing: the character-level slips of a document edited by
# several hands
# --------------------------------------------------------------------------
#
# Two kinds. A slip that changes characters ("the the", "days ,", "Buyer,and")
# is written as a tracked change: the fix is certain and the anchor is the
# slip itself. A slip that is only whitespace or quote style is counted and
# reported once, not written: the writer matches anchors with whitespace and
# quotes normalised, so the tracked change for a removed space would delete
# and re-insert the words around it, which looks like carelessness in a tool
# whose one job is to look careful.

# Two spaces after a full stop is a convention some firms keep, so only a run
# of spaces between words that no sentence boundary explains is counted.
# Exactly two: a run of three or more is alignment, not a slip.
_DOUBLE_SPACE = re.compile(r"(?<=[^\s.?!:])  (?=\S)")
# "days ," and "days ,and": the space is on the wrong side of the mark.
_SPACE_BEFORE_PUNCT = re.compile(r"(\w) ([,;:.])(?![.\d])(?=(\s|[A-Za-z]|$))")
_MISSING_SPACE_AFTER_COMMA = re.compile(r"\b([A-Za-z]{2,}),([A-Za-z]{2,})\b")
# Function words only. "that that" and "had had" are English.
_REPEATED_WORD = re.compile(
    r"\b(the|a|an|and|or|of|to|in|for|with|by|is|on|at|from|this|it|be|as)\s+\1\b",
    re.IGNORECASE,
)
_STRAIGHT_DOUBLE_QUOTE = re.compile(r'"[^"\n]{1,80}"')
_CURLY_DOUBLE_QUOTE = re.compile(r"“[^”\n]{1,80}”")


def punctuation(doc: ExtractedDoc) -> list[Finding]:
    out: list[Finding] = []
    double_spaces = 0
    first_double: tuple[str, int, int] | None = None
    straight: list[tuple[str, re.Match]] = []
    curly = 0

    for block in doc.blocks:
        text = block.text
        # A table cell or a line laid out with spaces is aligned, not slipped.
        aligned = block.kind == "table-cell" or len(re.findall(r"   +", text)) >= 2
        if not aligned:
            for m in _DOUBLE_SPACE.finditer(text):
                double_spaces += 1
                if first_double is None:
                    first_double = (text, m.start(), m.end())

        for m in _SPACE_BEFORE_PUNCT.finditer(text):
            span = _anchor(text, m.start(), m.end(), 12)
            slip = m.group(0)
            fixed = m.group(1) + m.group(2) + (" " if m.group(3).isalpha() else "")
            out.append(Finding(
                severity=Severity.FORMATTING, category="punctuation",
                title=f'Space before the {_PUNCT_NAME[m.group(2)]} in "{span}"',
                explanation="The space is on the wrong side of the punctuation. Moved.",
                anchor=span,
                suggested_text=span.replace(slip, fixed, 1),
                auto_apply=True, confidence=0.9,
            ))
        for m in _MISSING_SPACE_AFTER_COMMA.finditer(text):
            token = _token_around(text, m.start())
            if any(c in token for c in "@/.") or re.search(r"\d", token):
                continue                      # an address, a path, a number
            out.append(Finding(
                severity=Severity.FORMATTING, category="punctuation",
                title=f'No space after the comma in "{m.group(0)}"',
                explanation="A missing space after a comma. Added.",
                anchor=m.group(0),
                suggested_text=f"{m.group(1)}, {m.group(2)}",
                auto_apply=True, confidence=0.9,
            ))
        for m in _REPEATED_WORD.finditer(text):
            out.append(Finding(
                severity=Severity.FORMATTING, category="punctuation",
                title=f'"{m.group(0)}" repeats a word',
                explanation="The same word twice in a row. One removed.",
                anchor=m.group(0),
                suggested_text=m.group(1),
                auto_apply=True, confidence=0.9,
            ))
        straight += [(text, m) for m in _STRAIGHT_DOUBLE_QUOTE.finditer(text)]
        curly += len(_CURLY_DOUBLE_QUOTE.findall(text))

    if double_spaces and first_double is not None:
        text, start, end = first_double
        out.append(Finding(
            severity=Severity.FORMATTING, category="punctuation",
            title=(f"{double_spaces} double space{'s' if double_spaces != 1 else ''} "
                   "between words"),
            explanation=(
                "Two spaces where one belongs, not after a full stop. Word's "
                "find-and-replace clears these in one go; I have not touched them, "
                "because a tracked change that only removes a space reads as noise."
            ),
            anchor=_anchor(text, start, end), confidence=0.9,
        ))

    # Mixed quote styles: one of the two is the minority and the slip. Only
    # when both are genuinely present; a single stray pair in a document of
    # curly quotes is the usual copy-paste from an email.
    if straight and curly and len(straight) <= max(3, curly // 4):
        text, m = straight[0]
        n = len(straight)
        out.append(Finding(
            severity=Severity.FORMATTING, category="punctuation",
            title=(f"{n} pair{'s' if n != 1 else ''} of straight quotes in a document "
                   "that uses curly quotes"),
            explanation=(
                f"The document uses curly quotes ({curly} pairs) and {n} pair"
                f"{'s' if n != 1 else ''} came in straight, which is what pasting "
                "from an email does. Word converts them with Replace all."
            ),
            anchor=m.group(0), confidence=0.85,
        ))
    return _dedupe(out)


_PUNCT_NAME = {",": "comma", ";": "semicolon", ":": "colon", ".": "full stop"}


def _token_around(text: str, pos: int) -> str:
    """The whitespace-delimited token containing `pos`."""
    start = text.rfind(" ", 0, pos) + 1
    end = text.find(" ", pos)
    return text[start: end if end != -1 else len(text)]


# --------------------------------------------------------------------------
# Execution readiness: the signature page
# --------------------------------------------------------------------------
#
# The last thing checked before a document is signed, and the least checked,
# because by then everyone has read the body twenty times and nobody has read
# the signature page once. Three questions, all answerable from the text: does
# every party have a block, does anyone sign who is not a party, and is a
# block half filled in when the others are complete. Each is a copy-paste
# accident from the previous deal, and each is a question rather than an edit,
# because who signs is never ours to decide.

# The same entity pattern as party_names, plus each suffix in full capitals,
# because a signature page is set in capitals: "NORTHGATE FREIGHT LIMITED".
# Not case-insensitive: "a Delaware corporation" is a description, not a name.
_SUFFIXES = [
    r"LLC", r"L\.L\.C\.", r"Inc\.?", r"Incorporated", r"Ltd\.?", r"Limited", r"LLP",
    r"LP", r"L\.P\.", r"PLC", r"plc", r"Corp\.?", r"Corporation", r"Co\.", r"GmbH",
    r"N\.V\.", r"S\.A\.", r"S\.p\.A\.", r"AG", r"AB", r"BV", r"B\.V\.",
]
# A name may carry a lowercase connector: "The Royal Bank of Scotland plc",
# "Simmons and Simmons LLP". Without it the match began at "Scotland", the
# capitals on the signature page matched whole, and the two never agreed.
_NAME_CONNECTOR = r"(?:(?:of|and|the|de|del|della|di|du|des|van|von|der|&)\s+)?"
_ENTITY_ANY_CASE = re.compile(
    r"([A-Z][\w&'.\-]*(?:\s+" + _NAME_CONNECTOR + r"[A-Z][\w&'.\-]*){0,5})[,\s]+"
    r"(" + "|".join(dict.fromkeys(_SUFFIXES + [x.upper() for x in _SUFFIXES])) + r")"
    r"(?=\W|$)"
)

# A party is an entity given a role: `Acme Ltd (the "Buyer")`, `Acme Ltd, a
# Delaware corporation ("Acme")`. Without the parenthetical an entity in the
# opening blocks is letterhead, an addressee or a company being described,
# none of which sign. The window is wide because an English parties clause
# puts the registered office between the name and the role.
_ROLE_AFTER = re.compile(
    r"^[^\n]{0,400}?\(\s*(?:hereinafter\s+(?:referred\s+to\s+as\s+|called\s+)?)?"
    r"(?:the\s+|each\s+an?\s+|an?\s+|together\s+the\s+)?"
    r"[“\"](?P<role>[^”\"\n]{1,40})[”\"]\s*\)"
)
# Parties the clause names only by reference: "the Financial Institutions
# listed in Schedule 1 (the "Lenders")". Every one of them signs, none is in
# the text, so the list of parties is known to be incomplete.
_PARTIES_BY_REFERENCE = re.compile(
    r"\b(?:listed|set\s+out|named|specified|described|identified)\s+in\s+"
    r"(?:the\s+)?(?:Schedule|Exhibit|Annex|Appendix|Part|Schedules)\b",
    re.IGNORECASE,
)
# The English form puts the company under the signature: "for and on behalf of
# ACME LIMITED". That line belongs to the block above it, not to a new one.
_ON_BEHALF = re.compile(r"\bon\s+behalf\s+of\b", re.IGNORECASE)
# The parties clause ends where the recitals or the first clause begin. The
# target of a share sale is introduced in the recitals with a role of its own
# (the "Company") and does not sign, so the region has to stop before them.
_PARTIES_END = re.compile(r"^\s*(?:RECITALS|BACKGROUND|WHEREAS|INTRODUCTION)\b", re.IGNORECASE)
_PARTIES_MAX_BLOCKS = 40

_SIGNATURE_START = re.compile(
    r"^\s*(?:SIGNED|EXECUTED)\b.{0,80}?\b(?:by|on\s+behalf\s+of)\b", re.IGNORECASE
)
# A labelled line in a signature block. The colon, rule or end of line is what
# separates "Date:" from a recital beginning "Dated 3 March 2026".
_SIGNATURE_FIELD = re.compile(
    r"^\s*(By|Signed|Signature|Name|Title|Its|Position|Date|Dated|Director|Secretary|"
    r"Witness|Print(?:ed)?\s+name|Authori[sz]ed\s+signatory)\s*(?::|_{3,}|$)(.*)$",
    re.IGNORECASE,
)
_FIELD_KEY = {
    "by": "by", "signed": "by", "signature": "by",
    "name": "name", "print name": "name", "printed name": "name",
    "title": "title", "its": "title", "position": "title",
    "date": "date", "dated": "date",
    "director": "role", "secretary": "role", "witness": "role",
    "authorised signatory": "role", "authorized signatory": "role",
}
_RULE_ONLY = re.compile(r"^[\s_.\-:/]*$")
_SHORT_LINE = 80


@dataclass
class _SignatureBlock:
    entity: str | None                  # as written in the block, or None for an individual
    entity_key: tuple[str, str] | None  # (base, suffix family) for comparison
    fields: dict[str, str]              # normalised label -> value as written
    # The block indices it was read from, so a signature pack can lift the
    # block out of the document.
    positions: list[int] = _field(default_factory=list)


def _entity_key(name: str, suffix: str) -> tuple[str, str]:
    return _entity_base(name).casefold(), _suffix_family(suffix)


def _entity_verbatim(m: re.Match) -> str:
    """The entity exactly as the document writes it, prefix removed, so that a
    finding anchored on it can be placed. "Acme Holdings, Inc." keeps its comma."""
    whole = m.group(0)
    base = _entity_base(m.group(1))
    at = whole.find(base) if base else -1
    return whole[at:].strip() if at >= 0 else whole.strip()


def _first_entity(text: str) -> tuple[str, tuple[str, str]] | None:
    for m in _ENTITY_ANY_CASE.finditer(text):
        key = _entity_key(m.group(1), m.group(2))
        if len(key[0]) >= 3:
            return _entity_verbatim(m), key
    return None


def _field_value_present(value: str) -> bool:
    """Whether a signature field carries anything a signatory would recognise
    as filled in. Rules, dots and a bracketed placeholder do not count."""
    stripped = value.strip()
    if not stripped or _RULE_ONLY.match(stripped):
        return False
    return not (stripped.startswith("[") and stripped.endswith("]"))


def _signature_blocks(doc: ExtractedDoc) -> list[_SignatureBlock]:
    """The signature blocks in a document, in order.

    A block is a run of labelled lines (By, Name, Title, Date), the entity
    line above or inside them, and the "SIGNED for and on behalf of" line an
    English deed opens with. A run needs two distinct labels or an opening
    line to count, so a table whose header row says "Date" is not one.
    """
    blocks = doc.blocks
    runs: list[list[int]] = []
    current: list[int] = []
    gap = 0
    for i, block in enumerate(blocks):
        text = block.text
        is_start = bool(_SIGNATURE_START.match(text))
        is_field = bool(_SIGNATURE_FIELD.match(text))
        is_entity_line = len(text) <= _SHORT_LINE and _first_entity(text) is not None
        is_rule = bool(_RULE_ONLY.match(text))
        if is_start or is_field or is_entity_line or (is_rule and current):
            current.append(i)
            gap = 0
        elif current and gap == 0 and len(text) <= _SHORT_LINE:
            gap = 1                         # one short stray line is tolerated
        elif current:
            runs.append(current)
            current, gap = [], 0
    if current:
        runs.append(current)

    out: list[_SignatureBlock] = []
    for run in runs:
        parts: list[list[int]] = [[]]
        labels_seen: set[str] = set()
        last_by_was_a_chain = False
        for i in run:
            text = blocks[i].text
            starts_new = bool(_SIGNATURE_START.match(text))
            m = _SIGNATURE_FIELD.match(text)
            key = _FIELD_KEY.get(re.sub(r"\s+", " ", m.group(1).lower())) if m else None
            if key and key in labels_seen:
                # A second "By:" is a second signatory, unless the first was
                # "By: Acme GP, LLC, its general partner": the American form
                # for a partnership signs through a chain of By lines.
                starts_new = not (key == "by" and last_by_was_a_chain)
            if not m and not starts_new and len(text) <= _SHORT_LINE \
                    and _first_entity(text) and parts[-1] and labels_seen \
                    and not _ON_BEHALF.search(text):
                starts_new = True           # a new entity under a finished block
            if starts_new and parts[-1]:
                parts.append([])
                labels_seen = set()
            parts[-1].append(i)
            if key:
                labels_seen.add(key)
            if key == "by":
                last_by_was_a_chain = _first_entity(m.group(2)) is not None
        for part in parts:
            texts = [blocks[i].text for i in part]
            fields: dict[str, str] = {}
            for text in texts:
                m = _SIGNATURE_FIELD.match(text)
                if m:
                    key = _FIELD_KEY[re.sub(r"\s+", " ", m.group(1).lower())]
                    fields.setdefault(key, m.group(2))
            has_start = any(_SIGNATURE_START.match(t) for t in texts)
            if not has_start and len(fields) < 2:
                continue
            entity = next((e for t in texts if (e := _first_entity(t))), None)
            # Name and Title on their own are a table header or a schedule of
            # officers; "Director" on its own is a value in that table; and a
            # company above Name and Title is a notice clause ("If to Buyer:
            # Acme Inc. / Name: / Title:"). A signature block has somewhere to
            # sign: a By line, an opening line, or a rule under a company or a
            # capacity.
            has_rule = any(_RULE_ONLY.match(t) and t.strip() for t in texts)
            if not (has_start or "by" in fields or (has_rule and (entity or "role" in fields))):
                continue
            out.append(_SignatureBlock(
                entity=entity[0] if entity else None,
                entity_key=entity[1] if entity else None,
                fields=fields,
                positions=list(part),
            ))
    return out


def _parties(doc: ExtractedDoc) -> tuple[list[tuple[str, tuple[str, str]]], bool]:
    """The entities the parties clause gives a role to, as first written, and
    whether that is all of them. False when a party is a list somewhere else
    ("the Lenders listed in Schedule 1"), so that nobody on the signature
    pages can be called a stranger."""
    found: list[tuple[str, tuple[str, str]]] = []
    seen: set[tuple[str, str]] = set()
    complete = True
    for block in doc.blocks[:_PARTIES_MAX_BLOCKS]:
        if _NUMBERED_CLAUSE.match(block.text) or _PARTIES_END.match(block.text):
            break
        if _PARTIES_BY_REFERENCE.search(block.text):
            complete = False
        for m in _ENTITY_ANY_CASE.finditer(block.text):
            if not _ROLE_AFTER.match(block.text[m.end():]):
                continue
            key = _entity_key(m.group(1), m.group(2))
            if len(key[0]) < 3 or key in seen:
                continue
            seen.add(key)
            found.append((_entity_verbatim(m), key))
    return found, complete


def looks_like_execution_version(doc: ExtractedDoc) -> bool:
    """Whether this document is on its way to be signed rather than commented on.

    Decided from the document alone: an execution clause, two or more
    signature blocks, or a filename that says so. Checks that would be noise on
    a working draft (a schedule "to follow") are real omissions here.
    """
    if re.search(r"execut|signing|signature|for[ _-]sig", doc.filename, re.IGNORECASE):
        return True
    if any(_EXECUTION_CONTEXT.search(b.text) for b in doc.blocks):
        return True
    return len(_signature_blocks(doc)) >= 2


def signature_blocks(doc: ExtractedDoc) -> list[Finding]:
    blocks = _signature_blocks(doc)
    if not blocks:
        return []
    parties, parties_complete = _parties(doc)
    out: list[Finding] = []

    signing = {b.entity_key for b in blocks if b.entity_key}
    party_keys = {key for _, key in parties}
    # A block that signs for a party under another name is reported once, by
    # party_drift, as the blocker it is, not as a stranger signing and a
    # party with no block.
    drifted = _signature_drift(doc, blocks)
    signing |= {party.key for _, party in drifted}
    strangers = {id(block) for block, _ in drifted}
    # Only judged when both sides are known: a document signed by individuals
    # has no entities on its signature page to compare, and a letter with no
    # parties clause has nothing to compare them to.
    if signing and party_keys:
        for name, key in parties:
            if key in signing:
                continue
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="signature",
                title=f"No signature block for {name}",
                explanation=(
                    f"{name} is a party to this document and there is no block for "
                    "it to sign. Every other party has one. If it signs, the block "
                    "is missing; if it does not, it should not be named as a party."
                ),
                anchor=name,
                question=f"Does {name} sign this document?",
                confidence=0.7,
            ))
        for block in blocks:
            if not parties_complete:
                break               # some parties are only in a schedule; anyone may sign
            if block.entity_key and block.entity_key not in party_keys \
                    and id(block) not in strangers:
                out.append(Finding(
                    severity=Severity.SUBSTANTIVE,
                    category="signature",
                    title=f"{block.entity} signs, but is not a party",
                    explanation=(
                        f"The signature page has a block for {block.entity}, which "
                        "the parties clause does not name. Signature pages travel "
                        "between deals; this is usually the previous one's."
                    ),
                    anchor=block.entity,
                    question=f"Should {block.entity} be signing this?",
                    confidence=0.7,
                ))

    # A block left blank where the others are filled in. All blank is a
    # convention (the name is written by hand); one blank is an omission.
    # Only blocks for a company. A witness block or an individual's block
    # has a blank Name by design, and "Name:" is not an anchor that can be
    # placed anyway.
    named = [b for b in blocks if b.entity]
    for field, label in (("name", "name"), ("title", "title")):
        filled = [b for b in named if field in b.fields and _field_value_present(b.fields[field])]
        blank = [b for b in named if field in b.fields and not _field_value_present(b.fields[field])]
        if not filled or not blank:
            continue
        for block in blank:
            who = block.entity
            out.append(Finding(
                severity=Severity.SUBSTANTIVE,
                category="signature",
                title=f"Signature block for {who} has no {label}",
                explanation=(
                    f"The {label} is filled in for the other signatories and blank "
                    f"here. Tell me who signs for {who} and I will complete it."
                ),
                anchor=block.entity,
                question=f"Who signs for {who}, and in what capacity?",
                confidence=0.75,
            ))
    return _dedupe(out)


# --------------------------------------------------------------------------
# Party-name drift: a party renamed in one place and not the others
# --------------------------------------------------------------------------
#
# The parties clause says the Buyer is Falcon Topco Limited; the Buyer's
# signature block, its address for notices or a schedule still says Falcon
# Holdings Limited. party_names cannot see it (the two share no stem), and
# signature_blocks saw half of it as a stranger signing. Tied together by the
# role the parties clause gives each party, it is the blocker it is: the
# wrong company would sign, or be served.


@dataclass
class _Party:
    name: str                   # as the parties clause writes it
    key: tuple[str, str]
    role: str                   # "Buyer", as defined


_NOTICES_HEADING = re.compile(r"^\s*(?:(?:clause|section)\s+)?(?:\d+(?:\.\d+)*[.)]?\s+)?"
                              r"notices?\b", re.IGNORECASE)


def _party_roles(doc: ExtractedDoc) -> list[_Party]:
    """Each party the parties clause names, with the role it defines for it."""
    out: list[_Party] = []
    seen: set[tuple[str, str]] = set()
    for block in doc.blocks[:_PARTIES_MAX_BLOCKS]:
        if _NUMBERED_CLAUSE.match(block.text) or _PARTIES_END.match(block.text):
            break
        for m in _ENTITY_ANY_CASE.finditer(block.text):
            role = _ROLE_AFTER.match(block.text[m.end():])
            if not role:
                continue
            key = _entity_key(m.group(1), m.group(2))
            if len(key[0]) < 3 or key in seen:
                continue
            seen.add(key)
            out.append(_Party(_entity_verbatim(m), key, role.group("role").strip()))
    # A role two parties share ("each a “Seller”") names neither of them.
    shared = Counter(p.role.casefold() for p in out)
    return [p for p in out if shared[p.role.casefold()] == 1]


def _role_in(text: str, party: _Party) -> bool:
    return re.search(rf"\b{re.escape(party.role)}\b", text, re.IGNORECASE) is not None


def _signature_drift(doc: ExtractedDoc, blocks=None) -> list[tuple[_SignatureBlock, _Party]]:
    """Signature blocks that sign for a party under a name the parties clause
    does not give it, each with the party it signs for.

    A block is tied to a party by the party's role written in the block ("for
    and on behalf of the Buyer"), or, when that is missing, because it is the
    one block signed by a stranger and the party is the one party without a
    block."""
    parties = _party_roles(doc)
    if not parties:
        return []
    blocks = _signature_blocks(doc) if blocks is None else blocks
    keys = {p.key for p in parties}
    signing = {b.entity_key for b in blocks if b.entity_key}
    strangers = [b for b in blocks if b.entity_key and b.entity_key not in keys]
    out: list[tuple[_SignatureBlock, _Party]] = []
    for block in strangers:
        texts = " ".join(doc.blocks[i].text for i in block.positions)
        named = [p for p in parties if _role_in(texts, p)]
        if len(named) == 1 and named[0].key not in signing \
                and named[0].key[0] != block.entity_key[0]:
            out.append((block, named[0]))
    unsigned = [p for p in parties if p.key not in signing
                and not any(p is q for _, q in out)]
    left = [b for b in strangers if not any(b is c for c, _ in out)]
    # Untied by role, a stranger is the party's block only when the names
    # share their first word: Falcon Holdings for Falcon Topco is one group's
    # company renamed; Bluewater Shipping for Northgate Freight is a page from
    # another deal, which signature_blocks reports as that.
    if len(unsigned) == 1 and len(left) == 1 and len(parties) >= 2 \
            and unsigned[0].key[0] != left[0].entity_key[0] \
            and unsigned[0].key[0].split()[:1] == left[0].entity_key[0].split()[:1]:
        out.append((left[0], unsigned[0]))
    return out


def _labelled_mentions(doc: ExtractedDoc, parties: list[_Party]) -> list[tuple[int, str, _Party]]:
    """(block, entity as written, party) for each place outside the parties
    clause that names a company as one of the parties by its role: "Buyer:
    Falcon Holdings Limited" in the notices clause or a schedule, or "Falcon
    Holdings Limited (the “Buyer”)" restated anywhere."""
    out = []
    in_notices = in_schedule = False
    notices_clause = ""
    labels = _locations(doc)
    started = False
    for block in doc.blocks:
        if not started:
            started = bool(_NUMBERED_CLAUSE.match(block.text) or _PARTIES_END.match(block.text)
                           or block.number)
            if not started:
                continue
        text = block.text
        label = labels.get(block.index, "")
        if _ANNEX_TITLE.match(block.display):
            in_schedule, in_notices = True, False
        elif not in_schedule and _NOTICES_HEADING.match(block.display) and len(text) < 120:
            in_notices, notices_clause = True, label.split(".")[0]
        elif in_notices and label.split(".")[0] != notices_clause:
            in_notices = False
        for party in parties:
            role = re.escape(party.role)
            if in_notices or in_schedule:
                for m in re.finditer(rf"(?:^|\n|\b(?:to|for)\s+)(?:the\s+)?{role}\s*[:\-–]\s*",
                                     text, re.IGNORECASE):
                    found = _first_entity(text[m.end():m.end() + 120])
                    if found:
                        out.append((block.index, found[0], party))
            for m in _ENTITY_ANY_CASE.finditer(text):
                if re.match(rf"\s*,?\s*\((?:the\s+|as\s+(?:the\s+)?)?[“\"]?{role}[”\"]?\s*\)",
                            text[m.end():]):
                    out.append((block.index, _entity_verbatim(m), party))
    return out


def party_drift(doc: ExtractedDoc) -> list[Finding]:
    """A party named one way in the parties clause and another in its own
    signature block, notice details or a schedule."""
    parties = _party_roles(doc)
    if not parties:
        return []
    keys = {p.key for p in parties}
    labels = _locations(doc)
    out: list[Finding] = []
    for block, party in _signature_drift(doc):
        out.append(_drift_finding(f"the {party.role}'s signature block", block.entity, party,
                                  label="signature page"))
    for index, written, party in _labelled_mentions(doc, parties):
        found = _first_entity(written)
        if not found:
            continue
        key = found[1]
        if key in keys or key[0] == party.key[0]:
            continue            # another party, or the same name (party_names)
        out.append(_drift_finding(labels.get(index, "") or "a later clause", written, party,
                                  label=labels.get(index, "")))
    return _dedupe(out)


def _display_name(name: str) -> str:
    """A name set in capitals on the signature page, as it reads in a
    sentence: FALCON TOPCO LIMITED is Falcon Topco Limited, and LLP stays."""
    if not name.isupper():
        return name
    return " ".join(w if w.rstrip(".,") in _KEEP_CAPS else w.capitalize() for w in name.split())


_KEEP_CAPS = {"LLP", "LLC", "LP", "PLC", "AG", "AB", "BV", "NV", "SA", "UK", "US", "L.L.C",
              "L.P", "N.V", "S.A", "B.V"}


def _drift_finding(where: str, written: str, party: _Party, since: str = "",
                   label: str = "") -> Finding:
    place = where[:1].upper() + where[1:]
    shown, name = _display_name(written), _display_name(party.name)
    # The right name set the way this place sets names: capitals on a
    # signature page, ordinary case in a sentence.
    matched = party.name.upper() if written.isupper() else name
    return Finding(
        severity=Severity.BLOCKER,
        category="party-name",
        title=(f"{place} still names {shown} as the {party.role}; the parties clause "
               f"now says {name}" if since else
               f"{place} names {shown} as the {party.role}, but the parties clause "
               f"says {name}"),
        explanation=(
            (f"The parties clause was changed to {name} {since}, and {where} "
             "was not. " if since else
             f"The parties clause makes {name} the {party.role}; {where} names "
             f"{shown}. ")
            + "Those are different companies: the wrong one would sign or be "
            "served. Tell me which is right and I will make the other match."
        ),
        anchor=written,
        suggested_text=matched,
        options=[matched, written],
        question=f"Is the {party.role} {name} or {shown}?",
        confidence=0.85,
        where=label,
    )


def party_drift_since(earlier: ExtractedDoc, doc: ExtractedDoc, since: str = "") -> list[Finding]:
    """A party renamed in the parties clause since the earlier version, and
    still called by its old name somewhere in this one. `since` finishes the
    sentence "The parties clause was changed to X ...": "since the version I
    reviewed on 3 March 2026"."""
    before = {p.role.casefold(): p for p in _party_roles(earlier)}
    now = _party_roles(doc)
    now_keys = {p.key for p in now}
    labels = _locations(doc)
    signature_at = {i for b in _signature_blocks(doc) for i in b.positions}
    start = next((b.index for b in doc.blocks
                  if _NUMBERED_CLAUSE.match(b.text) or _PARTIES_END.match(b.text) or b.number),
                 None)
    out: list[Finding] = []
    for party in now:
        old = before.get(party.role.casefold())
        if old is None or old.key == party.key or old.key in now_keys \
                or old.key[0] == party.key[0]:
            continue
        for block in doc.blocks:
            if start is None or block.index < start:
                continue
            for m in _ENTITY_ANY_CASE.finditer(block.text):
                if _entity_key(m.group(1), m.group(2)) != old.key:
                    continue
                where = (f"the {party.role}'s signature block" if block.index in signature_at
                         else labels.get(block.index, "") or "a later clause")
                out.append(_drift_finding(where, _entity_verbatim(m), party,
                                          since or "since the earlier version",
                                          label=("signature page" if block.index in signature_at
                                                 else labels.get(block.index, ""))))
    return _dedupe(out)


# --------------------------------------------------------------------------
# Metadata and leftovers: what must never reach a client
# --------------------------------------------------------------------------


# Word keeps the body in word/document.xml and puts everything else in a part
# of its own. Headers, footers, footnotes and endnotes carry tracked changes
# and hidden text like any other text, and a watermark lives in the header by
# construction, so reading only the body told a lawyer the file was clean while
# an associate's header edit and a DRAFT watermark travelled out with it.
_TEXT_PART = re.compile(r"^word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


def _part_label(name: str) -> str:
    """Where in the document a leftover is, in words a lawyer would use."""
    stem = name.removeprefix("word/").removesuffix(".xml")
    if stem.startswith("document"):
        return "the body"
    if stem.startswith("header"):
        return "a header"
    if stem.startswith("footer"):
        return "a footer"
    if stem.startswith("footnotes"):
        return "the footnotes"
    return "the endnotes"


# Every kind of tracked change, not only insertions and deletions. A moved
# paragraph and a formatting change are revisions too, and they show in the
# recipient's Review pane with the author's name on them.
_REVISION_MARK = re.compile(
    r"<w:(?:ins|del|moveFrom|moveTo|rPrChange|pPrChange|sectPrChange|tblPrChange|"
    r"trPrChange|tcPrChange|numberingChange)\b"
)


def _has_hidden_text(xml: str) -> bool:
    """Whether a run of text is formatted hidden.

    Only a run with text in it. A hidden paragraph mark is how Word makes a
    style separator, the device that puts a run-in heading and its paragraph
    on one line with different styles, and it hides nothing a reader could
    want to see. Reporting it told the lawyer a clean document carried
    hidden text.
    """
    if "w:vanish" not in xml:
        return False
    try:
        from lxml import etree

        root = etree.fromstring(xml.encode("utf-8"))
    except Exception:  # noqa: BLE001 - unparseable: fall back to the plain test
        return "<w:vanish/>" in xml or 'w:vanish w:val="true"' in xml
    w = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    for vanish in root.iter(w + "vanish"):
        if vanish.get(w + "val") in ("false", "0", "off"):
            continue
        rpr = vanish.getparent()
        run = rpr.getparent() if rpr is not None else None
        if run is None or run.tag != w + "r":
            continue            # a paragraph mark or a style, not a run of text
        if any((t.text or "").strip() for t in run.iter(w + "t")):
            return True
    return False


def leftovers(content: bytes) -> list[Finding]:
    """Inspect the .docx package for things that should not go out.

    This is the check with the worst consequences when it is skipped, and it is
    entirely mechanical. Nothing here requires judgment.
    """
    out: list[Finding] = []
    try:
        z = zipfile.ZipFile(BytesIO(content))
        names = set(z.namelist())
        parts = {
            name: z.read(name).decode("utf-8", errors="replace")
            for name in sorted(names)
            if _TEXT_PART.match(name)
        }
    except Exception:  # noqa: BLE001 - an unreadable package is reported elsewhere
        return out
    if "word/document.xml" not in parts:
        return out

    revised = {n: x for n, x in parts.items() if _REVISION_MARK.search(x)}
    if revised:
        authors = sorted({
            a for xml in revised.values()
            for a in re.findall(r'w:author="([^"]+)"', xml)
        })
        # Naming the part matters: a lawyer who has already accepted every change
        # in the body will not think to look in the header.
        where = sorted({_part_label(n) for n in revised})
        out.append(
            Finding(
                severity=Severity.BLOCKER,
                category="leftovers",
                title="The document still contains tracked changes",
                explanation=(
                    "Tracked changes from "
                    + (", ".join(authors) if authors else "a previous draft")
                    + f" are still in {_join(where)}. "
                    "The recipient will see the edit history."
                ),
                anchor="",
                confidence=1.0,
            )
        )

    if "word/comments.xml" in names:
        try:
            comments = z.read("word/comments.xml").decode("utf-8", errors="replace")
            authors = sorted(set(re.findall(r'w:author="([^"]+)"', comments)))
            count = comments.count("<w:comment ")
        except Exception:  # noqa: BLE001
            authors, count = [], 0
        if count:
            out.append(
                Finding(
                    severity=Severity.BLOCKER,
                    category="leftovers",
                    title=f"{count} internal comment(s) are still in the document",
                    explanation=(
                        "Comments from "
                        + (", ".join(authors) if authors else "internal reviewers")
                        + " will be visible to whoever opens this."
                    ),
                    anchor="",
                    confidence=1.0,
                )
            )

    hidden = sorted({_part_label(n) for n, xml in parts.items() if _has_hidden_text(xml)})
    if hidden:
        out.append(
            Finding(
                severity=Severity.SUBSTANTIVE,
                category="leftovers",
                title="The document contains hidden text",
                explanation=(
                    f"Hidden text in {_join(hidden)} does not print but travels with "
                    "the file and is one toggle away from being visible."
                ),
                anchor="",
                confidence=0.9,
            )
        )

    # Both halves have to be in the same part, or a logo in the header pairs
    # with the word DRAFT in the body and invents a watermark that is not there.
    if any(
        re.search(r"DRAFT", xml)
        and re.search(r"<w:pict|watermark|WordArt", xml, re.IGNORECASE)
        for xml in parts.values()
    ):
        out.append(
            Finding(
                # An observation, not a defect: most documents that pass through
                # here are drafts, and a DRAFT watermark on a draft is correct.
                # Reported at blocker or substantive severity it would fire on a
                # large share of correct documents, which is the cost this
                # product cannot pay.
                severity=Severity.STYLE,
                category="leftovers",
                title="There appears to be a DRAFT watermark",
                explanation="If this is an execution copy, the watermark should come off.",
                anchor="",
                confidence=0.6,
            )
        )

    # Authorship metadata: who wrote it, how long it took, what it was called.
    if "docProps/core.xml" in names:
        core = z.read("docProps/core.xml").decode("utf-8", errors="replace")
        people = set(re.findall(r"<(?:dc:creator|cp:lastModifiedBy)>([^<]+)<", core))
        people = {p for p in people if p.strip()}
        # The organisation Office was registered to, and a manager, are in the
        # extended properties and travel the same way.
        if "docProps/app.xml" in names:
            app = z.read("docProps/app.xml").decode("utf-8", errors="replace")
            people |= {f"{v.strip()} (as {k.lower()})"
                       for k, v in re.findall(r"<(Company|Manager)>([^<]+)</\1>", app)
                       if v.strip()}
        if people:
            out.append(
                Finding(
                    severity=Severity.STYLE,
                    category="leftovers",
                    title="Author metadata identifies " + ", ".join(sorted(people)),
                    explanation=(
                        "Document properties travel with the file and name who worked "
                        "on it. Worth scrubbing before this goes outside the firm."
                    ),
                    anchor="",
                    confidence=1.0,
                )
            )

    return out


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------


# How many of one kind of finding an email can carry before it stops being
# readable. A firm template or a schedule-heavy agreement can produce dozens of
# broken cross-references, and reply.py renders every blocker and every
# substantive finding in full: a lawyer scanning a preview pane on a phone
# cannot get a verdict out of a hundred-item list, which is the whole point of
# the product.
_PER_CATEGORY_CAP = 10

_SEVERITY_ORDER = {
    Severity.BLOCKER: 0,
    Severity.SUBSTANTIVE: 1,
    Severity.STYLE: 2,
    Severity.FORMATTING: 3,
}


# What a lawyer calls each kind of finding. The internal category name
# ("defined-term findings") is ours, not theirs.
_CATEGORY_WORDS = {
    "placeholder": "unfilled blanks of the same kind",
    "defined-term": "defined-term points of the same kind",
    "cross-reference": "broken cross-references of the same kind",
    "numbering": "numbering slips of the same kind",
    "amount": "amount points of the same kind",
    "date": "date points of the same kind",
    "deadline": "deadline points of the same kind",
    "party-name": "party-name inconsistencies of the same kind",
    "arithmetic": "arithmetic points of the same kind",
    "signature": "signature-page points of the same kind",
    "punctuation": "punctuation slips of the same kind",
    "leftovers": "leftovers of the same kind",
}


def _cap_per_category(findings: list[Finding]) -> list[Finding]:
    """Keep the worst few of each kind and count the rest in one line.

    Selection is by severity, not by position, so a blocker at the end of a
    long document is never dropped in favour of a formatting nit at the top.
    What survives stays in document order, because that is the order the
    redliner walks the document in.
    """
    by_category: dict[str, list[int]] = {}
    for i, f in enumerate(findings):
        by_category.setdefault(f.category, []).append(i)

    keep: set[int] = set()
    overflow: dict[str, list[int]] = {}
    for category, indexes in by_category.items():
        ranked = sorted(indexes, key=lambda i: (_SEVERITY_ORDER[findings[i].severity], i))
        keep.update(ranked[:_PER_CATEGORY_CAP])
        if len(ranked) > _PER_CATEGORY_CAP:
            overflow[category] = ranked[_PER_CATEGORY_CAP:]

    out = [f for i, f in enumerate(findings) if i in keep]
    for category, rest in overflow.items():
        # The collapsed line inherits the worst severity it is standing in for,
        # so hiding twenty blockers behind it cannot soften the verdict.
        worst = min(
            (findings[i].severity for i in rest), key=_SEVERITY_ORDER.__getitem__
        )
        kind = _CATEGORY_WORDS.get(category, "findings like these")
        out.append(
            Finding(
                severity=worst,
                category=category,
                title=f"and {len(rest)} more {kind}",
                explanation=(
                    f"There are {len(rest)} further {kind} beyond the "
                    "ones listed. They are the same kind of thing, and listing them "
                    "all would make this email unreadable. Reply and I will go "
                    "through the rest with you."
                ),
                anchor="",
                confidence=0.9,
            )
        )
    return out


def deadline_conflicts(doc: ExtractedDoc) -> list[Finding]:
    """Time limits that contradict each other; see deadlines.py, which also
    builds the reply's table of dates and deadlines from the same reading."""
    from secondeye.pipeline import deadlines

    return deadlines.conflicts(doc)


def arithmetic(doc: ExtractedDoc) -> list[Finding]:
    """Figures the document adds up and contradicts; see dealmath.py, which
    also gives the review agent every amount and table from the same reading."""
    from secondeye.pipeline import dealmath

    return dealmath.arithmetic(doc)


def run_all(doc: ExtractedDoc, content: bytes | None = None) -> list[Finding]:
    """Every deterministic check, in one pass."""
    if getattr(doc, "transcribed", False):
        # Text the model transcribed from a scan can be wrong in ways a text
        # layer cannot: a digit misread, a word regularised, a line skipped.
        # Every check here would report those as the document's mistakes, and a
        # check that is wrong on scans is the check that gets the product
        # filtered (PRODUCT.md). The model reviews the page images itself; the
        # transcription exists so a Word copy can be built and marked up.
        return []
    findings: list[Finding] = []
    for check in (
        placeholders,
        defined_terms,
        duplicate_definitions,
        term_misspellings,
        term_capitalization,
        undefined_terms,
        cross_references,
        numbering,
        amounts,
        currency_notation,
        date_consistency,
        weekday_mismatch,
        contradictory_dates,
        deadline_conflicts,
        party_names,
        signature_blocks,
        party_drift,
        arithmetic,
        punctuation,
    ):
        try:
            findings.extend(check(doc))
        except Exception:
            import logging

            logging.getLogger(__name__).exception("check %s failed", check.__name__)
    if content and doc.filename.lower().endswith(".docx"):
        findings.extend(leftovers(content))
    if content:
        # Everything else travelling in the file: properties naming another
        # client, embedded workbooks, network links, speaker notes, hidden
        # sheets, PDF mark-up (outbound.py). Any kind of file, not only Word.
        from secondeye.pipeline import outbound

        findings.extend(outbound.package(content, doc.filename,
                                         "\n".join(b.text for b in doc.blocks)))
    try:
        _label_locations(doc, findings)
    except Exception:  # a missing label never costs a finding
        import logging

        logging.getLogger(__name__).exception("could not label finding locations")
    return _cap_per_category(findings)


# Where a finding is, the way a lawyer would say it: "clause 3.2", "Schedule
# 4, paragraph 2". A paragraph index means nothing to the reader.
_LOCATION_NUMBER = re.compile(
    r"^\s*(?:(?:Section|Clause|Article|SECTION|CLAUSE|ARTICLE)\s+)?"
    r"([0-9]+(?:\.[0-9]+)*)[.)]?(?=\s)"
)
_LOCATION_SUB = re.compile(r"^\s*(\((?:[a-z]{1,3}|[ivx]{1,5}|[0-9]{1,2})\))\s")


def _locations(doc: ExtractedDoc) -> dict[int, str]:
    """Block index -> the clause it is in, or "" before the first clause."""
    word = _clause_word(doc)
    labels: dict[int, str] = {}
    annex = ""
    clause = ""
    sub = ""
    for block in doc.blocks:
        text = block.display
        title = _ANNEX_TITLE.match(text)
        if title:
            annex = f"{title.group(1).capitalize()} {title.group(2).upper()}"
            clause, sub = "", ""
            labels[block.index] = annex
            continue
        m = _LOCATION_NUMBER.match(text)
        if m and int(m.group(1).split(".")[0]) <= _MAX_CLAUSE_NUMBER:
            clause, sub = m.group(1), ""
        else:
            s = _LOCATION_SUB.match(text)
            if s and clause:
                sub = s.group(1)
        if annex:
            labels[block.index] = f"{annex}, paragraph {clause}{sub}" if clause else annex
        else:
            labels[block.index] = f"{word} {clause}{sub}" if clause else ""
    return labels


def _label_locations(doc: ExtractedDoc, findings: list[Finding]) -> None:
    """Fill in `where` for each finding whose anchor can be found."""
    labels = _locations(doc)
    for f in findings:
        if f.where or not f.anchor:
            continue
        for block in doc.blocks:
            if f.anchor in block.text:
                f.where = labels.get(block.index, "")
                break


def as_prompt_block(findings: list[Finding]) -> str:
    """Tell the agent what the deterministic pass already caught.

    Without this the agent re-reports the same numbering error in its own words
    and the lawyer reads it twice.
    """
    # Said in both branches, because the agent is told to stop looking at
    # mechanics and this is the one mechanic the deterministic pass does not do.
    gap = (
        "Of capitalised terms that were never defined it reports only multi-word "
        "phrases used repeatedly after an article, so a single-word term the "
        "document leans on with no definition is still yours to catch."
    )
    if not findings:
        return (
            "# Mechanical checks\n\nA deterministic pass over placeholders, defined "
            "terms, cross-references, numbering, amounts, dates, party names and "
            "document metadata found nothing. Do not go looking for those again; "
            "spend your attention on substance. " + gap
        )
    lines = [
        "# Mechanical checks already done",
        "",
        (
            "A deterministic pass already found the following. They are in the report "
            "and you must NOT report them again. Mention one only if it changes your "
            "reading of something substantive. " + gap
        ),
        "",
    ]
    lines += [f"- [{f.severity.value}] {f.title}" for f in findings]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _anchor(text: str, start: int, end: int, pad: int = 25) -> str:
    """A verbatim span wide enough to be unique, so the redliner can find it.

    Widened to whole words. A span cut mid-word ("'loyment on thirty (13)'")
    is what the lawyer is shown as the place in their document, and it reads
    like a fault in the tool.
    """
    s, e = max(0, start - pad), min(len(text), end + pad)
    while s > 0 and not text[s - 1].isspace():
        s -= 1
    while e < len(text) and not text[e].isspace():
        e += 1
    return text[s:e].strip()


def _anchor_right(text: str, start: int, end: int, pad: int = 25) -> str:
    """From `start`, running on past `end` to the end of a word."""
    return _anchor(text, start, end, 0) if end >= len(text) else \
        text[start:_anchor_end(text, min(len(text), end + pad))].strip()


def _anchor_end(text: str, pos: int) -> int:
    while pos < len(text) and not text[pos].isspace():
        pos += 1
    return pos


def _join(items: list[str]) -> str:
    """"the body", "the body and a header", "the body, a header and a footer"."""
    if len(items) <= 1:
        return items[0] if items else ""
    return ", ".join(items[:-1]) + " and " + items[-1]


def _in_quotes(text: str, pos: int) -> bool:
    return text.count('"', 0, pos) % 2 == 1 or text.count("“", 0, pos) > text.count(
        "”", 0, pos
    )


def _dedupe(findings: list[Finding]) -> list[Finding]:
    seen: set[tuple[str, str]] = set()
    out: list[Finding] = []
    for f in findings:
        key = (f.title, f.anchor)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out
