"""scripts/export_public.py: the public repository's one commit, less what is private.

Every test builds a throwaway git repository; nothing here reads or writes
this one. gitleaks is switched off (it may not be installed), except where a
test says otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("export_public", ROOT / "scripts" / "export_public.py")
export_public = importlib.util.module_from_spec(_spec)
sys.modules["export_public"] = export_public
_spec.loader.exec_module(export_public)

TODO = "RELEASE" + "-TODO"      # never literally in an exported file


@pytest.fixture(autouse=True)
def _no_gitleaks(monkeypatch):
    monkeypatch.setattr(export_public, "find_gitleaks", lambda: None)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-c", "user.name=T", "-c", "user.email=t@example.com",
                           "-c", "commit.gpgsign=false", *args],
                          cwd=repo, check=True, capture_output=True, text=True).stdout


def _repo(tmp_path: Path, files: dict[str, str | bytes], *, ignore: str = "", deny: str = "") -> Path:
    repo = tmp_path / "src"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    files = {".publicignore": ignore, ".publicdeny": deny, **files}
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    _git(repo, "add", "--all", "--force")
    _git(repo, "commit", "-q", "-m", "private history")
    return repo


def _docx(text: str) -> bytes:
    import io

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("word/document.xml", f"<w:document><w:body><w:p><w:r><w:t>{text}</w:t>"
                                        "</w:r></w:p></w:body></w:document>")
    return buf.getvalue()


def _run(repo: Path, out: Path, *extra: str) -> int:
    return export_public.main(["--repo", str(repo), "--out", str(out), *extra])


# --- .publicignore -----------------------------------------------------------------


@pytest.mark.parametrize("pattern, path, dropped", [
    ("work/", "work/a.txt", True),
    ("work/", "src/work/a.txt", True),          # unanchored directory: any depth
    ("work/", "work", False),                   # a file called work is not the directory
    ("/work/", "src/work/a.txt", False),        # anchored at the root
    ("deployments/jim/", "deployments/jim/tenant.jsonc", True),
    ("deployments/jim/", "x/deployments/jim/tenant.jsonc", False),   # inner / anchors
    ("deployments/jim/", "deployments/jimmy/tenant.jsonc", False),
    (".env", ".env", True),
    (".env", "cloudflare/.env", True),
    (".env", ".env.example", False),
    ("*.sqlite3", "data/lra.sqlite3", True),
    ("*.sqlite3", "data/lra.sqlite3.bak", False),
    ("docs/*.md", "docs/a.md", True),
    ("docs/*.md", "docs/sub/a.md", False),      # * does not cross /
    ("docs/**/*.md", "docs/sub/deeper/a.md", True),
    ("docs/**/*.md", "docs/a.md", True),        # **/ matches zero directories
    ("**/secret", "a/b/secret", True),
    ("**/secret", "secret", True),
    ("notes/**", "notes/a/b.txt", True),
    ("notes/**", "notesx/a.txt", False),
    ("a?c", "abc", True),
    ("file[0-9].txt", "file7.txt", True),
    ("file[!0-9].txt", "file7.txt", False),
])
def test_ignore_patterns_match_as_gitignore_does(pattern, path, dropped):
    rules = export_public.parse_ignore(f"# a comment\n\n{pattern}\n")
    assert (export_public.ignored_by(path, rules) is not None) is dropped


@pytest.mark.parametrize("line", ["!keep.txt", r"\#literal"])
def test_unsupported_ignore_syntax_is_refused_not_misread(line):
    with pytest.raises(export_public.ExportError, match="not supported"):
        export_public.parse_ignore(line)


# --- refusals ---------------------------------------------------------------------


def test_a_denylisted_string_in_a_text_file_stops_the_export(tmp_path, capsys):
    repo = _repo(tmp_path, {"README.md": "intro\nmail me at boss@secret-firm.test\n"},
                 deny="# the firm\nsecret-firm\n")
    out = tmp_path / "out"
    assert _run(repo, out) == 1
    err = capsys.readouterr().err
    assert "README.md:2: matches 'secret-firm'" in err
    assert not out.exists()


def test_the_denylist_is_case_insensitive_and_checks_paths(tmp_path, capsys):
    repo = _repo(tmp_path, {"notes/Secret-Firm.txt": "nothing here\n"}, deny="secret-firm\n")
    assert _run(repo, tmp_path / "out") == 1
    assert "notes/Secret-Firm.txt: matches 'secret-firm'" in capsys.readouterr().err


def test_a_denylisted_string_inside_a_docx_stops_the_export(tmp_path, capsys):
    repo = _repo(tmp_path, {"samples/letter.docx": _docx("Dear Secret-Firm LLP")}, deny="secret-firm\n")
    out = tmp_path / "out"
    assert _run(repo, out) == 1
    assert "samples/letter.docx!word/document.xml:1: matches 'secret-firm'" in capsys.readouterr().err
    assert not out.exists()


def test_a_release_todo_marker_stops_the_export_with_no_denylist(tmp_path, capsys):
    repo = _repo(tmp_path, {"SECURITY.md": f"Report to {TODO}(contact).\n"})
    assert _run(repo, tmp_path / "out") == 1
    assert f"SECURITY.md:1: matches '{TODO}'" in capsys.readouterr().err


def test_ignored_files_are_not_scanned(tmp_path):
    repo = _repo(tmp_path, {"docs/private/plan.md": "secret-firm\n", "README.md": "public\n"},
                 ignore="docs/private/\n", deny="secret-firm\n")
    assert _run(repo, tmp_path / "out") == 0


def test_uncommitted_changes_are_refused_unless_allowed(tmp_path, capsys):
    repo = _repo(tmp_path, {"README.md": "committed\n"})
    (repo / "README.md").write_text("edited, not committed\n")
    out = tmp_path / "out"
    assert _run(repo, out) == 1
    assert "uncommitted changes" in capsys.readouterr().err
    assert not out.exists()
    # Allowed, it exports HEAD as committed, not the edit.
    assert _run(repo, out, "--allow-dirty") == 0
    assert (out / "README.md").read_text() == "committed\n"


def test_a_non_empty_output_directory_is_refused(tmp_path, capsys):
    repo = _repo(tmp_path, {"README.md": "x\n"})
    out = tmp_path / "out"
    out.mkdir()
    (out / "keep").write_text("mine")
    assert _run(repo, out) == 1
    assert "not an empty directory" in capsys.readouterr().err
    assert (out / "keep").read_text() == "mine"


def test_gitleaks_findings_stop_the_export(tmp_path, monkeypatch):
    repo = _repo(tmp_path, {"README.md": "x\n"})
    monkeypatch.setattr(export_public, "find_gitleaks", lambda: "gitleaks")

    def leak(binary, tree):
        assert (tree / "README.md").exists()
        raise export_public.ExportError("gitleaks found secrets")

    monkeypatch.setattr(export_public, "run_gitleaks", leak)
    out = tmp_path / "out"
    assert _run(repo, out) == 1
    assert not out.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["src"], "the staging directory is cleaned up"


# --- the happy path ---------------------------------------------------------------


def test_the_export_is_one_commit_by_the_given_author_without_ignored_paths(tmp_path, capsys):
    repo = _repo(tmp_path, {
        "README.md": "public\n",
        "src/app.py": "print('hi')\n",
        "bin/run": "#!/bin/sh\necho run\n",
        "deployments/jim/tenant.jsonc": "{}\n",
        "deployments/acme/tenant.jsonc": "{}\n",
        "work/scratch.txt": "x\n",
        ".claude/settings.json": "{}\n",
        "samples/letter.docx": _docx("Dear Sir"),
        ".gitignore": "*.docx\n",     # committed by force; must still be exported
    }, ignore="deployments/jim/\nwork/\n.claude/\n.publicignore\n", deny="secret-firm\n")
    os.chmod(repo / "bin" / "run", 0o755)
    _git(repo, "add", "bin/run")
    _git(repo, "commit", "-q", "-m", "executable")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "more private history")

    out = tmp_path / "public"
    assert _run(repo, out, "--author", "Pat Example <pat@example.com>", "--message", "Hello") == 0
    printed = capsys.readouterr().out
    assert "gh repo create jamesbaker1/<name> --public --source" in printed

    log = _git(out, "log", "--format=%an <%ae>|%cn <%ce>|%s").splitlines()
    assert log == ["Pat Example <pat@example.com>|Pat Example <pat@example.com>|Hello"]
    assert _git(out, "branch", "--show-current").strip() == "main"
    assert _git(out, "remote").strip() == ""
    files = sorted(_git(out, "ls-files").splitlines())
    assert files == [".gitignore", "README.md", "bin/run", "deployments/acme/tenant.jsonc",
                     "samples/letter.docx", "src/app.py"]
    assert _git(out, "ls-files", "-s", "bin/run").startswith("100755")
    assert os.stat(out / "bin" / "run").st_mode & stat.S_IXUSR
    assert not (out / ".publicdeny").exists(), "the denylist is never exported"
    assert _git(out, "status", "--porcelain") == ""


def test_the_default_author_is_the_owners_noreply_address(tmp_path):
    repo = _repo(tmp_path, {"README.md": "x\n"})
    out = tmp_path / "public"
    assert _run(repo, out) == 0
    assert _git(out, "log", "--format=%an <%ae>|%s").strip() == (
        "James Baker <26907599+jamesbaker1@users.noreply.github.com>|Initial public release")


@pytest.mark.skipif(not (ROOT / ".publicignore").exists(),
                    reason="the public repository is exported without its .publicignore")
def test_this_repositorys_export_rules_parse_and_cover_the_private_paths():
    rules = export_public.parse_ignore((ROOT / ".publicignore").read_text())
    for private in ["deployments/jim/tenant.jsonc", "docs/private/x.md", "work/a", ".claude/settings.json",
                    ".publicdeny", "samples/private/real.docx", ".env"]:
        assert export_public.ignored_by(private, rules), private
    for public in ["README.md", ".env.example", "deployments/acme-llp/tenant.jsonc", "samples/clean.docx",
                   "scripts/export_public.py", ".gitleaks.toml"]:
        assert export_public.ignored_by(public, rules) is None, public
