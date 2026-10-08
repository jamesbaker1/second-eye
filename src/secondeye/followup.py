"""Handling a reply about a document already under review.

The reply footer promises "reply in plain English and I will make the changes".
Until now a reply with no attachment got "I could not find a document to
review", which made the promise false on the most natural next action a lawyer
could take.

What is handled deterministically here, because it needs no model and must
never be got wrong:

  - an answer to a question the agent asked ("30", "13", "April 30, 2026")
  - undo, at any granularity the ledger can resolve
  - a new version of the document attached to the reply
  - a point dismissed by its letter: "B is fine", "ignore B and D"
  - several of these at once, with a clean copy: "B is fine; 30; clean copy"

Anything else is a free-form instruction and goes to the agent, with the thread
summary in front of it so it knows what it has already done.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from secondeye import thread
from secondeye.models import Attachment, Finding, InboundEmail, OutboundEmail, Severity
from secondeye.pipeline import reply as reply_mod
from secondeye.pipeline import router

log = logging.getLogger(__name__)

_UNDO_VERB = (
    r"(?:undo|revert|reverse|roll ?back|put (?:it|that|them) back|"
    r"take (?:it|that|them) out|leave (?:it|that) as it was|"
    r"don'?t change|do not change|restore)"
)
# The whole message has to BE the undo. Matching the verb anywhere meant
# "please don't change the governing law, but tighten the indemnity" reversed
# the indemnity change on the spot, and "restore the cap to 5m" reversed
# whatever shared a word with it. Anything longer or with a second request
# goes to the agent, which can read it properly.
_UNDO_COMMAND = re.compile(
    r"^\s*(?:(?:ok|okay|actually|sorry|hmm|no|thanks)[,.!]?\s+)*"
    r"(?:please\s+|can you\s+|could you\s+)?"
    + _UNDO_VERB + r"\b",
    re.IGNORECASE,
)
_SECOND_REQUEST = re.compile(
    r"\b(?:but|also|then|instead|tighten|add|insert|make|replace|amend|"
    r"increase|reduce|redraft|rewrite|change (?:it|this|that|them) to)\b"
    r"|;|\bto\s+(?:[£$€]|\d)",
    re.IGNORECASE,
)
_UNDO_MAX_WORDS = 14
_UNDO_EVERYTHING = re.compile(
    r"\b(everything|all of (?:it|them)|all (?:of )?your changes|all changes|"
    r"start over|from scratch)\b",
    re.IGNORECASE,
)
# "clause 7", "cl. 7.2", "section 4(a)". Separated out first so the 7 is never
# read as change number 7.
_CLAUSE_REF = re.compile(
    r"\b(?:clause|cl\.?|section|s\.|para(?:graph)?\.?|article)\s*"
    r"(\d+(?:\.\d+)*[a-z]?)",
    re.IGNORECASE,
)
_NUMBER_REF = re.compile(r"(?<![\w.£$€])#?(\d{1,3})(?![\w.%])")
_NOT_A_TARGET = {
    "undo", "revert", "reverse", "roll", "rollback", "the", "that", "this",
    "these", "those", "them", "please", "can", "could", "you", "change",
    "changes", "changed", "made", "make", "back", "put", "and", "but", "for",
    "take", "out", "leave", "was", "don't", "dont", "not", "restore", "one",
    "your", "edit", "edits", "fix", "correction", "clause", "section", "thanks",
    "number", "numbers", "both", "all",
}


@dataclass
class UndoMatch:
    """What an undo refers to. `ask` means say which ones exist, do nothing."""
    ids: list[int]
    described: str
    ask: bool = False


def is_undo_command(text: str) -> bool:
    body = _drop_sign_off(text)
    return (
        bool(_UNDO_COMMAND.search(body))
        and len(body.split()) <= _UNDO_MAX_WORDS
        and not _SECOND_REQUEST.search(body)
    )


def _drop_sign_off(text: str) -> str:
    """ "undo 2\n\nThanks\nJ" is still just "undo 2"."""
    lines = [ln.strip() for ln in (text or "").strip().splitlines() if ln.strip()]
    while len(lines) > 1 and len(lines[-1].split()) <= 3:
        lines.pop()
    return " ".join(lines)


def resolve_undo(state: thread.ThreadState, text: str) -> UndoMatch:
    """Which recorded changes a plain-English undo refers to.

    Acts only on an unambiguous reference: a change number from the email,
    "everything", a clause that holds exactly the changes meant, or words that
    pick out one change. Everything else asks. Reversing the wrong change is
    worse than asking, because the lawyer does not know it happened until the
    counterparty reads the clause.
    """
    active = state.active_changes
    if not active:
        return UndoMatch([], "there is nothing of mine left to undo")
    body = _drop_sign_off(text)

    if _UNDO_EVERYTHING.search(body):
        return UndoMatch([c.id for c in active], "everything I changed")

    clauses = [m.group(1).rstrip(".") for m in _CLAUSE_REF.finditer(body)]
    rest = _CLAUSE_REF.sub(" ", body)

    # "undo B" names a point the review raised, not a change it made. With one
    # change on the ledger the word-matching below read it as "undo that" and
    # reversed the change; a letter the lawyer can see is theirs to mean.
    # Lower case too ("undo b"), but not "a", which is a word before a letter.
    letters = [x.upper() for x in re.findall(r"\b([A-Za-z])\b", rest)
               if x != "a" and x.upper() in state.labelled]
    if letters:
        return UndoMatch(
            [], f"{' and '.join(letters)} {'is a point' if len(letters) == 1 else 'are points'}"
                " I raised, not one of my changes (to dismiss one, reply "
                f'"{letters[0]} is fine")', ask=True)

    numbers = [int(n) for n in _NUMBER_REF.findall(rest)]
    if numbers:
        by_number = {state.number(c): c for c in active}
        chosen = [by_number.get(n) for n in numbers]
        if all(chosen):
            return UndoMatch([c.id for c in chosen], _describe(state, chosen))
        missing = [str(n) for n, c in zip(numbers, chosen) if not c]
        return UndoMatch([], f"I have no change {' or '.join(missing)} to undo", ask=True)

    if clauses:
        located = _changes_in_clauses(state, clauses)
        if located:
            return UndoMatch([c.id for c in located], _describe(state, located))
        return UndoMatch(
            [], f"I could not tie clause {' or '.join(clauses)} to one of my changes",
            ask=True,
        )

    words = {w for w in re.findall(r"[\w']+", rest.lower()) if len(w) > 2}
    words -= _NOT_A_TARGET
    if not words:
        # "undo that" with one change is not ambiguous. With several it is.
        if len(active) == 1:
            return UndoMatch([active[0].id], _describe(state, active))
        return UndoMatch([], "I was not sure which change you meant", ask=True)

    scored: list[tuple[int, thread.Change]] = []
    for change in active:
        haystack = f"{change.anchor} {change.replacement or ''} {change.title}".lower()
        hits = sum(1 for w in words if re.search(rf"\b{re.escape(w)}", haystack))
        if hits:
            scored.append((hits, change))
    if not scored:
        return UndoMatch([], "I could not match that to one of my changes", ask=True)
    best = max(s for s, _ in scored)
    chosen = [c for s, c in scored if s == best]
    if len(chosen) > 1:
        return UndoMatch([], "that matches more than one of my changes", ask=True)
    return UndoMatch([chosen[0].id], _describe(state, chosen))


def undo_by_numbers(state: thread.ThreadState, numbers: list[int],
                    everything: bool = False) -> UndoMatch:
    """An undo a triage plan resolved to change numbers (TRIAGE=model). The
    same rule as `resolve_undo`: a number that names no change of ours, or
    no number at all, asks rather than guesses."""
    active = state.active_changes
    if not active:
        return UndoMatch([], "there is nothing of mine left to undo")
    if everything:
        return UndoMatch([c.id for c in active], "everything I changed")
    if not numbers:
        return UndoMatch([], "I was not sure which change you meant", ask=True)
    by_number = {state.number(c): c for c in active}
    missing = [str(n) for n in numbers if n not in by_number]
    if missing:
        return UndoMatch([], f"I have no change {' or '.join(missing)} to undo", ask=True)
    chosen = [by_number[n] for n in dict.fromkeys(numbers)]
    return UndoMatch([c.id for c in chosen], _describe(state, chosen))


def _describe(state: thread.ThreadState, changes: list) -> str:
    named = [f"change {state.number(c)} ({c.title.rstrip('.')})" for c in changes[:5]]
    more = len(changes) - len(named)
    return ", ".join(named) + (f" and {more} more" if more > 0 else "")


def _changes_in_clauses(state: thread.ThreadState, clauses: list[str]) -> list:
    """The active changes whose text sits in one of these clauses.

    Only as good as the typed numbering: an auto-numbered document has no
    clause numbers in its text, finds nothing here, and the lawyer is asked.
    """
    from secondeye.models import Attachment
    from secondeye.pipeline import compare, extract

    try:
        doc = extract.extract(Attachment(
            filename=state.filename, content_type=reply_mod.DOCX_TYPE,
            size_bytes=len(state.original), content=state.original,
        ))
    except Exception:
        log.exception("could not read the document to place a clause")
        return []
    texts = [b.text for b in doc.blocks]
    wanted = {c.lower() for c in clauses}
    found = []
    for change in state.active_changes:
        index = next((i for i, t in enumerate(texts) if change.anchor and change.anchor in t), None)
        if index is None:
            continue
        where = compare._where(texts, index).removeprefix("under ").lower()
        if where in wanted or any(where.startswith(w + ".") for w in wanted):
            found.append(change)
    return found


def undo_choices(state: thread.ThreadState, why: str) -> str:
    """The reply when an undo is not clear: every change, numbered, and how to
    name one. Doing nothing and asking is the whole point."""
    lines = [f"Which one? {why[0].upper() + why[1:]}, so I have not reversed anything.", ""]
    lines += [f"  {state.number(c)}. {c.title}" for c in state.active_changes]
    first = state.number(state.active_changes[0])
    lines += ["", f'Reply "undo {first}", or "undo everything".']
    return "\n".join(lines)


def apply_answer(state: thread.ThreadState, question: thread.Question,
                 value: str) -> Finding | None:
    """Turn an answered question into a change the writer can make."""
    if not question.anchor:
        return None
    replacement = _replacement_for(question, value)
    if replacement is None or replacement == question.anchor:
        return None
    return Finding(
        severity=Severity.FORMATTING,
        category="answered",
        title=_answer_title(value, question.anchor, replacement),
        explanation="",
        anchor=question.anchor,
        suggested_text=replacement,
        auto_apply=True,
    )


def _answer_title(value: str, anchor: str, replacement: str) -> str:
    """Describe the edit in the words that actually changed.

    Anchors carry surrounding context so they can be located uniquely, and
    quoting one whole made the reply read "I changed \"tinues for thirty (13)
    months from the E\"".
    """
    before, after = _differing_part(anchor, replacement)
    if before and after and len(before) < 60:
        return f'You said {value}, so "{before}" is now "{after}"'
    return f"You said {value}, so I made that change"


def _differing_part(before: str, after: str) -> tuple[str, str]:
    """Trim the common prefix and suffix so only the change remains."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    end = 0
    while (
        end < min(len(before), len(after)) - start
        and before[len(before) - 1 - end] == after[len(after) - 1 - end]
    ):
        end += 1
    # Widen to word boundaries so the quoted fragment reads as words.
    while start > 0 and before[start - 1].isalnum():
        start -= 1
    return before[start: len(before) - end].strip(), after[start: len(after) - end].strip()


def _replacement_for(question: thread.Question, value: str) -> str | None:
    """Rewrite the anchor using the answer.

    For a word/numeral disagreement the answer is one of the two numbers, and
    the fix is to make both halves agree rather than to paste the bare number
    over the whole phrase.
    """
    anchor = question.anchor or ""
    digits = value.replace(",", "").strip()
    if digits.isdigit():
        number = int(digits)
        words = _in_words(number)
        # "thirty (13)" -> "thirty (30)" or "thirteen (13)"
        m = re.search(r"([A-Za-z-]+)\s*\(\s*[\d,]+\s*\)", anchor)
        if m and words:
            return anchor[: m.start()] + f"{words} ({number:,})" + anchor[m.end():]
        return re.sub(r"[\d,]+", f"{number:,}", anchor) if re.search(r"\d", anchor) else None
    return value


_ONES = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight",
         "nine", "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
         "sixteen", "seventeen", "eighteen", "nineteen"]
_TENS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty",
         70: "seventy", 80: "eighty", 90: "ninety"}


def _in_words(n: int) -> str | None:
    if 0 <= n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        base = _TENS[tens * 10]
        return base if not ones else f"{base}-{_ONES[ones]}"
    return None


# --------------------------------------------------------------------------
# Dismissing a point by its letter: "B is fine", "ignore B and D"
# --------------------------------------------------------------------------
#
# Every finding a review shows that is not a tracked change carries a letter.
# Letters, because changes are numbered ("undo 2") and answers are usually
# numbers ("30"): neither can be read as the other. The whole part has to be
# the dismissal, in one of two shapes, so "B is fine but tighten clause 4" is
# not one and goes to the agent whole.

_LETTER = r"(?:(?:point|finding|item)\s+)?[A-Za-z]"
_LETTERS = _LETTER + r"(?:\s*(?:,\s*(?:and\s+)?|\s+and\s+|\s*&\s*|\s+)" + _LETTER + r")*"
_FINE = (r"(?:fine|ok|okay|good|all right|alright|right|correct|intended|intentional|"
         r"deliberate|not an issue|not a problem|no issue|as intended)"
         r"(?:\s+(?:as it is|as is))?")
_DISMISS = re.compile(
    r"^(?:(?:ok|okay|actually|thanks)[,.!]?\s+)*"
    r"(?:(?:please\s+)?(?:dismiss|ignore|drop|skip|disregard)\s+(?P<a>" + _LETTERS + r")"
    r"|(?P<b>" + _LETTERS + r")\s+(?:is|are)\s+(?:both\s+|all\s+)?" + _FINE + r")"
    r"(?:\s+(?:please|pls|thanks|thank you))*$",
    re.IGNORECASE,
)


@dataclass
class DismissMatch:
    """What a dismissal names. `ask` means none of it is done: a letter that
    names nothing is a typo, and dismissing the others around it would leave
    the lawyer believing the one they meant was dealt with."""
    ids: list[int]
    labels: list[str]
    described: str
    ask: bool = False


def dismissed_letters(text: str) -> list[str] | None:
    """The letters a "B is fine" names, upper-cased, or None if it is not one."""
    m = _DISMISS.match((text or "").strip(" .!"))
    if not m:
        return None
    named = re.sub(r"\b(?:point|finding|item|and)\b", " ", m.group("a") or m.group("b"),
                   flags=re.IGNORECASE)
    letters = re.findall(r"\b([A-Za-z])\b", named)
    return list(dict.fromkeys(x.upper() for x in letters)) or None


def resolve_dismiss(state: thread.ThreadState, letters: list[str]) -> DismissMatch:
    shown = state.labelled
    missing = [x for x in letters if x not in shown]
    if missing:
        which = " or ".join(missing)
        return DismissMatch([], letters, f"there is no point {which} in my last review",
                            ask=True)
    chosen = [shown[x] for x in letters]
    return DismissMatch([f.id for f in chosen], letters, _joined(letters))


def _joined(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def dismissed_said(state: thread.ThreadState, match: DismissMatch) -> str:
    """ "Noted: B is fine (Cl. 5: Uncapped indemnity)." The title rides along so
    a mistyped letter is seen for what it is before the lawyer moves on."""
    shown = state.labelled
    verb = "is" if len(match.labels) == 1 else "are"
    named = "; ".join(f"{x}: {_short(shown[x].title)}" if len(match.labels) > 1
                      else _short(shown[x].title) for x in match.labels)
    return f"Noted: {match.described} {verb} fine ({named})."


def _short(title: str, limit: int = 60) -> str:
    title = title.strip().rstrip(".")
    return title if len(title) <= limit else title[:limit - 1].rstrip() + "\u2026"


def dismiss_choices(state: thread.ThreadState, why: str) -> str:
    """The reply when a dismissal names a letter that is not there."""
    shown = [f for f in state.labelled.values() if not f.dismissed]
    lines = [f"Which one? {why[0].upper() + why[1:]}, so I have not dismissed anything.", ""]
    lines += [f"  {f.label}. {f.title}" for f in shown]
    if shown:
        lines += ["", f'Reply "{shown[0].label} is fine" to dismiss one.']
    return "\n".join(lines)


# --------------------------------------------------------------------------
# One reply, several things: "30; 4; clean copy"
# --------------------------------------------------------------------------
#
# The review's footer suggests answering every question in one reply, "30;
# 4", and then asks for "clean copy" as a second. Each of those parts is
# something this module already does deterministically on its own, but the
# message as a whole was none of them: "30; 4" went to the instruction agent
# (minutes, and a failure with no model), "30, then clean copy" likewise, and
# "30" and "clean copy" on two lines returned a clean copy with the answer
# silently dropped and "thirty (13)" still in it. So a message is split into
# its parts, and acted on here only when EVERY part is one of those things
# and none is in doubt. Anything else goes the way it always went.

# A full stop ends a part only before a capital or a request, and never after
# "cl", "s", "para", "sch", "art" or "no": "undo cl. 7" is one request.
_PART_BREAK = re.compile(
    r"\s*(?:;|\n+|"
    r"(?<!\bcl)(?<!\bs)(?<!\bpara)(?<!\bsch)(?<!\bart)(?<!\bno)"
    r"\.\s+(?=[A-Z]|(?:clean|accept|undo|revert|thanks?)\b)|"
    r",?\s+(?:and\s+)?then\s+|"
    r"(?:,|\s+and)\s+(?=(?:a\s+|the\s+|send\s+|please\s+)?clean\b|"
    r"accept\b|undo\b|revert\b|thanks?\b|cheers\b))\s*",
    re.IGNORECASE,
)
_CLEAN_PART = re.compile(
    r"^(?:(?:ok|okay|great|thanks|please|pls|now)\s+)*"
    r"(?:(?:send|give|make|do|get|attach|prepare)\s+(?:me\s+|us\s+)?)?"
    r"(?:(?:a|the)\s+)?clean(?:\s+(?:copy|version|one))?"
    r"(?:\s+(?:please|pls|thanks|thank you))*$"
    r"|^(?:(?:ok|okay|please)\s+)*accept\s+(?:all|everything|them\s+all|"
    r"all\s+(?:of\s+)?(?:the\s+|my\s+|your\s+)?changes)"
    r"(?:\s+(?:and\s+)?(?:send\s+(?:me\s+)?)?(?:a\s+|the\s+)?clean(?:\s+(?:copy|version))?)?"
    r"(?:\s+(?:please|pls|thanks))*$",
    re.IGNORECASE,
)


@dataclass
class ReplyPlan:
    """What a several-part reply asks for, in the order it is done: the undo,
    then the answers, then the clean copy of what results."""
    answers: list[tuple[thread.Question, str]]
    undo: UndoMatch | None = None
    clean: bool = False
    dismiss: DismissMatch | None = None

    @property
    def parts(self) -> int:
        return (len(self.answers) + (self.undo is not None) + self.clean
                + (self.dismiss is not None))


def plan_reply(state: thread.ThreadState, email: InboundEmail) -> ReplyPlan | None:
    """The reply as several deterministic requests, or None to handle it as
    before. None whenever any part is not understood, a part could answer
    two questions, or the message is a lone answer or undo, which already
    has its path. A lone clean-copy request comes back as a plan so that
    "accept all" is heard as one; the caller sends it down the clean-copy path.

    An undo in the plan acts only on what `resolve_undo` can name without
    asking. When it would ask, the plan comes back with that undo and nothing
    is done: the caller asks, as a lone undo would.
    """
    if email.attachments:
        return None
    segments = _segments(email)
    if len(segments) < 2:
        return _clean_copy_alone(segments) or _dismissal_alone(state, segments)

    plan = ReplyPlan(answers=[])
    unread: list[str] = []
    for segment in segments:
        kind = _part(state, segment, plan)
        if kind is None:
            unread.append(segment)
        elif unread:
            return None     # something not understood, with a request after it
    # A sign-off after the requests: "Jim", "Thanks, J", "Jim Baker". Up to
    # three words, capitalised, no digits. Anything else is a message we did
    # not understand, and it is not ours to act on half of it.
    if not all(_is_sign_off(s) for s in unread):
        return None
    if plan.parts == 0 or (plan.parts == 1 and plan.undo is not None):
        # Nothing asked, or a lone undo, which has its own path, sign-off and
        # all. A lone answer or clean copy with a "thanks" or a name after it
        # is ours: as one message it read as neither, and went to the model.
        return None
    return plan


def _is_sign_off(segment: str) -> bool:
    words = segment.split()
    return (len(words) <= 3 and not any(ch.isdigit() for ch in segment)
            and all(w[:1].isupper() for w in words))


def _segments(email: InboundEmail) -> list[str]:
    text = router.strip_reply(email.body)
    return [s.strip(" ,.!") for s in _PART_BREAK.split(text) if s and s.strip(" ,.!")]


def besides_clean_copy(email: InboundEmail) -> str:
    """What else a message asking for a clean copy asks for, when it is in a
    part of its own: "make the cap five million" above "clean copy". Empty
    when the rest is thanks and a name, or the message is one sentence.

    The clean-copy request used to win outright, so the instruction beside it
    was dropped without a word and the lawyer got a clean copy of a document
    that did not have the change they had just asked for.
    """
    from secondeye.pipeline import intake

    segments = _segments(email)
    if len(segments) < 2 or not any(intake._CLEAN_RE.search(s) for s in segments):
        return ""
    rest = [s for s in segments if not intake._CLEAN_RE.search(s) and not _CLEAN_PART.match(s)
            and not router.is_acknowledgement(s) and not _is_sign_off(s)]
    return "\n".join(rest)


def _dismissal_alone(state: thread.ThreadState, segments: list[str]) -> ReplyPlan | None:
    """ "B is fine" on its own: a plan, because the plan path is the one that
    dismisses, whether alone or beside answers and a clean copy."""
    if len(segments) != 1:
        return None
    plan = ReplyPlan(answers=[])
    return plan if _part(state, segments[0], plan) == "dismiss" else None


def _clean_copy_alone(segments: list[str]) -> ReplyPlan | None:
    """A clean-copy request on its own, "accept all" included: the clean-copy
    path knew "accept all changes" and "accept them all" but not the two words
    most people type, and sent those to the instruction agent."""
    if len(segments) == 1 and _CLEAN_PART.match(segments[0]):
        return ReplyPlan(answers=[], clean=True)
    return None


def _part(state: thread.ThreadState, segment: str, plan: ReplyPlan) -> str | None:
    """Add one part to the plan and say what it was, or None when it is not
    one we act on. An answer must fit exactly one open question not already
    answered in this reply."""
    if _CLEAN_PART.match(segment):
        plan.clean = True
        return "clean"
    letters = dismissed_letters(segment) if state.labelled else None
    if letters is not None:
        if plan.dismiss is not None:
            # "B is fine; D is fine": one dismissal of both.
            letters = plan.dismiss.labels + [x for x in letters if x not in plan.dismiss.labels]
        plan.dismiss = resolve_dismiss(state, letters)
        return "dismiss"
    taken = {q.id for q, _ in plan.answers}
    fits = [(q, v) for q in state.open_questions if q.id not in taken
            for v in [router.looks_like_an_answer(segment, q.options)] if v]
    if len(fits) == 1:
        plan.answers.append(fits[0])
        return "answer"
    if len(fits) > 1:
        return None
    if plan.undo is None and is_undo_command(segment):
        plan.undo = resolve_undo(state, segment)
        return "undo"
    if router.is_acknowledgement(segment):
        return "ack"
    return None


def classify(state: thread.ThreadState, email: InboundEmail) -> tuple[str, dict]:
    """What this reply is asking for. Deterministic cases only.

    Returns (kind, payload) where kind is one of "answer", "ack", "undo",
    "new_version" or "instruction". "instruction" means hand it to the agent.
    """
    text = router.strip_reply(email.body)

    if email.attachments:
        return "new_version", {}

    for question in state.open_questions:
        value = router.looks_like_an_answer(text, question.options)
        if value:
            return "answer", {"question": question, "value": value}

    # After the questions: "ok" to "1x or 2x?" is an answer; "ok" on its own
    # is the lawyer closing the loop, and the right reply to that is none.
    if router.is_acknowledgement(text):
        return "ack", {}

    if is_undo_command(text):
        return "undo", {"match": resolve_undo(state, text)}

    return "instruction", {"text": text}


def compose_update(email: InboundEmail, state: thread.ThreadState,
                   content: bytes | None, summary: str,
                   applied: list[Finding],
                   numbers: list[int | None] | None = None) -> object:
    """The reply after a round of changes.

    Deliberately not a review verdict. "Looks good. Minor cleanups only." at the
    top of a reply that only reversed a change reads as a fresh opinion on the
    document, which is not what happened.
    """
    lines = [summary, ""]
    if applied:
        lines.append("Tracked in the attached copy:")
        lines += _numbered(applied, numbers)
        lines.append("")
    still_open = state.open_questions
    if still_open:
        lines.append("Still waiting on:")
        lines += [f"  - {q.question}" for q in still_open]
        lines.append("")
    lines.append(
        'Reply with anything else to change, or "undo" and the number.'
        if applied else "Reply with anything else to change."
    )

    attachments = []
    if content:
        attachments.append(
            Attachment(
                filename=reply_mod._redline_name(state.filename),
                content_type=reply_mod.DOCX_TYPE,
                size_bytes=len(content),
                content=content,
            )
        )
    text = "\n".join(lines).rstrip() + "\n"
    return OutboundEmail(
        to=[email.from_address],
        subject=reply_mod._subject(email.subject, _short_verdict(summary)),
        text_body=text,
        html_body=reply_mod.text_as_html(text),
        in_reply_to=email.message_id,
        thread_id=email.thread_id,
        attachments=attachments,
    )


def _numbered(applied: list[Finding], numbers: list[int | None] | None) -> list[str]:
    """ "  2. Cl. 4: ..." when the change has a number, a bullet when not."""
    numbers = numbers or [None] * len(applied)
    lines = []
    for f, n in zip(applied, numbers):
        what = reply_mod._sentence(reply_mod._as_edit(f))
        lines.append(f"  {n}. {what}" if n else f"  - {what}")
    return lines


def compose_note(email: InboundEmail, text: str) -> OutboundEmail:
    """A short reply in the thread that changes nothing and attaches nothing."""
    body = text.rstrip() + "\n"
    return OutboundEmail(
        to=[email.from_address],
        subject=reply_mod._subject(email.subject, ""),
        text_body=body,
        html_body=reply_mod.text_as_html(body),
        in_reply_to=email.message_id,
        thread_id=email.thread_id,
    )


def _short_verdict(summary: str) -> str:
    """A few words for the subject line, taken from what actually happened."""
    first = summary.split(".")[0].strip()
    return first[:60] if first else "Updated"
