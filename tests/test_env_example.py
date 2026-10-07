"""`.env.example` is the only configuration documentation an operator reads.

Every drift between it and `Settings` has the same shape: an operator reaches
for a knob the file does not mention and concludes it does not exist, or sets
one the code stopped reading and watches nothing happen. Both had happened here
by the time of the audit -- MAX_REVIEW_CHARACTERS was missing, and TRIAGE_MODEL
outlived the setting it configured -- so the parity is asserted rather than
remembered.
"""

from __future__ import annotations

import re
from pathlib import Path

from lra.config import Settings

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"

# KEY=value, ignoring comments and blank lines. Values may be empty.
_ASSIGNMENT = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")


def _documented() -> dict[str, str]:
    out: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        match = _ASSIGNMENT.match(line.strip())
        if match:
            out[match.group(1)] = match.group(2).strip()
    return out


def test_every_setting_is_documented_in_env_example():
    documented = _documented()
    missing = sorted(
        name.upper() for name in Settings.model_fields if name.upper() not in documented
    )
    assert not missing, (
        f"settings with no line in .env.example: {missing}. An operator reading "
        "the file would conclude there is no knob for them."
    )


def test_env_example_documents_no_setting_that_was_removed():
    documented = _documented()
    fields = {name.upper() for name in Settings.model_fields}
    orphans = sorted(key for key in documented if key not in fields)
    assert not orphans, (
        f"lines in .env.example that no setting reads: {orphans}. Setting one of "
        "these changes nothing, which is worse than its absence: the operator "
        "believes they have configured something."
    )


# Settings where the template and the code are meant to disagree, with the
# reason. The code default is the one an unconfigured deployment gets, so it is
# chosen for safety; the template is a value a person edits, so it is chosen for
# usefulness. Anything not listed here disagreeing is drift, and fails.
DELIBERATE_DIVERGENCE: dict[str, str] = {}


def test_env_example_defaults_match_the_code():
    """A value in the file that disagrees with the code teaches the wrong default.

    Compared as text after normalising booleans and int separators, because the
    file is read by humans and by `pydantic-settings`, and only the latter cares
    about types.
    """

    def normalise(value: object) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return str(value).strip()

    documented = _documented()
    disagreements = []
    for name, field in Settings.model_fields.items():
        key = name.upper()
        if key not in documented or field.default is None:
            continue
        # Secrets and per-deployment addresses are deliberately left blank in the
        # example even where the code defaults to something.
        if documented[key] == "" and normalise(field.default) != "":
            continue
        if key in DELIBERATE_DIVERGENCE:
            continue
        if documented[key] != normalise(field.default):
            disagreements.append(f"{key}: file says {documented[key]!r}, "
                                 f"code defaults to {normalise(field.default)!r}")
    assert not disagreements, disagreements


def test_every_deliberate_divergence_is_actually_divergent():
    """An exception that has stopped being needed is an exception that hides
    the next real drift behind it."""

    def normalise(value: object) -> str:
        if isinstance(value, bool):
            return str(value).lower()
        return str(value).strip()

    documented = _documented()
    for key, reason in DELIBERATE_DIVERGENCE.items():
        assert reason.strip(), f"{key} is excepted with no reason given"
        name = key.lower()
        assert name in Settings.model_fields, f"{key} is not a setting any more"
        assert key in documented, f"{key} is not in .env.example any more"
        code = normalise(Settings.model_fields[name].default)
        assert documented[key] != code, (
            f"{key} no longer diverges (both are {code!r}); remove the exception"
        )
