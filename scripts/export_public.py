#!/usr/bin/env python3
"""Export the committed tree, less what is private, to the public repository.

The public repository is not a mirror of this one. It started from a single
squashed commit of HEAD with the paths in `.publicignore` left out, and each
release adds one commit on top, so nothing in this repository's history (old
configs, a firm's name in a commit message, a key that was committed and
reverted) can reach it.

Create mode makes the first commit as a new repository and nothing else: it
never adds a remote, pushes or creates a GitHub repository. It prints those
steps for a person to run.

    python3 scripts/export_public.py --out ../second-eye-public

Update mode clones the existing public repository, replaces its whole working
tree with the export of HEAD (so a file deleted or newly ignored here is
deleted there) and commits that on top of its default branch. It does not push
unless --push is given; then it prints the commit's `git show --stat`, checks
that the public branch is still where it was when cloned, and pushes with a
plain, fast-forward-only push. It never rewrites the public history. If the
export matches the public tree already it says so and makes no commit. The
default message is "Update YYYY-MM-DD"; nothing from this repository's history
goes into it.

    python3 scripts/export_public.py --update https://github.com/jamesbaker1/second-eye.git
    python3 scripts/export_public.py --update <url-or-path> [--out DIR] [--message M] --push

Both modes, in order, stopping at the first failure without writing the
output repository (or, updating, without committing, and removing the clone):

1. Refuses if tracked files have uncommitted changes (unless --allow-dirty),
   since only committed content is exported and the difference would be a
   surprise.
2. Lists HEAD's tree (`git ls-tree`) and drops every path `.publicignore`
   matches. `.publicdeny` is always dropped.
3. Scans every remaining file for the regular expressions in `.publicdeny`
   and for the literal marker RELEASE_TODO (with a hyphen for the underscore),
   which marks a placeholder someone has still to fill in. Text files line by line; Office files (.docx, .xlsx,
   .pptx and their macro and template variants) member by member inside the
   zip; PDFs including their deflated streams; and every file's path. Any hit
   is printed as file:line (or file!member:line) with the pattern, and the
   export stops.
4. Writes the files with their modes to a staging directory and runs
   `gitleaks dir` over it when gitleaks is on PATH. Without gitleaks it warns
   and carries on: the CI secret scan (secrets.yml) still runs on every push
   to the public repository.
5. Create: moves the staging directory to --out, runs `git init -b main` and
   makes one commit with --author as both author and committer.
   Update: clones the public repository (to --out, or a temporary directory),
   deletes everything in it but `.git`, moves the staged files in and commits
   with --author as both author and committer.

`.publicignore` syntax is a subset of gitignore's: `#` comments, blank lines,
`*`, `?`, `[...]`, `**` (as `**/x`, `x/**` and `x/**/y`), a trailing `/` for a
directory, and a leading or inner `/` to anchor the pattern at the repository
root (otherwise it matches at any depth). Not supported, and refused rather
than misread: negation (`!pattern`) and backslash escapes.

`.publicdeny` holds one Python regular expression per line, matched
case-insensitively; `#` starts a comment line. It is read from HEAD and from
the working tree, and both are used.

Standard library only; Python 3.12.
"""

from __future__ import annotations

import argparse
import datetime
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

DEFAULT_AUTHOR = "James Baker <26907599+jamesbaker1@users.noreply.github.com>"
DEFAULT_MESSAGE = "Initial public release"
IGNORE_FILE = ".publicignore"
DENY_FILE = ".publicdeny"
# Never exported, whatever .publicignore says: the denylist names what must
# not be public, so it is itself private.
ALWAYS_IGNORED = (DENY_FILE,)
# A placeholder left for a person to fill in before release, e.g. where a
# contact address must go. Built from two halves so this file, which is
# exported, does not contain it.
RELEASE_TODO = "RELEASE" + "-TODO"
OFFICE_SUFFIXES = {".docx", ".docm", ".dotx", ".dotm", ".xlsx", ".xlsm", ".xltx",
                   ".xltm", ".pptx", ".pptm", ".potx", ".potm"}


class ExportError(Exception):
    """A reason to stop without writing the output repository."""


# --- .publicignore ----------------------------------------------------------------


@dataclass(frozen=True)
class IgnoreRule:
    pattern: str        # as written, for messages
    regex: re.Pattern[str]
    dir_only: bool


def _glob_to_regex(glob: str) -> str:
    """A gitignore glob, already stripped of anchoring and the trailing slash."""
    out: list[str] = []
    i, n = 0, len(glob)
    while i < n:
        if glob.startswith("**/", i) and (i == 0 or glob[i - 1] == "/"):
            out.append("(?:.*/)?")
            i += 3
        elif glob.startswith("**", i) and i + 2 == n and (i == 0 or glob[i - 1] == "/"):
            out.append(".*")
            i += 2
        elif glob[i] == "*":
            out.append("[^/]*")
            i += 1
        elif glob[i] == "?":
            out.append("[^/]")
            i += 1
        elif glob[i] == "[":
            end = glob.find("]", i + 2 if glob.startswith("[!", i) or glob.startswith("[^", i) else i + 1)
            if end == -1:
                out.append(re.escape("["))
                i += 1
                continue
            body = glob[i + 1:end]
            if body[:1] in ("!", "^"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = end + 1
        else:
            out.append(re.escape(glob[i]))
            i += 1
    return "".join(out)


def parse_ignore(text: str, source: str = IGNORE_FILE) -> list[IgnoreRule]:
    rules: list[IgnoreRule] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("!"):
            raise ExportError(f"{source}:{number}: negation ('{line}') is not supported")
        if "\\" in line:
            raise ExportError(f"{source}:{number}: backslash escapes ('{line}') are not supported")
        pattern = line
        dir_only = pattern.endswith("/")
        pattern = pattern.rstrip("/")
        if not pattern:
            raise ExportError(f"{source}:{number}: '{line}' would match everything")
        anchored = "/" in pattern
        pattern = pattern.lstrip("/")
        body = _glob_to_regex(pattern)
        regex = re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")
        rules.append(IgnoreRule(line, regex, dir_only))
    return rules


def ignored_by(path: str, rules: list[IgnoreRule]) -> IgnoreRule | None:
    """The rule that drops `path` (a file, repository-relative, `/`-separated).

    A rule matches the file itself or any directory above it; a rule ending in
    `/` only matches directories, so never the file itself.
    """
    parts = path.split("/")
    ancestors = ["/".join(parts[:k]) for k in range(1, len(parts))]
    for rule in rules:
        candidates = ancestors if rule.dir_only else ancestors + [path]
        if any(rule.regex.match(c) for c in candidates):
            return rule
    return None


# --- the denylist -----------------------------------------------------------------


@dataclass(frozen=True)
class DenyPattern:
    name: str
    regex: re.Pattern[str]


def parse_deny(text: str, source: str = DENY_FILE) -> list[DenyPattern]:
    patterns: list[DenyPattern] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(DenyPattern(line, re.compile(line, re.IGNORECASE)))
        except re.error as e:
            raise ExportError(f"{source}:{number}: not a regular expression: {e}") from None
    return patterns


def builtin_deny() -> list[DenyPattern]:
    return [DenyPattern(RELEASE_TODO, re.compile(re.escape(RELEASE_TODO)))]


@dataclass(frozen=True)
class Hit:
    where: str      # path, path:line, or path!member:line
    pattern: str


def _scan_text(text: str, where: str, patterns: list[DenyPattern]) -> list[Hit]:
    hits = []
    for number, line in enumerate(text.splitlines(), 1):
        for p in patterns:
            if p.regex.search(line):
                hits.append(Hit(f"{where}:{number}", p.name))
    return hits


def _decode(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        # Byte for byte, so an ASCII identifier inside a binary is still found.
        return data.decode("latin-1")


_PDF_STREAM = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)


def scan_file(path: str, data: bytes, patterns: list[DenyPattern]) -> list[Hit]:
    hits = [Hit(path, p.name) for p in patterns if p.regex.search(path)]
    suffix = Path(path).suffix.lower()
    if suffix in OFFICE_SUFFIXES or suffix == ".zip":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for member in z.infolist():
                    where = f"{path}!{member.filename}"
                    hits += [Hit(where, p.name) for p in patterns if p.regex.search(member.filename)]
                    hits += _scan_text(_decode(z.read(member)), where, patterns)
            return hits
        except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError):
            pass    # not really a zip: scanned as bytes below
    hits += _scan_text(_decode(data), path, patterns)
    if suffix == ".pdf":
        for k, m in enumerate(_PDF_STREAM.finditer(data), 1):
            try:
                inflated = zlib.decompress(m.group(1))
            except zlib.error:
                continue
            hits += _scan_text(_decode(inflated), f"{path}!stream{k}", patterns)
    return hits


# --- git ---------------------------------------------------------------------------


def git(repo: Path, *args: str, input: bytes | None = None, env: dict | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=repo, input=input, capture_output=True, env=env,
                            check=False)
    if result.returncode != 0:
        raise ExportError(f"git {' '.join(args)} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


@dataclass(frozen=True)
class Entry:
    mode: str       # 100644, 100755 or 120000
    sha: str
    path: str


def head_tree(repo: Path) -> list[Entry]:
    entries = []
    for record in git(repo, "ls-tree", "-r", "-z", "--full-tree", "HEAD").split(b"\0"):
        if not record:
            continue
        meta, path = record.split(b"\t", 1)
        mode, kind, sha = meta.decode().split()
        name = path.decode("utf-8", errors="surrogateescape")
        if kind != "blob":
            raise ExportError(f"{name}: a {kind} (submodule?) cannot be exported")
        entries.append(Entry(mode, sha, name))
    return entries


def read_blobs(repo: Path, shas: list[str]) -> dict[str, bytes]:
    """Every blob in one `git cat-file --batch`."""
    unique = list(dict.fromkeys(shas))
    if not unique:
        return {}
    out = git(repo, "cat-file", "--batch", input=("\n".join(unique) + "\n").encode())
    blobs, pos = {}, 0
    for sha in unique:
        header_end = out.index(b"\n", pos)
        got, _kind, size = out[pos:header_end].decode().split()
        start = header_end + 1
        blobs[got] = out[start:start + int(size)]
        pos = start + int(size) + 1
        assert got == sha, (got, sha)
    return blobs


def head_file(repo: Path, path: str) -> str | None:
    result = subprocess.run(["git", "show", f"HEAD:{path}"], cwd=repo, capture_output=True, check=False)
    return result.stdout.decode() if result.returncode == 0 else None


def dirty(repo: Path) -> list[str]:
    status = git(repo, "status", "--porcelain", "--untracked-files=no").decode()
    return [line for line in status.splitlines() if line.strip()]


# --- gitleaks ----------------------------------------------------------------------


def find_gitleaks() -> str | None:
    return shutil.which("gitleaks")


def run_gitleaks(binary: str, tree: Path) -> None:
    help_text = subprocess.run([binary, "--help"], capture_output=True, text=True,
                               check=False).stdout
    if re.search(r"^\s+dir\s", help_text, re.MULTILINE):
        command = [binary, "dir", str(tree)]
    else:   # gitleaks before 8.19 has no `dir`
        command = [binary, "detect", "--no-git", "--source", str(tree)]
    command += ["--redact", "--no-banner", "--exit-code", "1"]
    config = tree / ".gitleaks.toml"
    if config.exists():
        command += ["--config", str(config)]
    print("Running " + " ".join(command[:3]) + " ...")
    result = subprocess.run(command, check=False)
    if result.returncode != 0:
        raise ExportError(f"gitleaks found secrets in the export (exit {result.returncode}); see above")


# --- the export --------------------------------------------------------------------


def parse_author(author: str) -> tuple[str, str]:
    m = re.fullmatch(r"\s*(.+?)\s*<([^<>\s]+@[^<>\s]+)>\s*", author)
    if not m:
        raise ExportError(f"--author must look like 'Name <email>', not {author!r}")
    return m.group(1), m.group(2)


def _write_tree(root: Path, entries: list[Entry], blobs: dict[str, bytes]) -> None:
    for e in entries:
        target = root / e.path
        target.parent.mkdir(parents=True, exist_ok=True)
        data = blobs[e.sha]
        if e.mode == "120000":
            os.symlink(os.fsdecode(data), target)
            continue
        target.write_bytes(data)
        target.chmod(0o755 if e.mode == "100755" else 0o644)


def _commit(out: Path, name: str, email: str, message: str, expected: int) -> str | None:
    """Commits the working tree as it is; None, and no commit, if it matches HEAD."""
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": email}
    # The exported tree's own .gitignore, and anyone's global excludes, must
    # not drop a file that was committed here: hence --force.
    config = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false",
              "-c", "commit.gpgsign=false", "-c", f"user.name={name}", "-c", f"user.email={email}"]
    git(out, *config, "add", "--all", "--force", ".", env=env)
    has_head = subprocess.run(["git", "rev-parse", "-q", "--verify", "HEAD"], cwd=out,
                              capture_output=True, check=False).returncode == 0
    if has_head and subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=out,
                                   check=False).returncode == 0:
        return None
    git(out, *config, "commit", "-q", "--no-verify", "-m", message, env=env)
    committed = git(out, "ls-tree", "-r", "--name-only", "-z", "HEAD").count(b"\0")
    if committed != expected:
        raise ExportError(f"committed {committed} files, expected {expected}")
    return git(out, "rev-parse", "HEAD").decode().strip()


def _filtered_head(repo: Path, allow_dirty: bool) -> tuple[list[Entry], dict[str, bytes]]:
    """HEAD's files less `.publicignore`, with their contents, denylist-checked."""
    if not allow_dirty and (changes := dirty(repo)):
        raise ExportError("uncommitted changes to tracked files (commit them, or --allow-dirty "
                          "to export HEAD without them):\n  " + "\n  ".join(changes[:20]))
    if allow_dirty and dirty(repo):
        print("warning: uncommitted changes are not exported; this is HEAD as committed")

    ignore_text = head_file(repo, IGNORE_FILE)
    if ignore_text is None:
        raise ExportError(f"no {IGNORE_FILE} committed at HEAD; refusing to export everything")
    rules = parse_ignore(ignore_text)
    rules += [IgnoreRule(p, re.compile("^" + re.escape(p) + "$"), False) for p in ALWAYS_IGNORED]

    deny_texts = [t for t in (head_file(repo, DENY_FILE),
                              (repo / DENY_FILE).read_text() if (repo / DENY_FILE).exists() else None)
                  if t is not None]
    if not deny_texts:
        print(f"warning: no {DENY_FILE}; only {RELEASE_TODO} is checked for", file=sys.stderr)
    patterns = builtin_deny()
    seen = set()
    for text in deny_texts:
        for p in parse_deny(text):
            if p.name not in seen:
                seen.add(p.name)
                patterns.append(p)

    entries = head_tree(repo)
    kept = [e for e in entries if ignored_by(e.path, rules) is None]
    print(f"{len(entries)} files at HEAD, {len(entries) - len(kept)} left out by {IGNORE_FILE}, "
          f"{len(kept)} to export")

    blobs = read_blobs(repo, [e.sha for e in kept])
    hits = [h for e in kept for h in scan_file(e.path, blobs[e.sha], patterns)]
    if hits:
        for h in hits:
            print(f"{h.where}: matches {h.pattern!r}", file=sys.stderr)
        raise ExportError(f"{len(hits)} denylisted string(s) in {len({h.where.split(':')[0].split('!')[0] for h in hits})} "
                          "file(s); nothing written")
    return kept, blobs


def _stage(parent: Path, kept: list[Entry], blobs: dict[str, bytes]) -> Path:
    """The files written to a new directory under `parent` and scanned by gitleaks."""
    parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".export-", dir=parent))
    try:
        _write_tree(staging, kept, blobs)
        if binary := find_gitleaks():
            run_gitleaks(binary, staging)
        else:
            print("\n" + "!" * 72 + "\nWARNING: gitleaks is not on PATH, so the export was NOT scanned for"
                  "\nsecrets. Install it (https://github.com/gitleaks/gitleaks) and run again,"
                  "\nor scan the output yourself before pushing it anywhere.\n" + "!" * 72 + "\n",
                  file=sys.stderr)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _check_out(repo: Path, out: Path) -> None:
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise ExportError(f"{out} exists and is not an empty directory")
    if out == repo or repo in out.parents:
        raise ExportError(f"{out} is inside the repository; put it somewhere else")


def export(repo: Path, out: Path, *, author: str = DEFAULT_AUTHOR,
           message: str = DEFAULT_MESSAGE, allow_dirty: bool = False) -> str:
    """Returns the new commit's id. Raises ExportError, having written nothing at `out`."""
    name, email = parse_author(author)
    repo = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip())
    out = out.resolve()
    _check_out(repo, out)
    kept, blobs = _filtered_head(repo, allow_dirty)
    staging = _stage(out.parent, kept, blobs)
    try:
        if out.exists():
            out.rmdir()
        staging.rename(out)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    try:
        git(out, "init", "-q", "-b", "main")
        sha = _commit(out, name, email, message, len(kept))
        assert sha is not None
        return sha
    except BaseException:
        shutil.rmtree(out, ignore_errors=True)
        raise


# --- update: a new commit on top of the public repository ---------------------------


@dataclass(frozen=True)
class Update:
    clone: Path
    branch: str
    base: str               # the public repository's tip when it was cloned
    sha: str | None         # the new commit, or None when nothing changed


def default_update_message() -> str:
    return f"Update {datetime.datetime.now(datetime.UTC).date().isoformat()}"


def update(repo: Path, public: str, clone: Path, *, author: str = DEFAULT_AUTHOR,
           message: str | None = None, allow_dirty: bool = False) -> Update:
    """Clone `public` into `clone` and commit HEAD's export on top of its default branch.

    The clone's working tree is replaced entirely, so a file deleted or newly
    ignored here is deleted there. Nothing is pushed. Raises ExportError,
    having removed the clone, if any check fails.
    """
    name, email = parse_author(author)
    repo = Path(git(repo, "rev-parse", "--show-toplevel").decode().strip())
    clone = clone.resolve()
    _check_out(repo, clone)
    kept, blobs = _filtered_head(repo, allow_dirty)

    created = not clone.exists()
    clone.parent.mkdir(parents=True, exist_ok=True)
    staging = None
    try:
        print(f"Cloning {public} into {clone} ...")
        git(clone.parent, "clone", "-q", "--", public, str(clone))
        branch = git(clone, "symbolic-ref", "--short", "HEAD").decode().strip()
        try:
            base = git(clone, "rev-parse", "--verify", "HEAD^{commit}").decode().strip()
        except ExportError:
            raise ExportError(f"{public} has no commits on {branch}; create it with --out instead") from None

        staging = _stage(clone.parent, kept, blobs)
        for child in clone.iterdir():
            if child.name == ".git":
                continue
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
        for child in staging.iterdir():
            child.rename(clone / child.name)
        staging.rmdir()
        staging = None

        sha = _commit(clone, name, email, message or default_update_message(), len(kept))
    except BaseException:
        if staging is not None:
            shutil.rmtree(staging, ignore_errors=True)
        if created:
            shutil.rmtree(clone, ignore_errors=True)
        else:   # it was an empty directory: leave it empty
            shutil.rmtree(clone, ignore_errors=True)
            clone.mkdir(exist_ok=True)
        raise
    if sha is not None and git(clone, "rev-parse", "HEAD^").decode().strip() != base:
        raise ExportError(f"the new commit's parent is not {base}; not a fast-forward")
    return Update(clone, branch, base, sha)


def push(result: Update) -> None:
    """Push the new commit to origin as a fast-forward, if origin is still where it was."""
    clone, branch = result.clone, result.branch
    if result.sha is None:
        raise ExportError("nothing to push")
    if git(clone, "rev-parse", "HEAD").decode().strip() != result.sha:
        raise ExportError(f"{clone} has moved on from {result.sha}; push it yourself")
    git(clone, "merge-base", "--is-ancestor", result.base, result.sha)   # raises if not
    remote = git(clone, "ls-remote", "origin", f"refs/heads/{branch}").decode().split()
    now = remote[0] if remote else None
    if now != result.base:
        raise ExportError(f"origin's {branch} has moved since the clone ({result.base[:12]} -> "
                          f"{(now or 'deleted')[:12]}); not pushing. Run the update again.")
    print(f"Pushing {result.sha[:12]} to origin {branch} (fast-forward from {result.base[:12]}) ...")
    git(clone, "push", "-q", "origin", f"{result.sha}:refs/heads/{branch}")


NEXT_STEPS = """\
Exported to {out} as one commit, {sha}, on main, with no remote.

Nothing has been pushed. To publish it (none of this is run for you):

  1. Read it once more:        git -C {out} show --stat | less
  2. Create and push the repo: gh repo create jamesbaker1/<name> --public --source {out} --push
  3. Turn on private vulnerability reporting:
       gh api -X PUT repos/jamesbaker1/<name>/private-vulnerability-reporting
  4. Protect main (pull requests, required checks CI / Secret scan / CLA, no force pushes):
       Settings > Branches > Add branch ruleset, or `gh api repos/jamesbaker1/<name>/rulesets`
  5. Create the CLA signatures branch (unprotected), which cla.yml commits to:
       git -C {out} switch --orphan cla-signatures && git -C {out} commit --allow-empty -m "CLA signatures" \\
         && git -C {out} push -u origin cla-signatures && git -C {out} switch main

Later releases go on top of this commit, never into a new repository:
  python3 scripts/export_public.py --update https://github.com/jamesbaker1/<name>.git
"""

UPDATE_NEXT_STEPS = """\
Committed {sha} on {branch} in {clone}, on top of {base}.

Nothing has been pushed. Read it, then push it with a plain push (never --force):

  git -C {clone} show --stat | less
  git -C {clone} push origin {branch}

or run the update again with --push, which pushes only if the public
repository has not moved since it was cloned.
"""


def _show_stat(clone: Path) -> str:
    return git(clone, "-c", "color.ui=never", "show", "--stat",
               "--format=commit %H%nAuthor:    %an <%ae>%nCommitter: %cn <%ce>%n%n    %s%n",
               "HEAD").decode(errors="replace")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        usage="%(prog)s --out DIR [options]\n"
              "       %(prog)s --update PUBLIC [--out DIR] [--push] [options]")
    parser.add_argument("--out", type=Path,
                        help="create mode: where to create the new repository. With --update: where "
                             "to clone the public repository (default: a new temporary directory). "
                             "Must not exist, or be empty")
    parser.add_argument("--update", metavar="PUBLIC",
                        help="add one commit on top of this existing public repository (a URL or a "
                             "local path; it is cloned) instead of creating a new one")
    parser.add_argument("--push", action="store_true",
                        help="with --update: push the new commit to the public repository's default "
                             "branch, fast-forward only, refused if it has moved since the clone")
    parser.add_argument("--author", default=DEFAULT_AUTHOR,
                        help=f"'Name <email>' for the commit's author and committer (default: {DEFAULT_AUTHOR})")
    parser.add_argument("--message",
                        help=f"the commit message (default: {DEFAULT_MESSAGE!r}; with --update, "
                             "'Update YYYY-MM-DD')")
    parser.add_argument("--allow-dirty", action="store_true",
                        help="export HEAD even if tracked files have uncommitted changes")
    parser.add_argument("--repo", type=Path, default=Path.cwd(),
                        help="the repository to export (default: the current directory's)")
    args = parser.parse_args(argv)

    if args.update is None:
        if args.out is None:
            parser.error("--out DIR is required, or --update PUBLIC")
        if args.push:
            parser.error("--push only goes with --update")
        try:
            sha = export(args.repo, args.out, author=args.author,
                         message=args.message or DEFAULT_MESSAGE, allow_dirty=args.allow_dirty)
        except ExportError as e:
            print(f"export_public: {e}", file=sys.stderr)
            return 1
        print(NEXT_STEPS.format(out=args.out.resolve(), sha=sha))
        return 0

    temp = Path(tempfile.mkdtemp(prefix="public-update-")) if args.out is None else None
    clone = temp / "public" if temp is not None else args.out
    try:
        result = update(args.repo, args.update, clone, author=args.author, message=args.message,
                        allow_dirty=args.allow_dirty)
    except ExportError as e:
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        print(f"export_public: {e}", file=sys.stderr)
        return 1
    if result.sha is None:
        print(f"Nothing changed: {args.update} {result.branch} at {result.base[:12]} already "
              "matches the export of HEAD. No commit made.")
        if temp is not None:
            shutil.rmtree(temp, ignore_errors=True)
        return 0
    print(_show_stat(result.clone))
    if args.push:
        try:
            push(result)
        except ExportError as e:
            print(f"export_public: {e}\nThe commit is still in {result.clone}.", file=sys.stderr)
            return 1
        print(f"Pushed {result.sha} to {args.update} {result.branch}.")
        return 0
    print(UPDATE_NEXT_STEPS.format(sha=result.sha, branch=result.branch, clone=result.clone,
                                   base=result.base[:12]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
