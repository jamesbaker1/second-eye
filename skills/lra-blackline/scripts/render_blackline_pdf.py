"""Write the blackline: the Word comparison and its PDF, and the record the
host reads. See SKILL.md.

    python scripts/render_blackline_pdf.py --versions /workspace/blackline-versions.json \
        --significance significance.json --outdir /mnt/session/outputs

`significance.json` is yours:

    {"earlier": "V1", "later": "V4",
     "why": "You said 'the original'; V1 is the first version on this thread.",
     "material": [{"index": 3, "significance": "Doubles the time they have to pay."}]}

The comparison is made again here from the two files, so the PDF, the Word
file and the counts all come from one run of the same code. The PDF is read
back before anything is written: if it does not carry every change, or the
Word file does not prove, nothing is written and the reason is printed.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from _bootstrap import fail, finish, read, write

SCHEMA = "lra-blackline/1"
RECORD = "blackline.json"
MAX_MATERIAL = 20
MAX_LINE = 240


def _entry(listing: dict, version_id: str) -> dict | None:
    return next((v for v in listing.get("versions", []) if v.get("id") == version_id), None)


def _safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", name).strip() or "document"


def names(later: dict, earlier: dict) -> tuple[str, str]:
    stem = (later.get("filename") or "document").rsplit(".", 1)[0]
    tag = earlier.get("label") or (earlier.get("filename") or "earlier").rsplit(".", 1)[0]
    base = _safe(f"{stem} (blackline against {tag})")[:150]
    return f"{base}.docx", f"{base}.pdf"


def problems_with(decision: dict, listing: dict, count: int) -> list[str]:
    out: list[str] = []
    for key in ("earlier", "later"):
        if not isinstance(decision.get(key), str) or _entry(listing, decision[key]) is None:
            out.append(f'"{key}" must be the id of a version in the versions file')
    if decision.get("earlier") == decision.get("later"):
        out.append('"earlier" and "later" are the same version')
    material = decision.get("material", [])
    if not isinstance(material, list):
        return out + ['"material" must be a list']
    if len(material) > MAX_MATERIAL:
        out.append(f"at most {MAX_MATERIAL} material changes; pick the ones that matter most")
    seen: set[int] = set()
    for n, item in enumerate(material):
        index = item.get("index") if isinstance(item, dict) else None
        line = item.get("significance") if isinstance(item, dict) else None
        if not isinstance(index, int) or not 0 <= index < count:
            out.append(f"material[{n}]: index must be a change index from summary.py "
                       f"(0 to {count - 1})")
            continue
        if index in seen:
            out.append(f"material[{n}]: change {index} is listed twice")
        seen.add(index)
        if not isinstance(line, str) or not line.strip():
            out.append(f"material[{n}]: significance is empty")
        elif len(line) > MAX_LINE or "\n" in line.strip():
            out.append(f"material[{n}]: significance must be one line of at most "
                       f"{MAX_LINE} characters")
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--versions", required=True)
    parser.add_argument("--significance", required=True)
    parser.add_argument("--outdir", default="/mnt/session/outputs")
    parser.add_argument("--engine", default="auto", choices=("auto", "typeset", "libreoffice"))
    args = parser.parse_args()

    from secondeye.pipeline import blackline_pdf, compare

    try:
        listing = json.loads(read(args.versions))
        decision = json.loads(read(args.significance))
    except ValueError as e:
        fail(f"not JSON: {e}")
        return
    if not isinstance(decision, dict):
        fail("the significance file must be a JSON object")
        return
    for key in ("earlier", "later"):
        if _entry(listing, str(decision.get(key))) is None:
            fail(f'"{key}" must be the id of a version in the versions file')
            return
    earlier, later = _entry(listing, decision["earlier"]), _entry(listing, decision["later"])

    try:
        result = compare.compare(read(earlier["file"]), read(later["file"]))
    except Exception as e:  # noqa: BLE001 - reported, never worked around
        fail(f"could not compare these files: {e}")
        return
    problems = problems_with(decision, listing, len(result.changes))
    if problems:
        fail("; ".join(problems))
        return

    summary = blackline_pdf.summarise(result)
    record = {"schema": SCHEMA, "earlier": earlier["id"], "later": later["id"],
              "why": str(decision.get("why", "")).strip(), "identical": result.identical,
              "counts": summary["counts"], "sentence": summary["sentence"],
              "proven": result.proven, "notes": result.notes}
    outdir = Path(args.outdir)
    if result.identical:
        write(str(outdir / RECORD), json.dumps({**record, "material": []}, indent=1).encode())
        finish({**record, "message": "the two versions say the same thing; nothing to render"})
        return
    if result.content is None or result.proven is False:
        fail("no Word comparison that proves: " + " ".join(result.notes[-1:]))
        return

    listed = {c["index"]: c for c in summary["changes"]}
    material = []
    for item in decision.get("material", []):
        change = listed[item["index"]]
        material.append({"index": item["index"], "clause": change["clause"],
                         "kind": change["kind"], "before": change["before"],
                         "after": change["after"],
                         "significance": " ".join(item["significance"].split())})
    labels = blackline_pdf.Labels(earlier["filename"], later["filename"],
                                  earlier.get("detail", ""), later.get("detail", ""))
    rendered = blackline_pdf.render(
        result.content, result.changes, labels,
        [blackline_pdf.Material(m["clause"], m["before"], m["after"], m["significance"],
                                m["kind"]) for m in material],
        engine=args.engine)
    checked = blackline_pdf.check(rendered.content, result.changes, labels, summary["sentence"])
    if checked:
        fail("the PDF did not pass its own check: " + "; ".join(checked))
        return

    docx_name, pdf_name = names(later, earlier)
    write(str(outdir / docx_name), result.content)
    write(str(outdir / pdf_name), rendered.content)
    record.update({"docx": docx_name, "pdf": pdf_name, "engine": rendered.engine,
                   "pages": rendered.pages, "material": material})
    write(str(outdir / RECORD), json.dumps(record, indent=1, ensure_ascii=False).encode())
    finish(record)


if __name__ == "__main__":
    main()
