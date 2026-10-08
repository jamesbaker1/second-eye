"""The apply() contract at the finding level.

The writer itself is tested in test_ooxml.py. These cover the policy decisions:
what gets written automatically, what is held back for the lawyer, and what
happens when an edit cannot be made safely.
"""

from pathlib import Path

from secondeye.models import Finding, Mode, ReviewResult, Severity
from secondeye.pipeline import redline


def result(*findings) -> ReviewResult:
    return ReviewResult(mode=Mode.REDLINE, summary="", findings=list(findings))


def finding(auto_apply=True, suggested="new text", anchor="old text") -> Finding:
    return Finding(
        severity=Severity.FORMATTING,
        category="typo",
        title="Fix typo",
        explanation="",
        anchor=anchor,
        suggested_text=suggested,
        auto_apply=auto_apply,
    )


def test_judgment_calls_are_not_auto_applied():
    out = redline.apply(b"", result(finding(auto_apply=False)))
    assert not out.applied
    assert len(out.skipped) == 1


def test_memo_only_findings_have_no_suggested_text():
    out = redline.apply(b"", result(finding(suggested=None)))
    assert not out.applied


def test_output_opens_in_word():
    sample = Path("samples/simple.docx").read_bytes()
    out = redline.apply(sample, result(finding(suggested="Mutual Nondisclosure Agreement")))
    assert redline.verify_opens(out.content)


def test_changes_are_tracked_not_baked_in():
    """Every edit must appear as w:ins/w:del so the recipient can reject it."""
    import zipfile
    from io import BytesIO

    sample = Path("samples/simple.docx").read_bytes()
    out = redline.apply(
        sample,
        result(finding(anchor="Mutual Non-Disclosure Agreement",
                       suggested="Mutual Nondisclosure Agreement")),
    )
    assert out.applied, "the edit did not land"
    xml = zipfile.ZipFile(BytesIO(out.content)).read("word/document.xml").decode()
    assert "<w:ins " in xml and "<w:del " in xml
    assert 'w:author="Reviewer"' in xml
