"""Versioned releases a self-hosted firm deploys when it chooses.

Our own deployments go out on every push to main (deploy.yml). A firm that
runs Second Eye in its own Cloudflare account and its own Anthropic
organisation (`second-eye tenant new <firm> --self-hosted`) does not take that: its
IT deploys a release, a tag `vX.Y.Z`, when it has read it. A release is what
.github/workflows/release.yml builds from the tag, as a draft GitHub release:

  second-eye-<v>-source.tar.gz      the repository at the tag, less every
                                      firm's deployment (deployments/) and with
                                      Jim's names blanked from the shared
                                      config; a RELEASE file says which tag.
                                      It is also the container's build context:
                                      the firm's `wrangler deploy` builds the
                                      image from its Dockerfile, on the firm's
                                      machine, into the firm's account.
  second-eye-<v>-worker.tar.gz      the Worker exactly as wrangler bundles it
                                      (`wrangler deploy --dry-run --outdir`), to
                                      read; the firm's deploy bundles its own
                                      from the source and can compare.
  second-eye-<v>-wrangler.template.jsonc
                                      the config a firm's tenant renders to,
                                      with placeholders where its names go
  second-eye-<v>-requirements.txt   the Python dependencies as they resolved
                                      on our runner for that tag
  second-eye-<v>-sbom.cdx.json      a CycloneDX SBOM (syft, via
                                      anchore/sbom-action), when it built
  release.json                        what is in the release and what is not
  SHA256SUMS                          `sha256sum -c SHA256SUMS` checks the rest

The source archive is reproducible: the same tag gives the same bytes (file
order, times, owners and the gzip header are fixed), so a firm can rebuild
it with `python -m secondeye.release build` and compare checksums.

What this is not, said plainly in release.json: the checksums are not a
signature (they prove the files are the ones listed, not who listed them;
the tag and the GitHub release are the provenance), and the container image
is not shipped prebuilt, so its Debian and Python packages are whatever
resolve when the firm builds it.

Standard library only at the top, like tenant.py: release.yml runs it on a
bare runner.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

from secondeye import tenant as tenants

REPO = tenants.REPO
PRODUCT = "second-eye"
VERSION = re.compile(r"^v\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
# Never in a release: every firm's deployment (names, addresses, ids, and
# their .env if one were ever committed by mistake).
EXCLUDED = ("deployments/",)
SUMS = "SHA256SUMS"
RELEASE_FILE = "RELEASE"

NOT_INCLUDED = [
    ("a prebuilt container image: the firm's `wrangler deploy` builds it from the Dockerfile "
    "in the source archive, in the firm's account; its base image and Debian packages are "
    "whatever resolve at that moment"),
    ("a signature: SHA256SUMS proves the files are the ones listed, not who listed them; the "
    "provenance is the tag and the GitHub release it is attached to"),
    "any firm's deployment (deployments/), secrets, or DATA_KEY",
]


class ReleaseError(Exception):
    pass


def check_version(version: str) -> str:
    if not VERSION.match(version):
        raise ReleaseError(f"{version!r} is not a release version (vX.Y.Z)")
    return version


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                          check=True).stdout


def tracked(root: Path = REPO) -> list[str]:
    """The files in the release: what git tracks, less what never ships."""
    names = _git(root, "ls-files", "-z").split("\0")
    return sorted(n for n in names if n and not n.startswith(EXCLUDED)
                  and (root / n).is_file())


def sanitized_config(text: str) -> str:
    """cloudflare/wrangler.jsonc with Jim's names, ids and vars blanked.

    The shared parts (code, compatibility date, container class, cron) are
    what a firm's config is rendered from and stay. What is Jim's alone is
    blanked, so a release carries no other deployment's address, sender list,
    Worker URL or database id. The behaviour knobs keep their values: they
    are the defaults a new tenant starts from."""
    shared = tenants.parse_jsonc(text)
    for key in shared.get("vars", {}):
        if key in tenants.KNOBS:
            continue
        text = tenants.set_field(text, key, tenants.FIRM_DEFAULTS.get(key, ""))
    for key in ("database_id",):
        text = tenants.set_field(text, key, "")
    return text


def _info(name: str, size: int, mtime: int, mode: int = 0o644) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mtime, info.mode = size, mtime, mode
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def deterministic_tar(entries: list[tuple[str, bytes, int]], mtime: int) -> bytes:
    """A .tar.gz whose bytes depend only on the entries: sorted, fixed times,
    owners and gzip header."""
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name, data, mode in sorted(entries):
            tar.addfile(_info(name, len(data), mtime, mode), io.BytesIO(data))
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0, compresslevel=9) as gz:
        gz.write(raw.getvalue())
    return out.getvalue()


def source_archive(version: str, root: Path = REPO) -> tuple[bytes, dict]:
    """The source tree at HEAD as a release: (archive bytes, the RELEASE record)."""
    check_version(version)
    commit = _git(root, "rev-parse", "HEAD").strip()
    when = int(_git(root, "show", "-s", "--format=%ct", "HEAD").strip())
    prefix = f"{PRODUCT}-{version}/"
    entries: list[tuple[str, bytes, int]] = []
    for name in tracked(root):
        data = (root / name).read_bytes()
        if name == "cloudflare/wrangler.jsonc":
            data = sanitized_config(data.decode()).encode()
        mode = 0o755 if os.access(root / name, os.X_OK) else 0o644
        entries.append((prefix + name, data, mode))
    record = {"product": PRODUCT, "version": version, "commit": commit}
    entries.append((prefix + RELEASE_FILE, (json.dumps(record, indent=2) + "\n").encode(),
                    0o644))
    return deterministic_tar(entries, when), record


def template(root: Path = REPO) -> str:
    """The wrangler config a firm's tenant renders to, with placeholders for
    its names: what IT reads to see what will be created in its account."""
    shared = tenants.parse_jsonc((root / "cloudflare" / "wrangler.jsonc").read_text())
    vars_ = {k: (v if k in tenants.KNOBS else tenants.FIRM_DEFAULTS.get(k, ""))
             for k, v in shared.get("vars", {}).items()}
    for key in tenants.FIRM_EXTRAS:
        vars_.setdefault(key, "")
    vars_.update({"MAIL_AGENT_ADDRESS": "<review@legal.firm.com>", "FIRM_DOMAINS": "<firm.com>",
                  "ALLOWED_SENDERS": "<firm.com>", "EDGE_URL": "<https://...workers.dev>"})
    t = tenants.Tenant("FIRM", root / "deployments" / "FIRM", {
        "worker": "second-eye-<firm>", "account_id": "<firm's Cloudflare account id>",
        "instance_type": "standard-1", "d1_database_name": "second-eye-<firm>",
        "d1_database_id": "<printed by wrangler d1 create>",
        "r2_bucket": "second-eye-<firm>-docs", "jurisdiction": "",
        "workflow": "legal-review-flow-<firm>", "self_hosted": True, "vars": vars_,
    }, root)
    return ("// The config `second-eye tenant render <firm>` writes for a self-hosted firm, with\n"
            "// placeholders where the firm's names and ids go. For reading: the firm's\n"
            "// own is rendered from its deployments/<firm>/tenant.jsonc.\n"
            + json.dumps(tenants.render(t), indent=2) + "\n")


def build(version: str, out: Path, root: Path = REPO) -> list[Path]:
    """Everything a release is that can be made without node or Docker."""
    check_version(version)
    out.mkdir(parents=True, exist_ok=True)
    archive, record = source_archive(version, root)
    written = []
    path = out / f"{PRODUCT}-{version}-source.tar.gz"
    path.write_bytes(archive)
    written.append(path)
    path = out / f"{PRODUCT}-{version}-wrangler.template.jsonc"
    path.write_text(template(root))
    written.append(path)
    manifest = {
        **record,
        "deploy": f"second-eye tenant deploy <firm> --release {version} (docs/it/self-hosted.md)",
        "excluded_from_source": list(EXCLUDED),
        "not_included": NOT_INCLUDED,
    }
    path = out / "release.json"
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    written.append(path)
    return written


def pack(directory: Path, archive: Path) -> Path:
    """A directory (the Worker bundle) as a deterministic .tar.gz, and the
    directory removed, so SHA256SUMS lists the archive and not its parts."""
    entries = [(str(p.relative_to(directory.parent)), p.read_bytes(), 0o644)
               for p in sorted(directory.rglob("*")) if p.is_file()]
    archive.write_bytes(deterministic_tar(entries, 0))
    for p in sorted(directory.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()
    directory.rmdir()
    return archive


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def checksums(directory: Path) -> Path:
    """SHA256SUMS over every file in the directory, as `sha256sum -c` reads it."""
    lines = [f"{sha256(p)}  {p.name}\n" for p in sorted(directory.iterdir())
             if p.is_file() and p.name != SUMS]
    path = directory / SUMS
    path.write_text("".join(lines))
    return path


def verify(directory: Path) -> list[str]:
    """What does not match SHA256SUMS. Empty: every listed file is there and
    matches."""
    sums = directory / SUMS
    if not sums.exists():
        return [f"{sums} is missing"]
    found: list[str] = []
    for line in sums.read_text().splitlines():
        if not line.strip():
            continue
        digest, _, name = line.partition("  ")
        path = directory / name
        if not path.exists():
            found.append(f"{name}: missing")
        elif sha256(path) != digest:
            found.append(f"{name}: checksum does not match")
    return found


def this_release(root: Path = REPO) -> dict | None:
    """The RELEASE record of the tree this runs from: None for a git checkout."""
    path = root / RELEASE_FILE
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except ValueError:
        return None


# --------------------------------------------------------------------------
# second-eye tenant deploy <firm> --release vX.Y.Z
# --------------------------------------------------------------------------


def deploy_steps(t: tenants.Tenant) -> list[tenants.Step]:
    cfg = t.config_arg()
    edge = str(t.vars.get("EDGE_URL", ""))
    return [
        tenants.Step("The Worker's build tools, exactly as locked in the release",
                     ["npm", "ci"]),
        tenants.Step("The Worker, the Workflow and the container (built here from the "
                     "release's Dockerfile, pushed to your account's registry)",
                     ["npx", "wrangler", "deploy", "--config", cfg]),
        tenants.Step("The edge answers",
                     ["curl", "--fail", "--silent", "--show-error", f"{edge}/health"]
                     if edge else [], manual=not edge,
                     note="" if edge else "EDGE_URL is not set in tenant.jsonc yet: open "
                     "<the Worker URL wrangler printed>/health"),
    ]


def deploy(t: tenants.Tenant, version: str, *, assets: Path | None = None, apply: bool = False,
           runner: tenants.Runner = tenants.run_command,
           environ: dict[str, str] | None = None) -> int:
    """Deploy this release to a self-hosted firm's account, with the firm's
    own CLOUDFLARE_API_TOKEN. A dry run unless `apply`."""
    environ = os.environ if environ is None else environ
    check_version(version)
    if not t.self_hosted:
        print(f"{t.name} is hosted by us: deploy.yml deploys it on every push to main. "
              "`second-eye tenant deploy` is for a self-hosted firm.")
        return 2
    here = this_release(t.root)
    if here is None:
        print(f"This is not a release: there is no {RELEASE_FILE} file in {t.root}. Download "
              f"the {version} release, check it (`sha256sum -c {SUMS}`), unpack "
              f"{PRODUCT}-{version}-source.tar.gz, put deployments/{t.name}/ in it, and run "
              "this from there.")
        return 2
    if here.get("version") != version:
        print(f"This tree is release {here.get('version')}, not {version}. Unpack the "
              f"{version} source archive and run this from there.")
        return 2
    if assets is not None:
        bad = verify(assets)
        if bad:
            print("The release files do not match their checksums; do not deploy them:\n  "
                  + "\n  ".join(bad))
            return 1
        print(f"Checked {assets / SUMS}: every release file matches.")
    found = tenants.problems(t)
    if found:
        print("Fix the tenant first:\n  " + "\n  ".join(found))
        return 2
    if not t.provisioned:
        print(f"{t.name} has no D1 database yet: "
              f"`second-eye tenant provision {t.name} --apply` first.")
        return 2

    steps = deploy_steps(t)
    print(f"Deploying release {version} ({here.get('commit', '')[:12]}) to {t.name}: Worker "
          f"{t.worker} in Cloudflare account {t.account_id}.\n")
    if not apply:
        for i, step in enumerate(steps, 1):
            print(f"# {i}. {step.title}" + (" [by hand]" if step.manual else ""))
            if step.note:
                print(f"#    {step.note}")
            if step.argv:
                print(f"{_shell(t, step)}\n")
        print("Dry run: nothing was deployed. "
              f"`second-eye tenant deploy {t.name} --release {version} --apply` runs "
              "these with your CLOUDFLARE_API_TOKEN (a token for your account only; we never "
              "see it). Docker must be running: wrangler builds the container "
              "image here.")
        return 0
    if not environ.get("CLOUDFLARE_API_TOKEN"):
        print("CLOUDFLARE_API_TOKEN is not set: create one in your Cloudflare account "
              "(docs/it/self-hosted.md, 'The deploy token') and export it first.")
        return 2
    env = {"CLOUDFLARE_ACCOUNT_ID": t.account_id}
    for i, step in enumerate(steps, 1):
        if step.manual or not step.argv:
            continue
        print(f"# {i}. {step.title}\n$ {_shell(t, step)}")
        cwd = t.root if step.argv[0] == "curl" else t.root / "cloudflare"
        code, _ = runner(step.argv, cwd, env, None)
        if code != 0:
            print(f"\nStopped at step {i} (exit {code}). Nothing after it ran. The Worker "
                  "keeps the version it had if the deploy itself failed.")
            return 1
    print(f"\nDone: {t.name} runs {version}.")
    return 0


def _shell(t: tenants.Tenant, step: tenants.Step) -> str:
    if step.argv[0] == "curl":
        return " ".join(step.argv)
    return tenants.shell(t, step) if step.argv[0] == "npx" else f"(cd cloudflare && {' '.join(step.argv)})"


# --------------------------------------------------------------------------
# python -m secondeye.release build|pack|checksums|verify (release.yml)
# --------------------------------------------------------------------------


def main(argv: list[str], *, root: Path = REPO) -> int:
    parser = argparse.ArgumentParser(prog="python -m secondeye.release")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("build", help="the source archive, config template and release.json")
    p.add_argument("--version", required=True)
    p.add_argument("--out", required=True, type=Path)
    p = sub.add_parser("pack", help="a directory as a deterministic .tar.gz, then removed")
    p.add_argument("directory", type=Path)
    p.add_argument("archive", type=Path)
    p = sub.add_parser("checksums", help="write SHA256SUMS for a directory")
    p.add_argument("directory", type=Path)
    p = sub.add_parser("verify", help="check a directory against its SHA256SUMS")
    p.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.cmd == "build":
            for path in build(args.version, args.out, root):
                print(f"wrote {path}")
        elif args.cmd == "pack":
            print(f"wrote {pack(args.directory, args.archive)}")
        elif args.cmd == "checksums":
            print(checksums(args.directory).read_text(), end="")
        elif args.cmd == "verify":
            bad = verify(args.directory)
            print("\n".join(bad) if bad else "every file matches SHA256SUMS")
            return 1 if bad else 0
    except ReleaseError as e:
        print(e)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
