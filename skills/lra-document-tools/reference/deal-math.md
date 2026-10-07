# Checking a deal's figures, timings and party names

Three scripts for the reading a tired eye does worst: adding up, counting
days, and noticing that a name changed in one place and not another. Each
prints JSON and does the arithmetic; you decide what is wrong. Run them on
the document in `/workspace` (they never change it).

Findings you make from these use category `arithmetic`, `timing` or
`party-name`, name both figures (or both dates, or both names) and both
clause labels, and quote one of them verbatim as the `anchor`. Nothing here
is ever `auto_apply`: which figure is right is the lawyer's call. Ask it as
a `question` with both values in `options`.

Anything the scripts put under `findings` is already in the mechanical
findings you were handed. Do not report it again.

## The arithmetic

```bash
python scripts/deal_math.py INPUT.docx
```

- `amounts`: every sum of money and percentage, with `where` (the clause
  label, as the reply shows it), `value` as a number, `currency`, `defines`
  (the defined term it is given, such as `Purchase Price`), `context` and,
  for a table cell, `cell` as `[table, row, column]`.
- `tables`: each table as `rows` (text) and `values` (numbers or null), with
  its `where` and `caption` (the line above it), and per column the sum of
  the body rows, the Total row's figure and `plain_sum` (false when a
  subtotal, a deduction, an adjustment or rows after the total make a plain
  sum the wrong test).
- `splits`: a lead-in that divides something into listed parts ("paid in
  instalments as follows:") with each item's amounts and their `sum`.
- `defined_amounts`: each defined sum and every place it is restated.

Check with code, not by eye:

- a schedule's figures against the clause that states the total ("the
  Purchase Price in 3.1 is £12,500,000; Schedule 2 adds up to £12,050,000"),
  including a schedule with no Total row, where the survey cannot say which
  clause it should match and you can;
- percentages that should come to 100 (shareholdings, allocations, the
  split of a price between sellers);
- instalments, tranches or milestones against the sum they pay;
- the same figure in words and numerals in different clauses, and a figure
  restated in a later clause or a schedule.

Mind what is not an error: a table in thousands rounds; a cap expressed as
a percentage of the price is not a part of it; a deferred or contingent
amount may sit outside the headline price by design. When the document
explains a difference, it is not one.

## The timeline

```bash
python scripts/timeline.py INPUT.docx
```

- `dated`: every deadline with a date, earliest first, as ISO dates.
- `relative`: every period that runs from an event (`from`: "Completion"),
  with `runs` (before or after) and `days` as `[shortest, longest]` in
  calendar days on any reading (a month is 28 to 31 days).
- `flags`: orderings to judge, each with `kind`, `title`, `detail`,
  `where` and an `anchor`:
  `claims-before-release` (claims close before the retention or escrow they
  are secured on is released), `notice-longer-than-term`,
  `long-stop-before-signing`, `long-stop-before-date`,
  `date-before-signing` and `cure-longer-than-notice`.

A flag is a question, not a verdict. Read both clauses. Report it (category
`timing`, usually `substantive`; `blocker` only when a date is impossible,
such as a long stop before signing) when the ordering defeats what the
clauses are for; say nothing when the deal plainly intends it. Anchor on the
text in the flag's `anchor`.

## The parties

```bash
python scripts/parties.py INPUT.docx [--previous EARLIER.docx]
```

- `parties`: each party the parties clause names, with its defined role.
- `signature_blocks`, and `named_by_role`: the notice details, schedules and
  restatements that name a company as the Buyer, the Seller and so on.
- `findings`: a party named one way in the parties clause and another in its
  own signature block, notice details or a schedule. A blocker, and already
  in your mechanical findings.
- With `--previous`, `since_previous`: a party renamed in the parties clause
  since the earlier version whose old name survives somewhere in this one.
  Use it when the session gives you an earlier version.

Look beyond what the script ties together: a recital, a definition or a
schedule that names the old entity in prose, or a guarantor who is the
renamed party's parent (which may be right).
