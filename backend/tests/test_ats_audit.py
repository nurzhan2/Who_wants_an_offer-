"""The ATS readability audit: will an employer's parser read this resume?

The audit answers a question the rest of the pipeline never asks. This project
hands PDFs to a language model, which sees the page; an applicant tracking
system reads the text layer and nothing else. A resume can therefore work
perfectly here and be invisible to every employer it is sent to, and the
candidate never finds out why nobody called.

So these tests defend three things.

**That the checks fire on real files.** Almost every assertion below runs
against the fixtures in ``fixtures/resumes`` rather than against hand-built page
objects. A finding that only fires on synthetic input is a finding that will not
fire on a resume.

**That a clean file stays clean.** A false critical is worse than a miss here:
it tells someone to rebuild a resume that was already fine. The three
well-formed PDFs must score 100 with nothing reported.

**That the score means something.** It is arithmetic on the findings, never a
separate judgement, and the number has to survive being read on its own — a file
yielding zero characters must not score 65 just because one finding fired.
"""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from app.core.config import settings
from app.resume import ats_audit, extractor
from app.resume.ats_audit import PageFacts
from app.schemas.ats import ATSReport, FindingCode, Overall, Severity
from app.schemas.llm import ExtractedSkill, ProfileExtraction, WorkPeriod

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures" / "resumes"

#: PDFs laid out the way an ATS wants: one column, real text, contacts written
#: out, recognisable headings.
CLEAN_PDFS = ("single_column_ru.pdf", "english.pdf", "mixed_ru_en.pdf")

ALL_FIXTURES = (
    *CLEAN_PDFS,
    "two_column_ru.pdf",
    "scanned.pdf",
    "image_contacts.pdf",
    "with_table.docx",
    "plain.txt",
)

#: A private-use codepoint: a glyph drawn on the page with no character behind
#: it, which is exactly what a font without a ToUnicode map extracts as.
UNMAPPED = "\ue000"


def audit_fixture(name: str, extraction: ProfileExtraction | None = None) -> ATSReport:
    """Audit a fixture the way the upload path does.

    Through the real extractor rather than a shortcut: the format detection and
    the text a DOCX yields are exactly the inputs the audit gets in production,
    and hand-rolling them here is how a test ends up asserting on a file nobody
    will ever upload.
    """
    content = (FIXTURES / name).read_bytes()
    document = extractor.extract(content, name)
    return ats_audit.audit(
        content,
        source_format=document.source_format,
        raw_text=document.raw_text,
        extraction=extraction,
    )


def codes(report: ATSReport) -> set[FindingCode]:
    """What the report actually raised."""
    return {finding.code for finding in report.findings}


def page(
    text: str = "",
    *,
    words: list[dict[str, Any]] | None = None,
    mixed: list[str] | None = None,
    lines: int = 0,
    table_words: int = 0,
) -> PageFacts:
    """A page carrying only the facts one check reads.

    Used for the threshold tests, and only for those: a fixture cannot be nudged
    to sit exactly on a ratio, and a threshold nobody tests at its boundary is a
    threshold nobody knows the behaviour of.
    """
    return PageFacts(
        words=list(words or []),
        text=text,
        width=595.0,
        columns=[],
        mixed_lines=list(mixed or []),
        line_count=lines,
        table_words=table_words,
        chars=[],
    )


# ── the fixtures ──────────────────────────────────────────────────────


@pytest.mark.parametrize("name", CLEAN_PDFS)
def test_a_well_formed_pdf_is_reported_clean(name: str) -> None:
    """No findings, full score. A false critical costs someone a rewrite."""
    report = audit_fixture(name)
    assert report.findings == []
    assert report.score == 100
    assert report.is_machine_readable


def test_two_column_pdf_is_reported_as_interleaved() -> None:
    """The failure this whole phase exists for."""
    report = audit_fixture("two_column_ru.pdf")

    assert FindingCode.COLUMN_INTERLEAVING in codes(report)
    assert not report.is_machine_readable
    finding = next(f for f in report.findings if f.code is FindingCode.COLUMN_INTERLEAVING)
    assert finding.severity is Severity.CRITICAL


def test_interleaving_finding_shows_the_mangled_text() -> None:
    """A score is an opinion; the shuffled line is evidence.

    The fragment is checked against what the file really produces rather than
    against a stored string, so it stays a quotation and cannot drift into a
    plausible-looking invention.
    """
    report = audit_fixture("two_column_ru.pdf")
    finding = next(f for f in report.findings if f.code is FindingCode.COLUMN_INTERLEAVING)

    assert finding.example_fragment
    pages = ats_audit.gather((FIXTURES / "two_column_ru.pdf").read_bytes())
    produced = {line for facts in pages for line in facts.mixed_lines}
    for shown in finding.example_fragment.splitlines():
        assert any(line.startswith(shown[:40]) for line in produced)


def test_a_scan_scores_zero() -> None:
    """No text layer is not a 65% resume. The parser receives nothing."""
    report = audit_fixture("scanned.pdf")

    assert report.score == 0
    assert codes(report) == {FindingCode.NO_TEXT_LAYER}
    assert report.word_count == 0
    assert not report.is_machine_readable


def test_a_scan_reports_one_thing_rather_than_seven() -> None:
    """Every text check fails on a scan; only the cause is worth reporting.

    Contacts, sections and glyphs would fire at once and bury the single finding
    that explains them, leaving the candidate a list to work through instead of
    one instruction.
    """
    pages = ats_audit.gather((FIXTURES / "scanned.pdf").read_bytes())
    would_fire = {
        ats_audit.CHECK_CODES[check] for check in ats_audit.PDF_CHECKS if check(pages) is not None
    }

    assert len(would_fire) > 1, "fixture no longer exercises the suppression"
    assert codes(audit_fixture("scanned.pdf")) == {FindingCode.NO_TEXT_LAYER}


def test_contacts_drawn_as_an_image_are_reported() -> None:
    """The failure nobody notices: the resume passes, the reply address does not."""
    report = audit_fixture("image_contacts.pdf")
    finding = next(f for f in report.findings if f.code is FindingCode.CONTACTS_NOT_TEXT)

    assert finding.severity is Severity.CRITICAL
    # Both halves of the contact line are pixels, so both must be named: the
    # instruction differs when only one is missing.
    assert "почту" in finding.title
    assert "телефон" in finding.title


def test_the_image_contacts_fixture_isolates_one_defect() -> None:
    """Otherwise the test above could pass for the wrong reason.

    The fixture is ordinary single-column text apart from the contact strip. A
    second finding here means either the fixture drifted or a check is firing on
    well-formed input.
    """
    assert codes(audit_fixture("image_contacts.pdf")) == {FindingCode.CONTACTS_NOT_TEXT}


@pytest.mark.parametrize("name", ["with_table.docx", "plain.txt"])
def test_a_non_pdf_says_what_it_could_not_check(name: str) -> None:
    """Silence on the layout checks would read as "your layout is fine"."""
    report = audit_fixture(name)

    assert FindingCode.FORMAT_NOT_PDF in codes(report)
    assert FindingCode.COLUMN_INTERLEAVING not in report.checks_run
    assert FindingCode.UNMAPPED_FONT not in report.checks_run
    # Saying so is not a defect: the file may never be sent as a PDF at all.
    assert next(f for f in report.findings if f.code is FindingCode.FORMAT_NOT_PDF).penalty == 0


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_the_score_is_the_arithmetic_of_the_findings(name: str) -> None:
    """No separate judgement anywhere: the number is explained by the list."""
    report = audit_fixture(name)
    assert report.score == max(0, 100 - sum(f.penalty for f in report.findings))


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_every_finding_carries_an_instruction(name: str) -> None:
    """A finding without a fix is a complaint."""
    for finding in audit_fixture(name).findings:
        assert finding.fix.strip()
        assert finding.explanation.strip()


@pytest.mark.parametrize("name", ALL_FIXTURES)
def test_a_report_only_claims_the_checks_it_ran(name: str) -> None:
    """Every finding raised must be one this run was able to look for."""
    report = audit_fixture(name)
    assert codes(report) <= set(report.checks_run)


# ── thresholds, at the boundary ───────────────────────────────────────


def test_text_layer_threshold() -> None:
    """One character below the floor is a scan; on it is a resume."""
    floor = settings.ats_min_text_chars

    assert ats_audit.check_text_layer([page("x" * floor)]) is None
    below = ats_audit.check_text_layer([page("x" * (floor - 1))])
    assert below is not None
    assert below.code is FindingCode.NO_TEXT_LAYER


def test_interleaving_threshold() -> None:
    """A couple of crossed lines is a wide heading; a sixth of the page is columns."""
    total = 100
    at = round(total * settings.ats_mixed_line_ratio)

    below = [page(mixed=[f"line {i}" for i in range(at - 1)], lines=total)]
    assert ats_audit.check_columns(below) is None

    exactly = [page(mixed=[f"line {i}" for i in range(at)], lines=total)]
    assert ats_audit.check_columns(exactly) is not None


def test_broken_glyph_threshold() -> None:
    """Below the ratio is punctuation the pattern does not know; above is a dead font."""
    total = 200
    at = round(total * settings.ats_broken_glyph_ratio)

    def text(bad: int) -> str:
        return UNMAPPED * bad + "a" * (total - bad)

    assert ats_audit.check_glyphs([page(text(at - 1))]) is None
    assert ats_audit.check_glyphs([page(text(at))]) is not None


def test_table_word_threshold() -> None:
    """A dates column in a table is fine; a resume built out of tables is not."""
    total = 100
    at = round(total * settings.ats_table_word_ratio)
    words: list[dict[str, Any]] = [{"x0": 0.0, "top": float(i), "text": "w"} for i in range(total)]

    assert ats_audit.check_tables([page(words=words, table_words=at - 1)]) is None
    assert ats_audit.check_tables([page(words=words, table_words=at)]) is not None


def test_a_column_needs_a_real_share_of_the_page() -> None:
    """A page number in the corner is not a second column."""
    width = 595.0
    body: list[dict[str, Any]] = [{"x0": 50.0, "top": float(i), "text": "w"} for i in range(100)]
    stray: list[dict[str, Any]] = [{"x0": 520.0, "top": 800.0, "text": "1"}]

    assert len(ats_audit.detect_columns(body + stray, width)) == 1

    # Enough words in the same band and it is a column, at the same coordinates.
    sidebar: list[dict[str, Any]] = [{"x0": 520.0, "top": float(i), "text": "w"} for i in range(30)]
    assert len(ats_audit.detect_columns(body + sidebar, width)) == 2


# ── contact detection ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    "dates",
    ["04.2022 — 09.2023", "Опыт: 2019 — 2024", "01.2021 - 12.2022"],
)
def test_a_date_range_is_not_a_phone_number(dates: str) -> None:
    """Otherwise a resume whose phone is an image passes half of this check.

    A permissive pattern reads "04.2022 — 09.2023" as digits and separators and
    calls it a phone number. The candidate is then told only the email is
    missing, fixes only that, and stays unreachable.
    """
    found = ats_audit.check_contacts([page(f"Иван Иванов\ni@example.com\n{dates}")])
    assert found is not None
    assert "телефон" in found.title


@pytest.mark.parametrize(
    "phone",
    ["+7 700 000 00 11", "8 (700) 000-00-11", "+77000000011", "+49 30 0000000"],
)
def test_real_phone_formats_are_found(phone: str) -> None:
    """Being strict about length must not make the check miss actual numbers."""
    assert ats_audit.check_contacts([page(f"i@example.com\n{phone}")]) is None


# ── the table the score rests on ──────────────────────────────────────


def test_every_finding_code_has_a_penalty() -> None:
    """A new code without a penalty is a KeyError on somebody's upload."""
    assert set(ats_audit.PENALTY) == set(FindingCode)


def test_a_missing_text_layer_costs_everything() -> None:
    """The one penalty that has to be total, asserted where it is set."""
    assert ats_audit.PENALTY[FindingCode.NO_TEXT_LAYER] >= 100


# ── the two readings ──────────────────────────────────────────────────
#
# The headline the audit exists to produce: how much of what the model read off
# the page survives into the text layer an employer's parser is given.


def extraction_of(
    *companies: tuple[str, str | None], skills: Sequence[str] = ()
) -> ProfileExtraction:
    """A model reading, built from what a fixture demonstrably contains."""
    return ProfileExtraction(
        work_periods=[
            WorkPeriod(company=name, title="Data Engineer", start=start)
            for name, start in companies
        ],
        skills=[ExtractedSkill(name=name) for name in skills],
    )


#: What the model reads out of single_column_ru.pdf: four jobs with their start
#: dates, and skills the sidebar spells out. Written from the fixture source, so
#: a fixture edit that breaks the comparison shows up here rather than silently.
CLEAN_READING = extraction_of(
    ("ООО «Северный Компас»", "2022-04"),
    ("Мастерская «Тихий Пеликан»", "2021-01"),
    ("ТОО «Гранит-Логистика»", "2019-08"),
    ("ООО «Ясный Ветер»", "2018-06"),
    skills=("Python", "Airflow", "PostgreSQL", "Kafka", "dbt", "Docker", "Pandas"),
)

#: The same for two_column_ru.pdf, taken from the hand-maintained reference
#: extraction that already sits beside the fixture.
TWO_COLUMN_READING = ProfileExtraction.model_validate(
    {
        key: value
        for key, value in json.loads(
            (FIXTURES / "two_column_ru.expected.json").read_text(encoding="utf-8")
        ).items()
        if not key.startswith("_")
    }
)


def test_a_clean_resume_loses_nothing_between_the_two_readings() -> None:
    """The control.

    Without it the divergence below would prove only that the comparison finds
    something, not that it finds the right thing.
    """
    content = (FIXTURES / "single_column_ru.pdf").read_bytes()
    report = ats_audit.audit(content, source_format="pdf", extraction=CLEAN_READING)

    assert report.coverage is not None
    assert report.coverage.work_periods.recovered == 4
    assert report.coverage.work_periods.lost == []
    assert report.coverage.is_complete
    assert report.overall is Overall.OK
    assert report.score == 100


def test_a_two_column_resume_loses_the_job_glued_to_the_sidebar() -> None:
    """The number the candidate came for, and the reason behind it.

    The freelance studio is the company that lands on the interleaved line — the
    one extraction really produces, with a sidebar heading welded to its front.
    A parser reading that line files the job under the skills section, so it
    counts as lost even though every letter of the name is present.
    """
    content = (FIXTURES / "two_column_ru.pdf").read_bytes()
    report = ats_audit.audit(content, source_format="pdf", extraction=TWO_COLUMN_READING)

    coverage = report.coverage
    assert coverage is not None
    assert coverage.work_periods.total == 4
    assert coverage.work_periods.recovered == 3
    assert coverage.work_periods.lost == ["Студия «Пиксель-Мираж»"]
    assert not coverage.is_complete
    assert FindingCode.CONTENT_LOST in codes(report)


def test_the_loss_names_what_was_lost() -> None:
    """A count without the names is a number nobody can act on."""
    content = (FIXTURES / "two_column_ru.pdf").read_bytes()
    report = ats_audit.audit(content, source_format="pdf", extraction=TWO_COLUMN_READING)

    finding = next(f for f in report.findings if f.code is FindingCode.CONTENT_LOST)
    assert finding.example_fragment
    assert "Пиксель-Мираж" in finding.example_fragment
    assert "3 из 4" in finding.explanation


def test_coverage_is_absent_rather_than_assumed_complete() -> None:
    """At upload there is no extraction to compare against yet.

    The distinction carries weight: a report with no coverage must not read as a
    report showing no loss, or every resume would look perfect for the half
    minute before extraction finishes.
    """
    content = (FIXTURES / "single_column_ru.pdf").read_bytes()
    report = ats_audit.audit(content, source_format="pdf")

    assert report.coverage is None
    assert FindingCode.CONTENT_LOST not in report.checks_run


def test_a_scan_loses_everything() -> None:
    """Nothing in the text layer means nothing recovered, by definition."""
    content = (FIXTURES / "scanned.pdf").read_bytes()
    report = ats_audit.audit(content, source_format="pdf", extraction=CLEAN_READING)

    assert report.coverage is not None
    assert report.coverage.work_periods.recovered == 0
    # Still one finding: the cause, not the consequence.
    assert codes(report) == {FindingCode.NO_TEXT_LAYER}


# ── the verdict ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("single_column_ru.pdf", Overall.OK),
        ("english.pdf", Overall.OK),
        ("plain.txt", Overall.OK),
        ("two_column_ru.pdf", Overall.UNREADABLE),
        ("scanned.pdf", Overall.UNREADABLE),
        ("image_contacts.pdf", Overall.UNREADABLE),
        ("with_table.docx", Overall.DEGRADED),
    ],
)
def test_the_verdict_matches_what_was_found(name: str, expected: Overall) -> None:
    """One word, because a banner cannot render a distribution.

    plain.txt is OK rather than degraded on purpose: "the layout of a text file
    could not be checked" is information, and grading every non-PDF as degraded
    would make the word mean nothing.
    """
    assert audit_fixture(name).overall is expected


def test_a_docx_built_out_of_tables_is_degraded() -> None:
    """The layout mistake DOCX resumes actually make.

    Word makes table layout the path of least resistance, and many parsers read
    a table cell by cell — so a skills grid arrives as a shuffled bag of words.
    Answering "not a PDF, cannot say" here would be a miss, not modesty.
    """
    report = audit_fixture("with_table.docx")

    assert FindingCode.TEXT_IN_TABLES in codes(report)
    assert FindingCode.TEXT_IN_TABLES in report.checks_run
    assert report.overall is Overall.DEGRADED


def test_the_verdict_is_derived_and_not_stored() -> None:
    """It travels in the API payload but cannot be written to.

    Stored, it would be the one part of the report that nothing keeps honest.
    """
    report = audit_fixture("two_column_ru.pdf")
    revalidated = ATSReport.model_validate(report.model_dump(mode="json"))

    assert revalidated.overall is report.overall
    assert "overall" in report.model_dump(mode="json")


# ── dates and sections ────────────────────────────────────────────────


@pytest.mark.parametrize("name", CLEAN_PDFS)
def test_dates_are_found_in_a_normal_resume(name: str) -> None:
    """The check has to stay quiet on the fixtures that do nothing wrong."""
    assert FindingCode.DATES_NOT_EXTRACTABLE not in codes(audit_fixture(name))


def test_a_resume_with_no_readable_dates_is_flagged() -> None:
    """An ATS keys seniority off dates; without them the resume reads as no
    experience at all, however long the history really is."""
    text = "Иван Иванов\ni@example.com\n+7 700 000 00 11\nОпыт работы\nОбразование\nНавыки"
    found = ats_audit.check_dates([page(text)])

    assert found is not None
    assert found.severity is Severity.WARNING


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("04.2022", (2022, 4)),
        ("2022-04", (2022, 4)),
        ("12/2019", (2019, 12)),
        ("апрель 2022", (2022, 4)),
        ("Sep 2021", (2021, 9)),
    ],
)
def test_dates_compare_by_meaning_not_by_spelling(token: str, expected: tuple[int, int]) -> None:
    """The model normalises every date to YYYY-MM; the resume prints whatever it
    likes. Comparing the strings would report a loss on every resume that writes
    its dates the ordinary way."""
    assert ats_audit.year_month(token) == expected


def test_sections_are_reported_by_name() -> None:
    """The UI shows which sections a parser finds, not only which are missing."""
    report = audit_fixture("single_column_ru.pdf")

    assert set(report.sections_detected) == {"experience", "education", "skills"}
    assert audit_fixture("scanned.pdf").sections_detected == []


def test_a_line_carrying_a_heading_part_way_along_is_not_trusted() -> None:
    """This is what separates "present" from "readable".

    The words are all there; the parser attributes them to the wrong section.
    Counting such a line as recovered would report a clean resume while the
    candidate's job history is being filed under their skills list.
    """
    facts = [
        page(
            "ООО «Северный Компас» — Алматы\n"
            # The heading sits part way along, which is the signal. At the start
            # of a line it is just a heading; the fixture's own interleaved
            # lines look like that, and geometry is what catches those.
            "Мастерская «Тихий Пеликан» — удалённо НАВЫКИ Python, SQL"
        )
    ]
    lines = ats_audit.readable_lines(facts)

    assert any("Северный Компас" in line for line in lines)
    assert not any("Тихий Пеликан" in line for line in lines)
