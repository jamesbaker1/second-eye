"""Build every Project Falcon document and every email, deterministically.

    python -m demos.falcon.build [--out demos/falcon/build]

Writes the documents in one folder per beat, the signed pages the story
returns at completion, and one .eml per step of emails.yaml under emails/.
Nothing touches the network, and two runs write identical bytes.

A step that replies to an earlier one is written without threading headers:
in a mail client you send it by pressing Reply on the agent's reply, and the
rehearsal threads it itself. `python -m demos.falcon.send <step>
--in-reply-to <id>` writes one with them.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from demos import identity
from demos.falcon import cast, documents, mail, signing

HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "build"

# key -> (path under the build folder, builder). The keys are what
# emails.yaml attaches.
FILES: dict[str, tuple[str, Callable[[], bytes]]] = {
    "memo": ("1 Playbook/Carrow Lisle - buy-side positions memo.docx",
             documents.positions_memo),
    "heron": ("1 Playbook/Project Heron - SPA (executed).docx", documents.heron_precedent),
    "kestrel": ("1 Playbook/Project Kestrel - SPA (executed).docx",
                documents.kestrel_precedent),
    "spa_v1": ("2 Their paper/Project Falcon - SPA v1 (Harrowgate).docx",
               lambda: documents.spa("v1")),
    "response_commented": ("3 Comments/Project Falcon - SPA (our response, JP comments).docx",
                           documents.our_response),
    "spa_v2": ("4 v2/Project Falcon - SPA v2 (Harrowgate).docx", lambda: documents.spa("v2")),
    "spa_v2_jp": ("6 The save/Project Falcon - SPA v2 (Harrowgate) - JP notes.docx",
                  documents.v2_with_partner_comment),
    "spa_v3": (("6 The save/Project Falcon - SPA v3 (Carrow Lisle) - the file that should "
               "have gone.docx"), documents.v3),
    "spa_agreed": ("7 Closing/Project Falcon - SPA (agreed form).docx",
                   lambda: documents.spa("agreed")),
    "spa_execution": (f"7 Closing/{signing.SPA}", lambda: documents.spa("execution")),
    "disclosure_letter": (f"7 Closing/{signing.LETTER}", documents.disclosure_letter),
    "board_resolution": (f"7 Closing/{signing.BOARD}", documents.board_minutes),
    "beta_nda": ("8 Encore/Beta Freight - NDA (Harrowgate).docx", documents.beta_nda),
}
SIGNED = {
    "signed_beta": "7 Closing/signed/Beta signed pages.pdf",
    "signed_northwind": "7 Closing/signed/Northwind signed pages.pdf",
}


def build_files() -> dict[str, tuple[str, bytes]]:
    """Every attachment the story uses, by key: (relative path, bytes)."""
    out = {key: (path, make()) for key, (path, make) in FILES.items()}
    closing_docs = {signing.SPA: out["spa_execution"][1],
                    signing.LETTER: out["disclosure_letter"][1],
                    signing.BOARD: out["board_resolution"][1]}
    with tempfile.TemporaryDirectory() as tmp:
        packets = signing.build_packets(closing_docs, Path(tmp))
    signed = signing.signed_sets(packets)
    out["signed_beta"] = (SIGNED["signed_beta"], signed["beta"])
    out["signed_northwind"] = (SIGNED["signed_northwind"], signed["northwind"])
    return out


def eml_name(step: dict) -> str:
    return f"{step['id']} - {step['title']}.eml".replace("/", "-").replace(":", "")


def build(out: Path = DEFAULT_OUT) -> list[Path]:
    files = build_files()
    written: list[Path] = []
    for path, data in files.values():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        written.append(target)
    emails = out / "emails"
    emails.mkdir(parents=True, exist_ok=True)
    for step in mail.steps():
        target = emails / eml_name(step)
        target.write_bytes(mail.build_message(step, files))
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--example", action="store_true",
                        help="build with the invented addresses (demos/identity.py), to look at")
    args = parser.parse_args(argv)
    if not args.example:
        identity.require(cast.IDENTITY, "python -m demos.falcon.build")
    written = build(args.out)
    print(f"{len(written)} files in {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
