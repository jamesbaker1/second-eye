"""The other half of the learning loop: what the lawyer actually sent.

An undo tells us one change was refused. The version that leaves the building
tells us about every change at once: which of our tracked changes were accepted
into it, which were rejected, and which fell inside a passage the lawyer
rewrote. `memory.record_outcome` was written for this source and, until now,
only the conversation fed it.

The signal arrives through the BCC habit (docs/ux.md): the lawyer copies the
agent on the email that goes to the client, the document attached is the sent
version, and this module finds the conversation it came from and reads the
result. Nothing here may fail the review that follows it, and nothing here is
ever reported as a finding; the reply gets one sentence saying what was learned,
so the lawyer sees the loop close.
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from functools import lru_cache

from secondeye import memory, thread
from secondeye.models import Attachment
from secondeye.pipeline import extract

log = logging.getLogger(__name__)

# How alike the sent document and a conversation's original must be, when the
# filename does not settle it, before the conversation is taken to be the one
# it came from. The lawyer accepted some changes and edited a little, so the
# two are close but not identical.
MIN_SIMILARITY = 0.8
# How many of the lawyer's recent conversations to read originals for when the
# filename does not match any of them. Each is a fetch from object storage.
MAX_CANDIDATES = 10

# The words a filename gains between the draft and the version sent.
_FILENAME_NOISE = re.compile(
    r"\((?:redline|comparison|clean)\)|\b(?:redline|clean|final|execution|exec|signed|"
    r"v(?:ersion)?\s*\d+|draft\s*\d*|rev(?:ision)?\s*\d*)\b|[\s._-]+",
    re.IGNORECASE,
)
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


@dataclass
class Reconciliation:
    thread_id: str
    accepted: list[thread.Change] = field(default_factory=list)
    rejected: list[thread.Change] = field(default_factory=list)
    unknown: list[thread.Change] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.accepted) + len(self.rejected) + len(self.unknown)

    def sentence(self) -> str:
        """One line for the reply, so the lawyer sees the loop close."""
        n = self.total
        kept = len(self.accepted)
        plural = "s" if n != 1 else ""
        parts = [f"Of the {n} change{plural} I suggested on this document, you kept {kept}"]
        if self.rejected:
            kinds = _count_kinds(self.rejected)
            parts.append(f" and dropped {len(self.rejected)} ({kinds})")
        if self.unknown:
            parts.append(f"; {len(self.unknown)} fell inside passages you rewrote")
        # Only said when it is so (LEARN_FROM_OUTCOMES): by default nothing
        # is learned from what a lawyer drops unless they ask.
        tail = " I learn from what you drop." if memory.learning() else ""
        return "".join(parts) + "." + tail


def reconcile(job_id: str, user: str, sent: Attachment) -> Reconciliation | None:
    """Find the conversation this sent document came from and record what
    happened to each of our changes. None when it came from no conversation
    we hold, or none of them had changes to check."""
    sent_text = _text_of(sent)
    if not sent_text:
        return None
    state = conversation_for(user, sent, sent_text)
    if state is None or not state.active_changes:
        return None

    result = Reconciliation(thread_id=state.id)
    for change in state.active_changes:
        getattr(result, _fate(change, sent_text)).append(change)

    for change in result.accepted:
        memory.record_outcome(job_id, user, state.matter_id, change.category, "",
                              change.title, change.anchor, change.replacement, "accepted")
    for change in result.unknown:
        memory.record_outcome(job_id, user, state.matter_id, change.category, "",
                              change.title, change.anchor, change.replacement, "unknown")
    if result.rejected:
        # Records the outcomes and, past the threshold, promotes the kind of
        # change to a suppression, exactly as an undo does.
        memory.record_rejections(job_id, user, state.matter_id, result.rejected)
    log.info("reconciled %s: %d accepted, %d rejected, %d unknown", state.id,
             len(result.accepted), len(result.rejected), len(result.unknown))
    return result


def _fate(change: thread.Change, sent_text: str) -> str:
    """accepted, rejected or unknown, by whether our words or the original words
    survived into the sent version."""
    replacement = _norm(change.replacement or "")
    anchor = _norm(change.anchor)
    if change.edit_kind == "insert_after":
        return "accepted" if replacement and replacement in sent_text else "rejected"
    has_ours = bool(replacement) and replacement in sent_text
    has_theirs = bool(anchor) and anchor in sent_text
    if has_ours and not has_theirs:
        return "accepted"
    if has_theirs and not has_ours:
        return "rejected"
    if has_ours and has_theirs:
        # Both present: the anchor recurs elsewhere in the document, and our
        # replacement is in it. Accepted is the honest reading.
        return "accepted"
    return "unknown"


def conversation_for(user: str, sent: Attachment, sent_text: str,
                     require_changes: bool = True,
                     exclude: str | None = None) -> thread.ThreadState | None:
    """The lawyer's conversation this document came from: by filename first,
    then by how alike the texts are.

    `require_changes` is the learning loop's need: a conversation with no
    active changes has nothing to score. The "since last time" comparison and
    "compare this to the one I sent" want the conversation whatever happened
    in it, so they pass False. `exclude` is the conversation the caller is
    already in, which would otherwise match itself by name.
    """
    candidates = [(key, name) for key, name in thread.recent_for_owner(user, limit=50)
                  if key != exclude]
    if not candidates:
        return None

    def usable(state: thread.ThreadState | None) -> bool:
        if state is None or not state.original:
            return False
        return bool(state.active_changes) if require_changes else True

    stem = _stem(sent.filename)
    by_name = [key for key, filename in candidates if _stem(filename) == stem]
    for key in by_name:
        state = thread.load(key)
        if usable(state):
            return state

    for key, _ in candidates[:MAX_CANDIDATES]:
        # The document alone first. Most of a lawyer's conversations are about
        # other documents, and reading each one's whole ledger to learn that
        # was two database round trips apiece on every review.
        held = thread.original_of(key)
        if held is None or not held[1]:
            continue
        filename, original = held
        original_text = _text_of(Attachment(filename=filename, content_type="",
                                            size_bytes=len(original), content=original))
        if not original_text or not _similar(original_text, sent_text):
            continue
        state = thread.load(key)
        if usable(state):
            return state
    return None


# Below this word-level likeness two texts are different documents, and the
# character-level measure is not run. Measured over every pair in the eval
# corpus and edits of each at up to 40% of words: no pair at or above
# MIN_SIMILARITY by characters scored below 0.76 by words, so 0.5 leaves a wide
# margin and only ever skips work whose answer was already no.
_WORD_PREFILTER = 0.5


def _similar(a: str, b: str) -> bool:
    """Whether two texts are the same document at different drafts.

    The head of each is enough to tell one contract from another. Character
    by character with autojunk off, this took a quarter of a second per pair
    on a long agreement, and every review ran it against up to ten of the
    lawyer's other conversations to find the one it came from: seconds before
    the model was even called, nearly all of it spent proving that an NDA is
    not a share purchase agreement. Two cheap tests go first, each only able
    to answer no: the lengths (which bound the ratio exactly), then the same
    measure over words, which is a hundred times faster. Cached on the heads,
    because the match found here is measured again before it is compared.
    """
    return _similar_heads(a[:20_000], b[:20_000])


@lru_cache(maxsize=32)
def _similar_heads(a: str, b: str) -> bool:
    total = len(a) + len(b)
    if total and 2 * min(len(a), len(b)) / total < MIN_SIMILARITY:
        return False
    words = SequenceMatcher(None, a.split(), b.split(), autojunk=False)
    if words.ratio() < _WORD_PREFILTER:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= MIN_SIMILARITY


def _text_of(att: Attachment) -> str:
    """The document's words, normalised. Cached by content, because the same
    originals are read on every review a lawyer sends (the candidates above)
    and twice within one (the match, then the comparison against it). The
    digest is the key, so a changed file is a miss and no document is held."""
    ext = att.filename.rsplit(".", 1)[-1].lower() if "." in att.filename else ""
    key = (ext, hashlib.sha256(att.content or b"").digest())
    with _TEXTS_LOCK:
        if key in _TEXTS:
            _TEXTS.move_to_end(key)
            return _TEXTS[key]
    try:
        doc = extract.extract(att)
    except Exception:  # noqa: BLE001 - a document we cannot read is one we cannot learn from
        return ""
    text = _norm(" ".join(b.text for b in doc.blocks))
    with _TEXTS_LOCK:
        _TEXTS[key] = text
        while len(_TEXTS) > _TEXTS_KEPT:
            _TEXTS.popitem(last=False)
    return text


_TEXTS: OrderedDict[tuple[str, bytes], str] = OrderedDict()
_TEXTS_KEPT = 32
_TEXTS_LOCK = threading.Lock()


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.translate(_QUOTES)).strip()


def _stem(filename: str) -> str:
    base = filename.rsplit(".", 1)[0] if "." in filename else filename
    return _FILENAME_NOISE.sub("", base).lower()


def _count_kinds(changes: list[thread.Change]) -> str:
    counts: dict[str, int] = {}
    for c in changes:
        counts[c.category or "other"] = counts.get(c.category or "other", 0) + 1
    return ", ".join(f"{k} x{n}" if n > 1 else k
                     for k, n in sorted(counts.items(), key=lambda kv: -kv[1]))
