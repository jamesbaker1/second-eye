"""Run the deterministic checks over a Word document. See SKILL.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import fail, finish, read


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    args = parser.parse_args()

    from secondeye.models import Attachment
    from secondeye.pipeline import checks, extract

    content = read(args.input)
    try:
        doc = extract.extract(Attachment(
            filename=Path(args.input).name, content_type="application/octet-stream",
            size_bytes=len(content), content=content,
        ))
        findings = checks.run_all(doc, content)
    except Exception as e:  # noqa: BLE001
        fail(f"could not check this file: {e}")
        return

    finish({"findings": [
        {
            "severity": getattr(f.severity, "value", f.severity),
            "category": f.category,
            "title": f.title,
            "explanation": f.explanation,
            "anchor": f.anchor,
            "suggested_text": f.suggested_text,
        }
        for f in findings
    ]})


if __name__ == "__main__":
    main()
