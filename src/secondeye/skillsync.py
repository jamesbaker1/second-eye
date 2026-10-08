"""Build this product's own Agent Skill and upload it.

Anthropic's container runs whatever code the model writes. For most derived
work that is the point. For comparing versions, cleaning a file or checking a
contract it is a liability: the model's version of a comparison is different on
every run and has none of the refusals or proofs that `pipeline/compare.py`
has. A custom skill closes that gap by putting the tested code in the container
and telling the model to call it.

The skill in `skills/lra-document-tools` holds only the instructions, a few
thin scripts, the findings schema and a pydantic stand-in. `build()` copies the real modules in
beside them, so the container never runs a fork: what is uploaded is the source
tree at the moment of the sync.
"""

from __future__ import annotations

import shutil
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILLS = REPO / "skills"
SKILL_SOURCE = SKILLS / "lra-document-tools"
PACKAGE = Path(__file__).resolve().parent

# What the scripts import, and what those import. Nothing here reaches for
# configuration, the network or the database: a module that did would fail in
# the container, and tests/test_skillsync.py runs the built bundle to prove
# none does.
BUNDLED = (
    "__init__.py",
    "models.py",
    "report.py",
    "pipeline/__init__.py",
    "pipeline/checks.py",
    "pipeline/clean.py",
    "pipeline/compare.py",
    "pipeline/dealmath.py",
    "pipeline/deadlines.py",
    "pipeline/extract.py",
    "pipeline/filetype.py",
    "pipeline/ooxml.py",
    "pipeline/outbound.py",
    "pipeline/redline.py",
    "pipeline/timeline.py",
    "pipeline/turning.py",
    "pipeline/validate.py",
    "pipeline/wordcomments.py",
)

# The closing tools (skills/lra-closing): the document tools' modules, plus
# the signature pack it builds packets from and the state both sides check.
CLOSING_BUNDLED = BUNDLED + (
    "convert.py",
    "pipeline/closing_docs.py",
    "pipeline/closing_state.py",
    "pipeline/sigpack.py",
)

# The blackline (skills/lra-blackline): the document tools' modules, plus the
# PDF renderer and the local LibreOffice call it prefers when installed.
BLACKLINE_BUNDLED = BUNDLED + (
    "convert.py",
    "pipeline/blackline_pdf.py",
)

# Every skill this product ships, by folder name, with the display name the
# Skills API shows, which settings key holds its id once uploaded, and the
# modules bundled with it. A skill with modules carries code and also gets
# the document tools' stand-in pydantic; the others are instructions and
# files only.
CATALOGUE = {
    "lra-document-tools": ("LRA document tools", "sandbox_skill_id", BUNDLED),
    "lra-playbook": ("LRA playbook", "sandbox_playbook_skill_id", ()),
    "lra-key-terms": ("LRA key terms and summary", "sandbox_key_terms_skill_id", ()),
    "lra-closing": ("LRA closing", "sandbox_closing_skill_id", CLOSING_BUNDLED),
    "lra-comments": ("LRA comments", "sandbox_comments_skill_id", BUNDLED),
    # Standard library only: its rules are loaded by the host from the skill.
    "lra-negotiation": ("LRA negotiation ledger", "sandbox_negotiation_skill_id", ()),
    "lra-blackline": ("LRA blackline", "sandbox_blackline_skill_id", BLACKLINE_BUNDLED),
}
SHIMS = SKILL_SOURCE / "shims"


def build(target: Path, name: str = "lra-document-tools") -> Path:
    """Assemble the uploadable skill under `target` and return its folder."""
    if name not in CATALOGUE:
        raise KeyError(f"no such skill: {name}; known: {', '.join(CATALOGUE)}")
    source = SKILLS / name
    bundle = target / name
    if bundle.exists():
        shutil.rmtree(bundle)
    shutil.copytree(source, bundle,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "lib"))
    modules = CATALOGUE[name][2]
    for relative in modules:
        destination = bundle / "lib" / "secondeye" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PACKAGE / relative, destination)
    if modules and not (bundle / "shims").exists():
        shutil.copytree(SHIMS, bundle / "shims",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return bundle


def sync(skill_id: str = "", name: str = "lra-document-tools") -> tuple[str, str]:
    """Upload a skill. Returns (skill id, version id).

    With no id this creates the skill; with one it adds a version, and because
    the container is asked for `latest`, the next request picks it up.
    """
    import tempfile

    from anthropic.lib import files_from_dir

    from secondeye.config import anthropic_client

    display_name = CATALOGUE[name][0]
    client = anthropic_client()
    with tempfile.TemporaryDirectory() as scratch:
        bundle = build(Path(scratch), name)
        files = files_from_dir(bundle)
        if skill_id:
            version = client.skills.versions.create(skill_id, files=files)
            return skill_id, version.id
        skill = client.skills.create(files=files, display_name=display_name)
        return skill.id, skill.latest_version_id
