"""The send-moment demo: the committed files are what the builder makes, and
the review of them says what the README promises."""

from __future__ import annotations

from demos.outbound import build
from secondeye.mail.console import ConsoleProvider
from secondeye.pipeline import checks, email_checks, extract, intake


def test_the_committed_files_are_what_the_builder_writes():
    """Someone attaching files/ by hand sends exactly what the eval measures."""
    for name, builder in ((build.SPA, build.agreement), (build.DECK, build.deck),
                          (build.EXHIBIT, build.exhibit), ("email.eml", build.message)):
        assert (build.FILES / name).read_bytes() == builder(), name


def test_the_review_makes_every_catch_the_readme_lists():
    email = ConsoleProvider().parse_eml(build.message())
    att = intake.pick_document(email)
    assert att.filename == build.SPA
    doc = extract.extract(att)
    found = checks.run_all(doc, att.content) + email_checks.run_all(email, doc, att, [])
    titles = "\n".join(f.title for f in found)
    for expected in (
        "Unfilled square-bracket placeholder: [●]",
        "2 internal comment(s) are still in the document",
        "This is going out as final, but it is still marked as a draft",
        "In Exhibit A - Permitted Advisers.pdf: the PDF still carries mark-up",
        "The document contains hidden text",
        "The file carries an embedded spreadsheet",
        'You said you removed "exclusivity clause"',
        "In Atlas board update.pptx: speaker notes are on slide 3",
        "In Atlas board update.pptx: slide 4 is hidden in the deck",
        "The file's properties name Bluewater Shipping LLC",
        "Text is still highlighted in a version going out as final",
        "Your note says version 5, but the file attached is",
        "Your note says the disclosure letter is attached, but it is not",
        "Author metadata identifies Priya Associate",
    ):
        assert expected in titles, expected
    # The claim that is true, and the parties the note names, say nothing.
    assert "2,000,000" not in titles and "Northgate" not in titles
