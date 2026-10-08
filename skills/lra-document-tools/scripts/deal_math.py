"""Every amount and table in a document, for checking its arithmetic with
code. See reference/deal-math.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import fail, finish, read


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    args = parser.parse_args()

    from secondeye.models import Attachment
    from secondeye.pipeline import dealmath, extract

    content = read(args.input)
    try:
        doc = extract.extract(Attachment(
            filename=Path(args.input).name, content_type="application/octet-stream",
            size_bytes=len(content), content=content,
        ))
        payload = dealmath.survey(doc)
    except Exception as e:  # noqa: BLE001
        fail(f"could not read the figures in this file: {e}")
        return
    finish(payload)


if __name__ == "__main__":
    main()
