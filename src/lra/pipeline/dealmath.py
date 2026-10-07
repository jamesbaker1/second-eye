"""The document's arithmetic: every amount and every table, with where it is.

A purchase price in clause 3.1 that the completion statement in Schedule 2
does not add up to, instalments that do not sum to the price they pay,
sellers' proportions that come to 99 per cent: each is a figure contradicting
another figure, which is a blocker, and each is exactly the check a tired
reader skips because it means getting out a calculator.

Two uses of one reading:

  survey(doc)     every amount (money and percentages) with its clause label
                  and a few words of context, every table as a grid with its
                  numbers parsed, and the lists that divide a sum into parts.
                  JSON-ready. The review agent runs it through the
                  lra-document-tools skill (scripts/deal_math.py) and checks
                  with code whatever the document's arithmetic needs checking.
  arithmetic(doc) the few contradictions that are certain from the text alone,
                  as findings, for checks.run_all: the evidence the host
                  hands the model, and the whole review when there is no model.

Conservative throughout (PRODUCT.md: false positives are existential). A sum
is checked only where the document itself says what it should be: a row
labelled Total, a lead-in that says "in the following instalments" above
items that each carry one amount, a defined amount restated as "the Purchase
Price of £...". A table with a subtotal, a deduction or a mix of currencies
is read for the survey and never judged. Everything else is the model's call,
from the survey.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from lra.models import Finding, Severity
from lra.pipeline import checks
from lra.pipeline.extract import ExtractedDoc

CATEGORY = "arithmetic"

# --------------------------------------------------------------------------
# Reading an amount
# --------------------------------------------------------------------------

_CURRENCY = {"US$": "USD", "USD": "USD", "$": "USD", "£": "GBP", "GBP": "GBP",
             "€": "EUR", "EUR": "EUR"}
_SCALE = {"million": 1_000_000, "m": 1_000_000, "mn": 1_000_000, "bn": 1_000_000_000,
          "billion": 1_000_000_000, "k": 1_000, "thousand": 1_000}
_MONEY = re.compile(
    r"(?<![\w.,])(?P<cur>US\$|USD|GBP|EUR|[£$€])\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d+)?)"
    r"(?:\s?(?P<scale>million|billion|thousand|bn|mn|m|k)\b)?"
)
_PERCENT = re.compile(
    r"(?<![\w.,])(?P<num>\d{1,3}(?:\.\d+)?)\s?(?:%|per\s?cent\.?|percent\b)", re.IGNORECASE)
# A cell that is only a number: "4,200,000", "£ 4,200,000", "(150,000)", "25%".
_CELL_NUMBER = re.compile(
    r"^\s*(?P<neg>\(|-|–)?\s*(?P<cur>US\$|USD|GBP|EUR|[£$€])?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(?P<pct>%|per\s?cent\.?)?\s*\)?\s*$",
    re.IGNORECASE,
)
_DEFINES = re.compile(
    r"^\s*(?:\([^()]{0,80}\)\s*)?\(\s*(?:the\s+|each\s+a\s+|an?\s+)?[“\"]"
    r"(?P<term>[A-Z][A-Za-z\- ]{1,40})[”\"]\s*\)")
_TOTAL_LABEL = re.compile(r"^\s*(?:grand\s+|aggregate\s+)?(?:total|aggregate|sum)\b",
                          re.IGNORECASE)
# Rows that make a column something other than a plain sum of its parts.
_NOT_A_PLAIN_SUM = re.compile(
    r"\b(?:sub-?\s?total|less|deduct\w*|minus|net\b|plus|adjust\w*|balance|"
    r"cumulative|running|average|mean|rate|per\s+annum|p\.a\.)", re.IGNORECASE)
_THOUSANDS = re.compile(r"[’']000|000s\b|\(\s*[£$€]?\s*(?:m|bn|k)\s*\)|in\s+(?:thousands|millions)",
                        re.IGNORECASE)
_SUB_ITEM = re.compile(r"^\s*\(?(?:[a-z]{1,2}|[ivx]{1,5}|\d{1,2})\)\s*")
_LEAD_IN = re.compile(r"(?::\s*$|\bas\s+follows\b[^.]*$)", re.IGNORECASE)
_INSTALMENTS = re.compile(r"\b(?:instal?ments?|tranches?)\b", re.IGNORECASE)
_PROPORTIONS = re.compile(r"\bin\s+the\s+(?:following\s+)?proportions\b|"
                          r"\bproportions\s+(?:set\s+out\s+)?(?:below|as\s+follows)",
                          re.IGNORECASE)


@dataclass
class Amount:
    where: str
    block: int
    text: str                   # as written: "£12,500,000", "10%"
    value: float
    kind: str                   # money | percent
    currency: str = ""
    defines: str = ""           # the defined term it is given, if any
    context: str = ""
    cell: list[int] | None = None   # [table, row, column] when in a table


@dataclass
class Table:
    table: int
    where: str
    caption: str                # the nearest heading or line above it
    rows: list[list[str]]
    values: list[list[float | None]]
    columns: list[dict] = field(default_factory=list)


@dataclass
class Split:
    """A lead-in that divides a sum into listed parts."""
    where: str
    lead: str
    lead_amounts: list[dict]
    items: list[dict]           # {"where", "text", "amounts"}
    kind: str                   # instalments | proportions | list
    sum: float | None = None    # of the items' single amounts, where each has one


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _money_value(m: re.Match) -> float:
    value = _number(m.group("num"))
    if m.group("scale"):
        value *= _SCALE[m.group("scale").lower()]
    return value


def _cell_value(text: str) -> tuple[float, str, str] | None:
    """(value, kind, currency) for a cell that holds one number, else None."""
    if "\n" in text.strip():
        return None
    m = _CELL_NUMBER.match(text)
    if not m:
        return None
    value = _number(m.group("num"))
    if m.group("neg"):
        if m.group("neg") == "(" and not text.strip().endswith(")"):
            return None
        value = -value
    kind = "percent" if m.group("pct") else "money" if m.group("cur") else "number"
    return value, kind, _CURRENCY.get(m.group("cur") or "", "")


def _context(text: str, start: int, end: int, pad: int = 70) -> str:
    s, e = max(0, start - pad), min(len(text), end + pad)
    while s > 0 and not text[s - 1].isspace():
        s -= 1
    while e < len(text) and not text[e].isspace():
        e += 1
    return " ".join(text[s:e].split())


def _amounts_in(text: str) -> list[tuple[re.Match, str]]:
    """Money and percentages in reading order, skipping a percentage that is
    only the numeral half of "ten per cent. (10%)"."""
    found = [(m, "money") for m in _MONEY.finditer(text)]
    found += [(m, "percent") for m in _PERCENT.finditer(text)
              if not any(a.start() <= m.start() < a.end() for a, _ in found)]
    found.sort(key=lambda pair: pair[0].start())
    return found


def _dicts(items: list) -> list[dict]:
    return [asdict(i) for i in items]


# --------------------------------------------------------------------------
# The survey
# --------------------------------------------------------------------------


class _Reader:
    def __init__(self, doc: ExtractedDoc):
        self.doc = doc
        self.labels = checks._locations(doc)
        self.amounts: list[Amount] = []
        self.tables: list[Table] = []
        self.splits: list[Split] = []
        self._read_amounts()
        self._read_tables()
        self._read_splits()

    def where(self, index: int) -> str:
        return self.labels.get(index, "")

    def _read_amounts(self) -> None:
        for block in self.doc.blocks:
            text = block.text
            for m, kind in _amounts_in(text):
                if kind == "money":
                    value = _money_value(m)
                    currency = _CURRENCY[m.group("cur")]
                else:
                    value, currency = _number(m.group("num")), ""
                after = text[m.end():]
                # Past the words in brackets: "£2,500,000 (two million ...
                # pounds) (the “Consideration”)".
                defined = _DEFINES.match(after)
                self.amounts.append(Amount(
                    where=self.where(block.index), block=block.index,
                    text=m.group(0).strip(), value=value, kind=kind, currency=currency,
                    defines=defined.group("term").strip() if defined else "",
                    context=_context(text, m.start(), m.end()),
                    cell=list(block.cell) if block.cell else None,
                ))

    def _caption(self, first: int) -> str:
        for block in reversed(self.doc.blocks[max(0, first - 4):first]):
            if block.cell is None and block.text.strip():
                return " ".join(block.text.split())[:120]
        return ""

    def _read_tables(self) -> None:
        grids: dict[int, dict[tuple[int, int], str]] = {}
        firsts: dict[int, int] = {}
        for i, block in enumerate(self.doc.blocks):
            if block.cell is None:
                continue
            t, r, c = block.cell
            grids.setdefault(t, {})[(r, c)] = block.text
            firsts.setdefault(t, i)
        for t, grid in grids.items():
            n_rows = max(r for r, _ in grid) + 1
            n_cols = max(c for _, c in grid) + 1
            rows = [[grid.get((r, c), "") for c in range(n_cols)] for r in range(n_rows)]
            values = [[(v[0] if (v := _cell_value(cell)) else None) for cell in row]
                      for row in rows]
            first = firsts[t]
            table = Table(table=t, where=self.where(self.doc.blocks[first].index),
                          caption=self._caption(first), rows=rows, values=values)
            table.columns = [self._column(table, c) for c in range(n_cols)]
            self.tables.append(table)

    @staticmethod
    def _column(table: Table, c: int) -> dict:
        """What one column adds up to, and what its Total row says it does."""
        header = next((row[c] for row in table.rows if row[c].strip()
                       and _cell_value(row[c]) is None), "")
        kinds, currencies = set(), set()
        body: list[float] = []
        total: float | None = None
        total_row: int | None = None
        plain = True
        for r, row in enumerate(table.rows):
            label = " ".join(x for x in row[:c] if x.strip()) if c else ""
            parsed = _cell_value(row[c])
            if parsed is None:
                continue
            value, kind, currency = parsed
            kinds.add(kind)
            if currency:
                currencies.add(currency)
            if _TOTAL_LABEL.match(label):
                if total is None:
                    total, total_row = value, r
                else:
                    plain = False           # two totals: grouped, not one sum
                continue
            if total is not None:
                plain = False               # rows after the total
            if _NOT_A_PLAIN_SUM.search(label):
                plain = False
            body.append(value)
        if _NOT_A_PLAIN_SUM.search(header):
            plain = False                   # a column of rates or balances
        return {
            "column": c,
            "header": " ".join(header.split())[:80],
            "kind": ("percent" if kinds == {"percent"} else
                     "money" if "money" in kinds and "percent" not in kinds else
                     "number" if kinds == {"number"} else "mixed" if kinds else ""),
            "currencies": sorted(currencies),
            "body_rows": len(body),
            "body_sum": round(sum(body), 6) if body else None,
            "total": total,
            "total_row": total_row,
            "plain_sum": plain and len(currencies) <= 1,
        }

    def _read_splits(self) -> None:
        blocks = self.doc.blocks
        for i, block in enumerate(blocks):
            if block.cell is not None or not _LEAD_IN.search(block.text):
                continue
            items = []
            for later in blocks[i + 1:i + 16]:
                if later.cell is not None or not later.text.strip():
                    break
                if not (_SUB_ITEM.match(later.text) or _SUB_ITEM.match(later.number + " ")):
                    break
                items.append(later)
            if len(items) < 2:
                continue
            kind = ("instalments" if _INSTALMENTS.search(block.text) else
                    "proportions" if _PROPORTIONS.search(block.text) else "list")
            item_amounts = [[a for a in self.amounts if a.block == it.index] for it in items]
            singles = [a[0] for a in item_amounts if len(a) == 1]
            same = (len(singles) == len(items)
                    and len({(a.kind, a.currency) for a in singles}) == 1)
            self.splits.append(Split(
                where=self.where(block.index),
                lead=" ".join(block.text.split())[:240],
                lead_amounts=_dicts([a for a in self.amounts if a.block == block.index]),
                items=[{"where": self.where(it.index),
                        "text": " ".join(it.display.split())[:160],
                        "amounts": [a.text for a in am]}
                       for it, am in zip(items, item_amounts, strict=True)],
                kind=kind,
                sum=round(sum(a.value for a in singles), 6) if same else None,
            ))

    def defined(self) -> dict[str, list[Amount]]:
        """Each defined amount, and every place the document restates it."""
        out: dict[str, list[Amount]] = {}
        for a in self.amounts:
            if a.defines and a.kind == "money":
                out.setdefault(a.defines, []).append(a)
        for term in list(out):
            # "the Purchase Price of £X", "the Consideration (being £X)". Not
            # "part of the Consideration of £X", which is a part of it.
            said = re.compile(
                rf"(?<![\w-])(?P<pre>(?:[\w%.]+\s+){{0,2}})(?:the\s+){re.escape(term)}\s*"
                rf"(?:\(\s*being\s+|of\s+|,\s*being\s+|"
                rf"(?:is|shall\s+be|equal\s+to|amounting\s+to|in\s+the\s+(?:sum|amount)\s+of)\s+)$")
            for a in self.amounts:
                if a.kind != "money" or a.defines:
                    continue
                block = self.doc.blocks[a.block]
                at = block.text.find(a.text)
                m = said.search(block.text[max(0, at - 80):at]) if at > 0 else None
                if m and not re.search(r"\b(?:of|from|towards?|against|than|to|by|under|"
                                       r"part|portion|balance|instalment)\s*$",
                                       m.group("pre"), re.IGNORECASE):
                    out[term].append(a)
        return out


def survey(doc: ExtractedDoc) -> dict:
    """Everything the arithmetic needs, as plain data."""
    reader = _Reader(doc)
    return {
        "amounts": _dicts(reader.amounts),
        "tables": _dicts(reader.tables),
        "splits": _dicts(reader.splits),
        "defined_amounts": {term: [{"where": a.where, "text": a.text, "value": a.value}
                                   for a in said]
                            for term, said in reader.defined().items()},
        "findings": [as_json(f) for f in _findings(reader)],
    }


def as_json(f: Finding) -> dict:
    """A finding as the skill's scripts print it: the fields a review
    reports, with the severity as its word. Plain, so it works under the
    container's stand-in pydantic too."""
    return {
        "severity": getattr(f.severity, "value", f.severity),
        "category": f.category,
        "title": f.title,
        "explanation": f.explanation,
        "anchor": f.anchor,
        "where": f.where,
        "options": list(f.options),
        "question": f.question,
    }


# --------------------------------------------------------------------------
# What is certain
# --------------------------------------------------------------------------


def arithmetic(doc: ExtractedDoc) -> list[Finding]:
    """Sums the document states and contradicts, on any reading."""
    if getattr(doc, "transcribed", False):
        return []
    return _findings(_Reader(doc))


def _money(value: float, currency: str) -> str:
    symbol = {"GBP": "£", "USD": "$", "EUR": "€"}.get(currency, "")
    shown = f"{value:,.2f}".removesuffix(".00")
    return f"-{symbol}{shown[1:]}" if shown.startswith("-") else f"{symbol}{shown}"


def _shown(value: float, kind: str, currency: str) -> str:
    if kind == "percent":
        return f"{value:g}%"
    return _money(value, currency)


def _close(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance + 1e-9


def _findings(reader: _Reader) -> list[Finding]:
    out = checks._dedupe(_table_totals(reader) + _defined_restated(reader)
                         + _split_sums(reader))
    checks._label_locations(reader.doc, out)
    return out


def _tolerance(table: Table, column: dict) -> float:
    """Rounding the document itself admits to: a table in thousands or
    millions may be one unit out per row; percentages to two places."""
    text = " ".join([table.caption, column["header"]] + [" ".join(r) for r in table.rows[:2]])
    if _THOUSANDS.search(text):
        return float(column["body_rows"])
    if column["kind"] == "percent":
        return 0.01 * column["body_rows"]
    return 0.0


def _table_totals(reader: _Reader) -> list[Finding]:
    out = []
    defined = {term: said[0] for term, said in reader.defined().items()}
    for table in reader.tables:
        for column in table.columns:
            if column["total"] is None or column["kind"] in ("", "mixed"):
                continue
            row = table.rows[column["total_row"]]
            label = " ".join(x for x in row[:column["column"]] if x.strip())
            anchor = row[column["column"]].strip()
            currency = (column["currencies"] or [""])[0]
            total = column["total"]
            if column["plain_sum"] and column["body_rows"] >= 2 and not _close(
                    column["body_sum"], total, _tolerance(table, column)):
                body = column["body_sum"]
                kind = column["kind"]
                out.append(Finding(
                    severity=Severity.BLOCKER,
                    category=CATEGORY,
                    title=(f"{_where(table.where)}the {label.strip() or 'total'} is "
                           f"{_shown(total, kind, currency)}, but the rows above add up "
                           f"to {_shown(body, kind, currency)}"),
                    explanation=(
                        f"The {column['body_rows']} figures above the total come to "
                        f"{_shown(body, kind, currency)}, a difference of "
                        f"{_shown(abs(total - body), kind, currency)}. Either a figure "
                        "or the total is wrong; tell me which and I will correct it."
                    ),
                    anchor=anchor if _unique(reader, anchor) else "",
                    options=[_shown(total, kind, currency), _shown(body, kind, currency)],
                    question=f"Which is right: {_shown(total, kind, currency)} or "
                             f"{_shown(body, kind, currency)}?",
                    confidence=0.9,
                ))
                continue
            if column["kind"] != "money":
                continue
            # A total that says whose total it is: "Total Purchase Price", or a
            # table headed "Allocation of the Purchase Price".
            for term, stated in defined.items():
                named = re.search(rf"\b{re.escape(term)}\b", label) or re.search(
                    rf"\b(?:allocation|apportionment|breakdown|composition)\s+of\s+"
                    rf"(?:the\s+)?{re.escape(term)}\b", table.caption, re.IGNORECASE)
                if not named or stated.currency != (currency or stated.currency):
                    continue
                if _close(stated.value, total, _tolerance(table, column)):
                    continue
                out.append(Finding(
                    severity=Severity.BLOCKER,
                    category=CATEGORY,
                    title=(f"The {term} is {stated.text} in {stated.where or 'the agreement'}, "
                           f"but {_where(table.where, lower=True)}totals "
                           f"{_money(total, stated.currency)}"),
                    explanation=(
                        f"{_cap(stated.where) or 'The agreement'} sets the {term} at "
                        f"{stated.text}; the table {_in(table.where)}adds up to "
                        f"{_money(total, stated.currency)}, a difference of "
                        f"{_money(abs(stated.value - total), stated.currency)}. One of "
                        "them is wrong."
                    ),
                    anchor=anchor if _unique(reader, anchor) else stated.text,
                    options=[stated.text, _money(total, stated.currency)],
                    question=f"Is the {term} {stated.text} or "
                             f"{_money(total, stated.currency)}?",
                    confidence=0.85,
                ))
    return out


def _defined_restated(reader: _Reader) -> list[Finding]:
    out = []
    for term, said in reader.defined().items():
        first = said[0]
        for later in said[1:]:
            if later.currency != first.currency or _close(later.value, first.value, 0.0):
                continue
            block = reader.doc.blocks[later.block].text
            at = block.find(later.text)
            out.append(Finding(
                severity=Severity.BLOCKER,
                category=CATEGORY,
                title=(f"The {term} is {first.text} in {first.where or 'the agreement'} "
                       f"and {later.text} in {later.where or 'another clause'}"),
                explanation=(
                    f"The {term} is defined as {first.text} and restated as "
                    f"{later.text}. Which one governs is now a dispute; tell me which "
                    "is right and I will make the other match."
                ),
                anchor=checks._anchor(block, at, at + len(later.text), pad=15),
                options=[first.text, later.text],
                question=f"Is the {term} {first.text} or {later.text}?",
                confidence=0.9,
            ))
    return out


def _split_sums(reader: _Reader) -> list[Finding]:
    """Instalments that do not add up to the sum they pay; proportions that
    do not add up to the whole."""
    out = []
    for split in reader.splits:
        if split.sum is None or split.kind == "list":
            continue
        items = [a for a in reader.amounts if any(
            a.where == it["where"] and a.text in it["amounts"] for it in split.items)]
        if not items:
            continue
        kind, currency = items[0].kind, items[0].currency
        if split.kind == "proportions":
            if kind != "percent" or _close(split.sum, 100.0, 0.01 * len(split.items)):
                continue
            expected, expected_text = 100.0, "100%"
        else:
            leads = [(a["value"], a["text"]) for a in split.lead_amounts
                     if a["kind"] == kind and a["currency"] == currency]
            if not split.lead_amounts:
                # "The Purchase Price shall be paid in three instalments:"
                leads = [(said[0].value, said[0].text)
                         for term, said in reader.defined().items()
                         if said[0].currency == currency
                         and re.search(rf"\b{re.escape(term)}\b", split.lead)]
            if kind != "money" or len(leads) != 1:
                continue
            expected, expected_text = leads[0]
            if _close(split.sum, expected, 0.0):
                continue
        got = _shown(split.sum, kind, currency)
        out.append(Finding(
            severity=Severity.BLOCKER,
            category=CATEGORY,
            title=(f"{_where(split.where)}the {split.kind} add up to {got}, "
                   f"not {expected_text}"),
            explanation=(
                f"The {len(split.items)} {split.kind} listed come to {got}, where the "
                f"clause says they make up {expected_text}, a difference of "
                f"{_shown(abs(expected - split.sum), kind, currency)}. One of the "
                "figures is wrong."
            ),
            anchor=_lead_anchor(reader, split),
            question=f"Which figure in {split.where or 'the list'} is wrong?",
            confidence=0.85,
        ))
    return out


def _lead_anchor(reader: _Reader, split: Split) -> str:
    for block in reader.doc.blocks:
        if " ".join(block.text.split())[:240] == split.lead:
            text = block.text
            m = _INSTALMENTS.search(text) or _PROPORTIONS.search(text)
            return checks._anchor(text, m.start(), m.end()) if m else text[:80]
    return ""


def _unique(reader: _Reader, anchor: str) -> bool:
    return bool(anchor) and sum(b.text.count(anchor) for b in reader.doc.blocks) == 1


def _cap(where: str) -> str:
    return where[:1].upper() + where[1:]


def _where(where: str, lower: bool = False) -> str:
    """"Schedule 2: " to lead a title, or "" when the table is unplaced."""
    if not where:
        return "" if not lower else "the table "
    return f"{where} " if lower else f"{_cap(where)}: "


def _in(where: str) -> str:
    return f"in {where} " if where else ""
