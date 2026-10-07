# The closing state

One JSON object. `scripts/validate_state.py` holds you to these rules, and the
host holds the update to them again before storing it.

```json
{
  "schema": "lra-closing/1",
  "name": "Project Falcon",
  "return_to": "Scan or photograph each signed page and email it to Jim Baker (jim@firm.com).",
  "documents": [
    {"id": "spa", "title": "Share Purchase Agreement", "short": "SPA",
     "file": "Falcon SPA v3.docx", "execution_file": "Falcon SPA v3 (execution).docx",
     "ref": "51D8B1", "date": "3 March 2026",
     "parties": ["ACME HOLDINGS LIMITED", "BETA TRADING LLC"], "pages_total": 45}
  ],
  "signatories": [
    {"id": "beta-director", "name": "Beta Trading's director", "party": "BETA TRADING LLC",
     "person": "Bob Jones", "capacity": "Director", "email": ""}
  ],
  "pages": [
    {"id": "spa--beta-director", "document": "spa", "signatory": "beta-director",
     "party": "BETA TRADING LLC", "page": 42, "ref": "51D8B1",
     "lines": ["Signed for and on behalf of BETA TRADING LLC", "Name: Bob Jones"],
     "status": "received",
     "received": {"file": "signed - spa--beta-director.pdf",
                  "source": "Beta signed pages.pdf, page 2",
                  "from": "jim@firm.com", "at": "2026-09-28T10:14:00Z",
                  "signed": true, "version_ref": "51D8B1",
                  "note": "Wet-ink signature, scanned."}}
  ],
  "rejected": [
    {"source": "IMG_0412.jpg", "reason": "Disclosure Letter for Beta Trading is unsigned.",
     "at": "2026-09-28T10:14:00Z"}
  ],
  "checklist": [
    {"id": "cp-antitrust", "kind": "condition", "item": "Merger clearance obtained",
     "source": "SPA cl. 4.1(a)", "responsible": "Buyer", "status": "open", "note": ""},
    {"id": "sign-spa", "kind": "signing", "item": "SPA signed by all parties",
     "source": "SPA", "responsible": "All", "status": "open",
     "pages": ["spa--acme-director", "spa--beta-director"]}
  ],
  "executed": null
}
```

## Rules

- Ids are unique within their list. A page's `document` and `signatory` name
  entries that exist. Page ids are `<document id>--<signatory id>`, as
  `packets.py` makes them.
- A page's `ref` is its document's current `ref`. `page` is a page number or
  `null` when it is not known.
- `status` is `awaiting` or `received`. A received page has a `received`
  record whose `file` you wrote with `receive.py`, a `source`, `signed: true`
  and a `version_ref` equal to the document's `ref`. An awaiting page has
  `received: null`.
- A page that has been received stays received, with its file, unless its
  document has a new `ref` (a new version).
- A page you will not accept goes in `rejected` with a `source` and a
  `reason`, never in `pages`.
- A `signing` checklist item with `pages` is `done` exactly when all of them
  are received; that is set for you. Other statuses: `open`, `done`,
  `waived`, `not applicable`.
- `executed` is `null` until every page is received; then
  `{"files": [...], "index": "<closing> - closing index.pdf", "at": "..."}`,
  naming files `compile.py` wrote.

## The update

```json
{"schema": "lra-closing-update/1", "state": {...}, "message": ["..."], "attach": ["..."]}
```

`message` is at most 12 short lines. `attach` names files in
`/mnt/session/outputs/` to send to the lawyer.
