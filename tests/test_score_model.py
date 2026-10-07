"""The model scorecard: its corpus, its scorer, and its runner.

Nothing model-backed has run live yet, so the first live run has to produce a
scorecard rather than impressions (evals/score_model.py). These tests are what
make that scorecard worth reading: the corpus is silent to the deterministic
checks, so every finding on it is the model's; canned right answers meet every
target and canned wrong ones miss each target they were built to miss; saved
results score the same when replayed; and the live runner goes through the
real review path, within its budget, stopping when the account cannot pay.
"""

from __future__ import annotations

import json
import sys

import pytest

from evals import model_corpus, score_model
from evals.model_corpus import ALL, OFF, PLAYBOOK, SILENCE, THEIR_PAPER
from evals.score_model import Produced, stub
from lra.models import Attachment, Finding, Mode, ReviewResult, Severity
from lra.pipeline import checks, extract

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _card(quality: str) -> dict:
    return score_model.score(ALL, {s.name: stub(s, quality) for s in ALL})


@pytest.fixture(scope="module")
def perfect() -> dict:
    return _card("perfect")


@pytest.fixture(scope="module")
def bad() -> dict:
    return _card("bad")


# --- the corpus ---------------------------------------------------------------


@pytest.mark.parametrize("spec", ALL, ids=lambda s: s.name)
def test_the_mechanical_checks_are_silent_on_every_corpus_document(spec):
    """Any finding on this corpus must be the model's, or the scorecard is
    scoring the deterministic layer twice."""
    content = spec.build()
    doc = extract.extract(Attachment(filename=spec.filename, content_type=DOCX,
                                     size_bytes=len(content), content=content))
    found = [f for f in checks.run_all(doc, content) if f.category != "leftovers"]
    assert found == [], [f.title for f in found]


def test_the_corpus_has_the_sets_it_promises():
    kinds = [s.kind for s in ALL]
    assert kinds.count("playbook") == 8 and kinds.count("silence") == 5
    assert kinds.count("their_paper") == 3 and kinds.count("key_terms") == 3
    assert kinds.count("realistic") == 3
    # Every one of the twelve starter positions is planted at least once.
    planted = {p.clause for s in PLAYBOOK for p in s.planted}
    assert planted == set(model_corpus.CLAUSES)
    assert len({s.name for s in ALL}) == len(ALL)


def test_every_planted_clause_is_in_its_document_and_off_the_on_position_text():
    on = "\n".join(SILENCE[0].paragraphs())
    for spec in PLAYBOOK + THEIR_PAPER:
        text = "\n".join(spec.paragraphs())
        for planted in spec.planted_with_text():
            anchor = score_model._planted_anchor(planted)
            assert text.count(anchor) == 1, (spec.name, anchor)
            assert anchor not in on


def test_their_drafts_carry_their_typos_once_each_and_outside_the_planted_clauses():
    for spec in THEIR_PAPER:
        text = "\n".join(spec.paragraphs())
        planted = " ".join(p.text for p in spec.planted_with_text())
        for wrong, _ in spec.typos:
            assert text.count(wrong) == 1, (spec.name, wrong)
            assert wrong not in planted


def test_new_york_law_is_not_planted_because_the_playbook_accepts_it():
    """The starter playbook's fallback names New York; a clean silence about it
    must not be scored as a miss."""
    text = (score_model.ROOT / "skills/lra-playbook/positions/"
            "governing-law-and-disputes.md").read_text()
    assert "State of New York" in text.split("## Walk-away")[0]
    law = OFF[("governing-law-and-disputes", "supplier's home law and courts")]
    assert "Texas" in law[0] and "New York" not in " ".join(law)


# --- the scorer, on canned answers --------------------------------------------


def test_a_perfect_review_meets_every_target(perfect):
    m = perfect["metrics"]
    assert perfect["all_met"], score_model.render(perfect)
    assert m["playbook_recall"] == 1.0
    assert m["fp_per_on_position_doc"] == 0 and m["fp_per_realistic_doc"] == 0
    assert m["their_paper_edits_in_their_text"] == 0
    assert m["their_paper_positions_missing"] == 0
    assert m["key_terms_accuracy"] == 1.0
    assert m["voice_violations"] == 0 and m["errors"] == 0
    assert perfect["counts"]["planted_positions"] == 12
    assert perfect["counts"]["their_paper_positions"] == 8
    assert perfect["counts"]["key_term_fields"] == 18


def test_a_bad_review_misses_every_target_it_was_built_to_miss(bad):
    m, met = bad["metrics"], bad["met"]
    assert not bad["all_met"]
    assert m["playbook_recall"] == pytest.approx(8 / 12, abs=0.001)
    assert m["fp_per_on_position_doc"] == 1.0 and m["fp_per_realistic_doc"] == 1.0
    # One typo per draft fixed as if it were ours: a deletion and an insertion.
    assert m["their_paper_edits_in_their_text"] == 6
    assert m["their_paper_positions_missing"] == 3
    assert m["key_terms_accuracy"] == 0.5
    for key in ("playbook_recall", "fp_per_on_position_doc", "fp_per_realistic_doc",
                "their_paper_edits_in_their_text", "their_paper_positions_missing",
                "key_terms_accuracy", "voice_violations"):
        assert met[key] is False, key
    rules = set(bad["voice_by_rule"])
    assert {"summary longer than one sentence", "summary says whether to send",
            "system word in the summary", "title does not lead with the clause",
            "system word in the explanation",
            "starter finding not labelled as the starter playbook"} <= rules


def test_the_bad_scorecard_names_what_went_wrong(bad):
    text = score_model.render(bad)
    assert "does not meet target" in text
    assert "- pb_one_way_indemnity: Indemnity" in text
    assert "deleted 'recieving' in their text (a planted typo)" in text
    assert "Governing law and disputes is not in the issues list" in text
    assert "governing_law wrong, notice missing" in text


def test_saved_results_score_the_same_when_replayed(tmp_path):
    produced = {s.name: stub(s, "bad") for s in ALL}
    for p in produced.values():
        p.save(tmp_path / "results")
    replayed = score_model.load_dir(tmp_path / "results")
    assert score_model.score(ALL, replayed) == score_model.score(ALL, produced)
    assert score_model.main(["--replay", str(tmp_path), "--out", str(tmp_path / "card")]) == 1
    assert (tmp_path / "card" / "scorecard.json").exists()


def test_a_document_not_run_is_listed_and_not_scored():
    produced = {s.name: stub(s, "perfect") for s in ALL}
    produced["pb_texas_law"] = Produced(name="pb_texas_law", not_run="the budget")
    card = score_model.score(ALL, produced)
    assert card["counts"]["not_run"] == 1 and card["all_met"]
    assert card["counts"]["planted_positions"] == 11
    assert "pb_texas_law | playbook | not run (the budget)" in score_model.render(card)


def test_a_failed_review_counts_its_planted_clauses_as_missed():
    produced = {s.name: stub(s, "perfect") for s in ALL}
    produced["pb_three_commercial"] = Produced(name="pb_three_commercial",
                                               error="RuntimeError: out of time")
    card = score_model.score(ALL, produced)
    assert card["metrics"]["errors"] == 1
    assert card["metrics"]["playbook_recall"] == pytest.approx(9 / 12, abs=0.001)


# --- the scorer's rules, one at a time ---------------------------------------


def _finding(**kw) -> Finding:
    base = {"severity": Severity.SUBSTANTIVE, "category": "playbook",
                "title": "Off playbook: Limitation of liability - customer uncapped",
                "explanation": "Starter playbook (not yet your firm's): unlimited.",
                "anchor": "The Customer's liability"}
    return Finding(**{**base, **kw})


PLANTED = model_corpus.by_name("pb_customer_uncapped").planted_with_text()[0]


def test_a_finding_matches_by_category_and_clause_or_by_anchor():
    assert score_model.matches(_finding(), PLANTED)
    # The right clause in the wrong category is not a playbook finding.
    assert not score_model.matches(_finding(category="defined-term"), PLANTED)
    # A vague title still matches when it is anchored in the planted text.
    assert score_model.matches(_finding(title="Uncapped exposure",
                                        anchor="liability under or in connection"), PLANTED)
    assert not score_model.matches(_finding(title="Off playbook: Insurance - discretion",
                                            anchor="THIS SERVICES AGREEMENT"), PLANTED)


def _voice(findings=(), summary="", kind="playbook", mode=Mode.REDLINE):
    spec = model_corpus.ModelSpec("x", kind)
    return [v["rule"] for v in score_model.voice(
        spec, ReviewResult(mode=mode, summary=summary, findings=list(findings)))]


def test_voice_rules():
    assert _voice([_finding()]) == []
    assert _voice(summary="Clause 8 could not be read.") == []
    assert "summary longer than one sentence" in _voice(summary="One. Two.")
    # A question is answered in up to two sentences.
    assert _voice(summary="It is England. Clause 15 says so.", mode=Mode.QUESTION) == []
    assert "summary says whether to send" in _voice(summary="Good to go.")
    assert "title does not lead with the clause" in _voice([_finding(title="Issue found")])
    assert _voice([_finding(title="Clause 8.2: customer liability")]) == []
    assert "system word in the explanation" in _voice(
        [_finding(explanation="Starter playbook (not yet your firm's): my tool found it.")])
    # The document's own words, quoted, are not the reviewer's voice.
    assert _voice([_finding(explanation="Starter playbook (not yet your firm's): "
                                        "“risk shall pass on delivery” is fine.")]) == []
    assert "starter finding not labelled as the starter playbook" in _voice(
        [_finding(explanation="Unlimited.")])
    assert "position or response missing on their paper" in _voice(
        [_finding()], kind="their_paper")


def test_key_terms_read_from_a_table_in_the_reply_text():
    spec = model_corpus.by_name("key_terms_kestrel")
    p = spec.parties
    reply = "\n".join([
        "| Term | What the document says | Clause |",
        "| --- | --- | --- |",
        f"| Parties | {p.customer}; {p.supplier} | Parties |",
        (f"| Term | an initial term of {p.initial_term}; renews for {p.renewal}; notice "
        f"{p.renewal_notice} | 2 |"),
        ("| Liability cap | the greater of 200% of the Charges paid or payable in the 12 "
        f"months before the claim and {p.floor} | 8.1 |"),
        "| Governing law | New York | 15.1 |",
    ])
    produced = Produced(name=spec.name, result=ReviewResult(mode=Mode.QUESTION, summary=reply))
    scored = score_model.score_key_terms(spec, produced)
    assert scored["source"] == "reply text"
    assert scored["fields"] == {"parties": "right", "term": "right", "renewal": "right",
                                "cap": "right", "governing_law": "wrong", "notice": "missing"}


# --- the runner ---------------------------------------------------------------


def test_the_live_run_without_credentials_says_so_and_points_at_the_stub(monkeypatch, capsys):
    from lra.config import settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    settings.cache_clear()
    try:
        assert score_model.main(["--live"]) == 2
    finally:
        settings.cache_clear()
    out = capsys.readouterr().out
    assert "ANTHROPIC_API_KEY" in out and "--stub perfect" in out


def _fake_review(calls, fail=None):
    def review(doc, mode, instructions, **kw):
        calls.append((doc.filename, mode, instructions, kw))
        if fail:
            raise fail
        return ReviewResult(mode=mode, summary="", findings=[])
    return review


def test_the_live_run_stops_starting_sessions_before_the_budget_is_passed(tmp_path):
    calls: list = []
    specs = SILENCE
    produced = score_model.run_live(specs, tmp_path, budget_usd=5, per_doc_usd=2,
                                    time_budget=3600, review_fn=_fake_review(calls))
    assert len(calls) == 2
    not_run = [p for p in produced.values() if p.not_run]
    assert len(not_run) == 3 and "budget" in not_run[0].not_run
    # What ran is scored, what did not is listed, and all of it was saved.
    card = score_model.score(specs, score_model.load_dir(tmp_path))
    assert card["counts"]["scored"] == 2 and card["counts"]["not_run"] == 3


def test_the_live_run_stops_starting_sessions_after_the_time_budget(tmp_path):
    ticks = iter(range(0, 10_000, 100))
    calls: list = []
    score_model.run_live(SILENCE, tmp_path, budget_usd=100, per_doc_usd=1, time_budget=250,
                         review_fn=_fake_review(calls), clock=lambda: next(ticks))
    assert 1 <= len(calls) < len(SILENCE)


def test_the_live_run_stops_when_the_account_cannot_pay(tmp_path):
    calls: list = []
    produced = score_model.run_live(
        SILENCE, tmp_path, budget_usd=100, per_doc_usd=1, time_budget=3600,
        review_fn=_fake_review(calls, RuntimeError("Your credit balance is too low")))
    assert len(calls) == 1
    assert produced[SILENCE[0].name].error
    assert all(p.not_run for p in list(produced.values())[1:])


def test_the_live_run_asks_for_what_each_set_needs(tmp_path):
    calls: list = []
    specs = [model_corpus.by_name(n) for n in ("pb_texas_law", "their_draft_harbour",
                                               "key_terms_beacon")]
    score_model.run_live(specs, tmp_path, budget_usd=100, per_doc_usd=1, time_budget=3600,
                         review_fn=_fake_review(calls))
    (_, m1, i1, k1), (_, m2, i2, k2), (_, m3, i3, _) = calls
    assert m1 is Mode.REDLINE and i1 == "" and k1["their_paper"] is False
    assert m2 is Mode.REDLINE and k2["their_paper"] is True and "their draft" in i2
    assert m3 is Mode.QUESTION and "key-terms table" in i3
    # No lawyer's memory colours the score, and the Word copy is mounted.
    assert k1["memory_stores"] is None and k1["original"]


def test_the_live_run_goes_through_the_real_review_session(monkeypatch, tmp_path):
    """One document through pipeline/review.py on the scripted platform: the
    session is capped at the per-document budget, and what the agent reported
    is what is scored."""
    from lra import managed
    from lra.config import settings
    from tests import fake_sessions as fs

    fs.configure(monkeypatch, DATABASE_URL=f"sqlite:///{tmp_path}/live.sqlite3")
    monkeypatch.setattr(managed.time, "sleep", lambda s: None)
    spec = model_corpus.by_name("pb_texas_law")
    planted = spec.planted_with_text()[0]
    finding = score_model._position_finding(planted, theirs=False).model_dump(
        mode="json", include={"severity", "category", "title", "explanation", "anchor"})
    fake = fs.DetachedSessions([fs.Turn(fs.idle("end_turn"), polls=0, outputs={
        "findings.json": json.dumps({"summary": "", "findings": [finding]}).encode()})])
    monkeypatch.setattr(managed, "anthropic_client", lambda: fake.client)
    try:
        produced = score_model.run_live([spec], tmp_path / "results", budget_usd=10,
                                        per_doc_usd=1.5, time_budget=600)
    finally:
        settings.cache_clear()
    assert fake.created[0]["budget"]["max_list_cost"]["amount"] == "150"
    card = score_model.score([spec], produced)
    assert card["metrics"]["playbook_recall"] == 1.0 and card["all_met"]
    assert produced[spec.name].session_id


def test_lra_eval_runs_the_scorer(monkeypatch, tmp_path, capsys):
    from lra import cli

    monkeypatch.setattr(sys, "argv", ["lra", "eval", "--stub", "perfect", "--only",
                                      "playbook", "--out", str(tmp_path)])
    assert cli.main() == 0
    assert "meets every target" in capsys.readouterr().out
    assert (tmp_path / "scorecard.md").exists()
    monkeypatch.setattr(sys, "argv", ["lra", "eval"])
    assert cli.main() == 2


# --- triage (lra eval --triage) -----------------------------------------------


@pytest.fixture(scope="module")
def triage_rules(tmp_path_factory):
    return score_model.triage_rules(model_corpus.TRIAGE, tmp_path_factory.mktemp("triage"))


def test_the_triage_set_covers_the_command_set_and_the_hard_cases():
    from lra import triage

    cases = model_corpus.TRIAGE
    assert len(cases) >= 40
    assert len({c.name for c in cases}) == len(cases)
    planned = {triage.canon(i) for c in cases for i in c.expect["intents"]}
    left_out = {triage.canon(i) for i in triage.INTENTS} - planned
    # Discard, lift, connect and forget are the same handlers as their
    # neighbours in the set (approve, add, revoke, remember).
    assert left_out <= {"playbook_discard", "no_ai_lift", "connect", "forget"}, left_out
    assert sum(c.hard for c in cases) >= 10
    for case in cases:
        case.email()                         # every email builds


def test_the_rules_baseline_is_scored_and_leaves_the_model_something_to_beat(triage_rules):
    card = score_model.score_triage(
        model_corpus.TRIAGE, triage_rules,
        {c.name: score_model.triage_stub(c, "perfect") for c in model_corpus.TRIAGE})
    m = card["metrics"]
    assert m["triage_accuracy"] == 1.0 and card["all_met"]
    # The baseline: 51 of 59, and 7 of the 15 hard cases. It was 50 and 6
    # until the rules read "30 thanks" as the answer 30 (router's courtesy
    # words). A change to the rules moves it; say so in the commit.
    assert (m["rules_accuracy"], m["rules_accuracy_hard"]) == (round(51 / 59, 3),
                                                               round(7 / 15, 3))
    missed = {r["name"] for r in card["cases"] if r["rules"]}
    assert {"accept_3_not_5", "draft_and_blackline"} <= missed
    assert "answer_30_thanks" not in missed


def test_a_wrong_model_misses_and_fails_the_run(triage_rules):
    card = score_model.score_triage(
        model_corpus.TRIAGE, triage_rules,
        {c.name: score_model.triage_stub(c, "bad") for c in model_corpus.TRIAGE})
    assert card["metrics"]["triage_accuracy"] == 0.0
    assert not card["all_met"] and card["met"]["matches_or_beats_rules"] is False


def test_the_triage_run_writes_a_scorecard(tmp_path, capsys):
    assert score_model.main(["--triage", "--stub", "perfect", "--only",
                             "accept_3_not_5,help", "--out", str(tmp_path)]) == 0
    text = (tmp_path / "scorecard.md").read_text()
    assert "| accept_3_not_5 (hard) | ok | intents ['instruction']" in text


def test_the_live_triage_without_credentials_says_so(tmp_path, capsys):
    assert score_model.main(["--triage", "--live", "--only", "help",
                             "--out", str(tmp_path)]) == 2
    assert "--stub perfect" in capsys.readouterr().out


def test_the_live_triage_calls_the_model_with_the_evidence_and_saves_each_plan(
        tmp_path, monkeypatch):
    from lra import triage

    seen = []

    def model(facts):
        seen.append(facts)
        return triage.Plan(intents=[triage.Decision(intent="answer", reason="")],
                           answers=[triage.Answer(question_id=11, value="30")])

    monkeypatch.setattr(triage, "call_model", model)
    cases = [c for c in model_corpus.TRIAGE if c.name == "answer_30_thanks"]
    plans, errors = score_model.triage_live(cases, tmp_path / "db", 5.0, tmp_path / "plans")
    assert errors == {}
    assert seen[0]["senders_own_words"] == "30 thanks"
    assert [q["id"] for q in seen[0]["thread"]["open_questions"]] == [11, 12]
    assert score_model.triage_replay(tmp_path / "plans") == plans
    card = score_model.score_triage(cases, {}, plans)
    assert card["metrics"]["triage_accuracy"] == 1.0
