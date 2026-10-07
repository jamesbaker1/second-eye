"""Who the parties are, and every place that names them by role: signature
blocks, notice details, schedules. With --previous, what the parties clause
renamed since that version and where the old name survives. See
reference/deal-math.md."""

from __future__ import annotations

import argparse
from pathlib import Path

from _bootstrap import fail, finish, read


def _doc(path: str):
    from lra.models import Attachment
    from lra.pipeline import extract

    content = read(path)
    return extract.extract(Attachment(
        filename=Path(path).name, content_type="application/octet-stream",
        size_bytes=len(content), content=content,
    ))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input")
    parser.add_argument("--previous", default="",
                        help="the earlier version, to see what the parties clause renamed")
    args = parser.parse_args()

    from lra.pipeline import checks, dealmath

    try:
        doc = _doc(args.input)
        parties = checks._party_roles(doc)
        labels = checks._locations(doc)
        payload = {
            "parties": [{"name": p.name, "role": p.role} for p in parties],
            "signature_blocks": [
                {"entity": b.entity, "fields": b.fields,
                 "text": [doc.blocks[i].text for i in b.positions]}
                for b in checks._signature_blocks(doc)],
            "named_by_role": [
                {"where": labels.get(i, ""), "written": written, "role": p.role}
                for i, written, p in checks._labelled_mentions(doc, parties)],
            "findings": [dealmath.as_json(f) for f in checks.party_drift(doc)],
        }
        if args.previous:
            earlier = _doc(args.previous)
            payload["previous_parties"] = [{"name": p.name, "role": p.role}
                                           for p in checks._party_roles(earlier)]
            payload["since_previous"] = [
                dealmath.as_json(f) for f in checks.party_drift_since(
                    earlier, doc, f"since {Path(args.previous).name}")]
    except Exception as e:  # noqa: BLE001
        fail(f"could not read the parties in this file: {e}")
        return
    finish(payload)


if __name__ == "__main__":
    main()
