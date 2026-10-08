"""Read documents for signing: title, date, version ref, blockers, parties
and each party's signature-block lines. See SKILL.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import fail, finish, read


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("documents", nargs="+")
    args = parser.parse_args()

    from secondeye.pipeline import closing_docs

    out = []
    for path in args.documents:
        try:
            out.append(closing_docs.survey(read(path), Path(path).name))
        except closing_docs.ClosingError as e:
            out.append({"file": Path(path).name, "error": str(e)})
        except Exception as e:  # noqa: BLE001
            fail(f"could not read {path}: {e}")
    finish({"documents": out})


if __name__ == "__main__":
    main()
