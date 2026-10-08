"""The docs name files. The files have to exist, or the doc has to say they do not.

The audit found `ROADMAP.md` introducing three structural additions with the
same sentence -- "That is `pipeline/router.py`", "That is `archive.py`", "That
is `tracker.py`" -- when only two of them had been written. A reader cannot tell
a description of the repo from an intention when they are phrased identically,
and the cost lands on the next engineer, who greps for a file that was never
deleted because it was never there.

So: every path a doc names in backticks resolves, and anything that does not
exist yet has to be listed below, deliberately, in the future tense.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

DOCS = [
    ROOT / "README.md",
    ROOT / "STATUS.md",
    ROOT / "ROADMAP.md",
    ROOT / "PLAN.md",
    ROOT / "DECISIONS.md",
    ROOT / "PRODUCT.md",
    *sorted((ROOT / "docs").glob("*.md")),
]

# Where a bare filename in prose is allowed to live. `checks.py` means
# `src/secondeye/pipeline/checks.py`, and nobody writing a sentence wants to type that.
SEARCH_PATHS = ["", "src/secondeye/", "src/secondeye/pipeline/", "src/secondeye/prompts/", "docs/",
                "agents/", "skills/lra-document-tools/", "skills/lra-document-tools/scripts/",
                "skills/lra-closing/scripts/"]

# Named in the docs, deliberately not written yet. Each must be phrased as
# something that will exist, never as something that does. Deleting a name from
# this list means either building it or striking it from the doc.
UNBUILT = {
    "tracker.py",  # ROADMAP phase A item 4: derived state, not started.
}

_PATH_IN_BACKTICKS = re.compile(r"`([A-Za-z0-9_./-]+\.(?:py|md))`")
# The construction that caused the confusion in the first place.
_PRESENT_TENSE = re.compile(r"That is `([A-Za-z0-9_./-]+\.py)`")


def _resolves(name: str) -> bool:
    return any((ROOT / (prefix + name)).exists() for prefix in SEARCH_PATHS)


def test_every_file_the_docs_name_either_exists_or_is_listed_as_unbuilt():
    unresolved: dict[str, list[str]] = {}
    for doc in DOCS:
        for name in sorted(set(_PATH_IN_BACKTICKS.findall(doc.read_text()))):
            if _resolves(name) or name in UNBUILT:
                continue
            unresolved.setdefault(name, []).append(doc.name)
    assert not unresolved, (
        f"docs name files that do not exist: {unresolved}. Either build them, "
        "strike them, or add them to UNBUILT and write them in the future tense."
    )


def test_nothing_unbuilt_is_introduced_as_though_it_exists():
    offenders = []
    for doc in DOCS:
        for name in _PRESENT_TENSE.findall(doc.read_text()):
            if name in UNBUILT or not _resolves(name):
                offenders.append(f"{doc.name}: \"That is `{name}`\"")
    assert not offenders, (
        "a file that does not exist is introduced in the present tense: "
        f"{offenders}. Write \"That will be\" so a reader can tell the "
        "architecture from the intention."
    )


def test_unbuilt_list_holds_nothing_that_has_since_been_written():
    """Otherwise the list quietly becomes a licence to describe built things vaguely."""
    built = sorted(name for name in UNBUILT if _resolves(name))
    assert not built, (
        f"{built} now exists; take it out of UNBUILT and say so in the docs."
    )
