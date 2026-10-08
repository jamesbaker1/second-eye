"""The container image carries what the code reads from the repository.

managed.py, skillsync.py and playbook.py find agents/ and skills/ from their
own file. The image shipped neither, and installed the package where that
lookup lands in site-packages' parent, so every live review failed to load
its rubric on Cloudflare while every local run passed.
"""

from pathlib import Path

from secondeye import managed, skillsync

ROOT = Path(__file__).resolve().parents[1]


def test_the_image_copies_agents_and_skills_and_installs_in_place():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "COPY agents ./agents" in dockerfile
    assert "COPY skills ./skills" in dockerfile
    assert "pip install --no-cache-dir --no-deps -e ." in dockerfile


def test_the_repository_folders_resolve_beside_src():
    assert managed.AGENTS == ROOT / "agents"
    assert skillsync.SKILLS == ROOT / "skills"
    assert (managed.AGENTS / "review_rubric.md").exists()


def test_dockerignore_does_not_exclude_them():
    ignored = (ROOT / ".dockerignore").read_text().split()
    assert "agents" not in ignored and "skills" not in ignored
