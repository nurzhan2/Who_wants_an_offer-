"""Will a machine be able to read this resume?

Phase 2 established that ``pdfplumber`` turns a two-column resume into this:

    ЗАРПЛАТА покрытие тестами с 30 до 78 процентов.
    от 1 800 000 KZT Стек: Python 3, FastAPI, PostgreSQL, Redis, Kafka, Docker
    НАВЫКИ Студия «Пиксель-Мираж» (фриланс, part-time) — удалённо

A sidebar heading welded to a sentence from the other column. This project
sidesteps that by handing the PDF to a model, which sees the layout. **An
employer's applicant tracking system cannot.** It reads exactly this text layer,
and the candidate never finds out why nobody called.

So the same machinery answers a question worth money to the person applying:
what does the robot see? Every check here works on the extracted text layer and
its coordinates — no LLM. Using a capable model to judge this would be modelling
the wrong reader; the point is to be as dumb as the parser on the other side.

The findings are deliberately specific. "Improve readability" is not actionable;
"your email is a picture, type it next to the icon" is.
"""

import io
import re
import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import docx
import pdfplumber

from app.core.config import settings
from app.core.logging import get_logger
from app.schemas.ats import ATSCoverage, ATSReport, Finding, FindingCode, Recoverable, Severity
from app.schemas.llm import ProfileExtraction

logger = get_logger(__name__)

#: What each defect costs. The score is the arithmetic of the findings, never a
#: separate judgement, so it can always be explained by pointing at the list.
#:
#: Keyed by code rather than by severity, because two critical findings are not
#: equally bad. A flat severity penalty scored a file with no text layer at
#: 65/100 — a number that reads like a pass on a resume from which an employer's
#: parser extracts literally nothing. Severity says how to display a finding;
#: this says how much damage it does.
PENALTY: dict[FindingCode, int] = {
    # The parser gets nothing at all. There is no partial credit to give.
    FindingCode.NO_TEXT_LAYER: 100,
    # Text comes out, but shuffled: the parser reads a sentence assembled from
    # two columns and files the candidate under whatever it made of it.
    FindingCode.BROKEN_GLYPHS: 70,
    FindingCode.COLUMN_INTERLEAVING: 50,
    FindingCode.UNMAPPED_FONT: 40,
    # Everything parses except the one thing needed to reply.
    FindingCode.CONTACTS_NOT_TEXT: 35,
    # The parser reads the words but attributes them to the wrong job.
    FindingCode.CONTENT_LOST: 30,
    FindingCode.DATES_NOT_EXTRACTABLE: 25,
    FindingCode.TEXT_IN_TABLES: 15,
    FindingCode.MISSING_SECTIONS: 12,
    FindingCode.FORMAT_NOT_PDF: 0,
}

EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
#: Loose about formatting, strict about length. An ATS regex is loose too, and
#: the question is only whether *something* phone-shaped survives into the text
#: layer. The digit floor and the missing dash variants are what stop a date
#: range from answering that question: "04.2022 — 09.2023" is a run of digits,
#: spaces and a dash, and a permissive pattern reads it as a phone number — so
#: a resume whose real phone is an image would quietly pass this check.
PHONE = re.compile(r"\+?\d[\d\s()-]{7,}\d")
#: Shortest national number worth calling, minus nothing. Kazakh and Russian
#: numbers are 11 digits, EU ones 9 and up.
MIN_PHONE_DIGITS = 9
#: pdfminer emits this when a font carries no ToUnicode map: the glyph is drawn
#: but there is no character behind it.
CID_GLYPH = re.compile(r"\(cid:\d+\)")

#: Ways a resume writes an employment period. An ATS keys seniority off these,
#: so a resume whose dates survive only as an image reads as no experience at
#: all. Loose about separators, strict about the year: a bare "2019" inside a
#: sentence is not a period.
DATE_TOKEN = re.compile(
    r"(?:\b(?:0?[1-9]|1[0-2])[./-](?:19|20)\d{2}\b)"
    r"|(?:\b(?:19|20)\d{2}[./-](?:0?[1-9]|1[0-2])\b)"
    r"|(?:\b(?:янв|фев|мар|апр|мая|май|июн|июл|авг|сен|окт|ноя|дек"
    r"|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[а-яa-z]*\.?\s+(?:19|20)\d{2}\b)",
    re.IGNORECASE,
)
#: Below this a resume has no employment history a parser can date.
MIN_DATE_TOKENS = 2
#: Anything above this in a two-number date is the year, not the month.
MONTHS_IN_YEAR = 12

#: Section headings a parser looks for, in the two languages this market writes
#: resumes in. Missing them does not break extraction, it breaks *sectioning* —
#: the ATS cannot tell which dates belong to jobs and which to degrees.
SECTION_PATTERNS: dict[str, re.Pattern[str]] = {
    "experience": re.compile(
        r"\b(experience|employment|work\s+history)\b|опыт\s+работы|опыт", re.IGNORECASE
    ),
    "education": re.compile(r"\b(education|degree)\b|образование", re.IGNORECASE),
    "skills": re.compile(r"\b(skills|technical\s+skills|stack)\b|навыки|стек", re.IGNORECASE),
}

#: Characters a Russian or English resume is made of. Anything far outside this
#: is a font that did not survive extraction.
EXPECTED_CHARS = re.compile(r"[a-zA-Zа-яА-ЯёЁ0-9\s\.,;:!?()\[\]{}«»\"'`@#№%&*+/\\|_—–\-…€$₸₽]")


#: A check reads the pages and either reports something or stays quiet.
type Check = Callable[[Sequence[PageFacts]], Finding | None]


@dataclass(frozen=True, slots=True)
class Column:
    """A vertical band of the page that words line up in."""

    left: float
    right: float


@dataclass(frozen=True, slots=True)
class PageFacts:
    """Everything the checks need from one page, gathered once."""

    words: list[dict[str, Any]]
    text: str
    width: float
    columns: list[Column]
    mixed_lines: list[str]
    line_count: int
    table_words: int
    chars: list[dict[str, Any]]


def detect_columns(words: Sequence[dict[str, Any]], width: float) -> list[Column]:
    """Vertical bands the words start in.

    Clustering on the left edge rather than the whole box: a sidebar and a body
    column differ by where their lines *begin*, and using the full extent would
    merge them as soon as one long line crossed the gutter.

    A band has to hold a real share of the page's words to count. Otherwise a
    page number in a corner reads as a second column.
    """
    if not words:
        return []
    starts = sorted(word["x0"] for word in words)
    gap = width * settings.ats_column_gap_ratio
    clusters: list[list[float]] = [[starts[0]]]
    for value in starts[1:]:
        if value - clusters[-1][-1] > gap:
            clusters.append([value])
        else:
            clusters[-1].append(value)

    floor = len(words) * settings.ats_min_column_share
    return [Column(left=min(c), right=max(c)) for c in clusters if len(c) >= floor]


def _column_of(word: dict[str, Any], columns: Sequence[Column]) -> int:
    """Which band a word starts in, or -1 when it starts between them."""
    for index, column in enumerate(columns):
        if column.left - 1 <= word["x0"] <= column.right + 1:
            return index
    return -1


def _visual_lines(words: Sequence[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group words into the rows a reader sees.

    Bucketed on the rounded top coordinate: text extraction gives every word its
    own baseline, and words on one visual line differ by fractions of a point.
    """
    rows: dict[int, list[dict[str, Any]]] = {}
    for word in words:
        rows.setdefault(round(word["top"] / 3), []).append(word)
    return [sorted(row, key=lambda w: w["x0"]) for _, row in sorted(rows.items())]


def find_mixed_lines(
    words: Sequence[dict[str, Any]], columns: Sequence[Column]
) -> tuple[list[str], int]:
    """Lines that read across two columns, and how many lines there were.

    This is the interleaving, made visible. A line holding words from two bands
    is one the extractor will emit as a single run of text, gluing a sidebar
    heading to a sentence that belongs somewhere else entirely.
    """
    lines = _visual_lines(words)
    if len(columns) < 2:
        return [], len(lines)

    mixed: list[str] = []
    for line in lines:
        bands = {_column_of(word, columns) for word in line} - {-1}
        if len(bands) >= 2:
            mixed.append(" ".join(word["text"] for word in line))
    return mixed, len(lines)


def _count_table_words(page: pdfplumber.page.Page, words: Sequence[dict[str, Any]]) -> int:
    """Words that sit inside a detected table.

    A skills grid drawn as a table, or a "proficiency" bar chart with labels, is
    the classic way to lose an entire skill list: the words are there, and they
    come out in an order that means nothing.
    """
    try:
        tables = page.find_tables()
    except Exception:  # pdfplumber's table finder is best-effort on odd files
        return 0

    boxes = [table.bbox for table in tables]
    if not boxes:
        return 0
    return sum(
        1
        for word in words
        for (x0, top, x1, bottom) in boxes
        if x0 <= word["x0"] <= x1 and top <= word["top"] <= bottom
    )


def gather(content: bytes) -> list[PageFacts]:
    """Read every page once, so each check does not re-open the file."""
    pages: list[PageFacts] = []
    with pdfplumber.open(io.BytesIO(content)) as pdf:
        for page in pdf.pages:
            words = page.extract_words()
            columns = detect_columns(words, page.width)
            mixed, line_count = find_mixed_lines(words, columns)
            pages.append(
                PageFacts(
                    words=words,
                    text=page.extract_text() or "",
                    width=page.width,
                    columns=columns,
                    mixed_lines=mixed,
                    line_count=line_count,
                    table_words=_count_table_words(page, words),
                    chars=page.chars,
                )
            )
    return pages


def _finding(
    code: FindingCode,
    severity: Severity,
    title: str,
    explanation: str,
    fix: str,
    example: str | None = None,
) -> Finding:
    """One finding, with its penalty read off :data:`PENALTY`."""
    return Finding(
        code=code,
        severity=severity,
        title=title,
        explanation=explanation,
        example_fragment=example,
        fix=fix,
        penalty=PENALTY[code],
    )


def check_text_layer(pages: Sequence[PageFacts]) -> Finding | None:
    """Is there any text at all, or is this a picture of a resume?"""
    total = sum(len(page.text.strip()) for page in pages)
    if total >= settings.ats_min_text_chars:
        return None
    return _finding(
        FindingCode.NO_TEXT_LAYER,
        Severity.CRITICAL,
        "В файле нет текстового слоя",
        f"Из файла извлекается {total} символов. Это скан или картинка: "
        "робот работодателя не прочитает оттуда ни одного слова и увидит пустое резюме.",
        "Экспортируй резюме в PDF из текстового редактора, а не сканируй "
        "распечатку и не сохраняй как изображение.",
    )


def check_columns(pages: Sequence[PageFacts]) -> Finding | None:
    """Do the columns interleave when read as text?"""
    mixed = [line for page in pages for line in page.mixed_lines]
    lines = sum(page.line_count for page in pages)
    if not lines or not mixed:
        return None

    share = len(mixed) / lines
    if share < settings.ats_mixed_line_ratio:
        return None

    example = "\n".join(line[:100] for line in mixed[:3])
    return _finding(
        FindingCode.COLUMN_INTERLEAVING,
        Severity.CRITICAL,
        "Колонки перемешиваются при чтении",
        f"Резюме свёрстано в несколько колонок, и {share:.0%} строк текстового слоя "
        "склеивают боковую колонку с основной. Робот читает страницу построчно "
        "поперёк колонок и получает именно это:",
        "Перевёрстай в одну колонку. Это единственная надёжная починка: "
        "боковая панель с навыками и контактами всегда будет склеиваться "
        "с текстом рядом.",
        example,
    )


def check_contacts(pages: Sequence[PageFacts]) -> Finding | None:
    """Can a machine find an email and a phone number?"""
    text = "\n".join(page.text for page in pages)
    has_email = bool(EMAIL.search(text))
    has_phone = any(
        sum(char.isdigit() for char in match) >= MIN_PHONE_DIGITS for match in PHONE.findall(text)
    )
    if has_email and has_phone:
        return None

    missing = [
        name for name, present in (("почту", has_email), ("телефон", has_phone)) if not present
    ]
    return _finding(
        FindingCode.CONTACTS_NOT_TEXT,
        Severity.CRITICAL,
        f"Робот не находит {' и '.join(missing)}",
        "В текстовом слое нет ничего похожего на "
        f"{' и '.join(missing)}. Обычно это значит, что контакты нарисованы "
        "иконками или вставлены картинкой. Резюме может пройти отбор, "
        "и связаться с тобой будет нечем.",
        "Впиши почту и телефон обычным текстом — рядом с иконкой или вместо неё. "
        "Иконка остаётся, текст добавляется.",
    )


def check_glyphs(pages: Sequence[PageFacts]) -> Finding | None:
    """Does the text come out as characters, or as noise?"""
    text = "".join(page.text for page in pages)
    stripped = "".join(text.split())
    if not stripped:
        return None

    recognised = len(EXPECTED_CHARS.findall(stripped))
    broken = 1 - recognised / len(stripped)
    if broken < settings.ats_broken_glyph_ratio:
        return None

    sample = next(
        (line for line in text.splitlines() if line.strip() and not EXPECTED_CHARS.match(line)),
        None,
    )
    return _finding(
        FindingCode.BROKEN_GLYPHS,
        Severity.CRITICAL,
        "Текст извлекается искажённым",
        f"{broken:.0%} символов текстового слоя не опознаются как буквы или цифры. "
        "Шрифт нарисован правильно, но за глифами нет символов — робот получает мусор.",
        "Пересохрани PDF с внедрением шрифтов, либо смени шрифт на "
        "стандартный. Проверить просто: выдели текст в резюме и вставь "
        "в блокнот — увидишь то же, что увидит робот.",
        (sample or "")[:100] or None,
    )


def check_unmapped_fonts(pages: Sequence[PageFacts]) -> Finding | None:
    """Are there glyphs with no character behind them?"""
    text = "".join(page.text for page in pages)
    hits = CID_GLYPH.findall(text)
    if not hits:
        return None
    return _finding(
        FindingCode.UNMAPPED_FONT,
        Severity.CRITICAL,
        "Шрифт без таблицы соответствия символов",
        f"В извлечённом тексте {len(hits)} мест вида (cid:NN). Шрифт вставлен без "
        "таблицы ToUnicode: буквы видны человеку и отсутствуют для машины.",
        "Пересохрани PDF из исходника с внедрением шрифтов, или замени шрифт на системный.",
        " ".join(hits[:8]),
    )


def check_tables(pages: Sequence[PageFacts]) -> Finding | None:
    """How much of the content lives inside a table?"""
    inside = sum(page.table_words for page in pages)
    total = sum(len(page.words) for page in pages)
    if not total:
        return None

    share = inside / total
    if share < settings.ats_table_word_ratio:
        return None
    return _finding(
        FindingCode.TEXT_IN_TABLES,
        Severity.WARNING,
        "Содержимое свёрстано таблицами",
        f"{share:.0%} слов находятся внутри таблиц. Многие ATS читают таблицу "
        "по ячейкам без учёта смысла, и список навыков или дат превращается "
        "в перемешанный набор слов.",
        "Замени табличную вёрстку обычными абзацами и списками. "
        "Особенно это касается блока навыков и шкал «уровень владения».",
    )


def detect_sections(pages: Sequence[PageFacts]) -> list[str]:
    """Which standard sections a parser would find, by the name it looks for."""
    text = "\n".join(page.text for page in pages)
    return sorted(name for name, pattern in SECTION_PATTERNS.items() if pattern.search(text))


def check_dates(pages: Sequence[PageFacts]) -> Finding | None:
    """Can a machine find employment periods, or only see them as a picture?"""
    text = "\n".join(page.text for page in pages)
    found = DATE_TOKEN.findall(text)
    if len(found) >= MIN_DATE_TOKENS:
        return None
    return _finding(
        FindingCode.DATES_NOT_EXTRACTABLE,
        Severity.WARNING,
        "Робот не находит периоды работы",
        f"В текстовом слое найдено дат в узнаваемом формате: {len(found)}. "
        "ATS считает стаж по датам рядом с местами работы; без них резюме "
        "выглядит как опыт длиной ноль лет, каким бы он ни был на самом деле.",
        "Пиши периоды текстом в формате ММ.ГГГГ — «04.2022 — 09.2023» или "
        "«04.2022 — по настоящее время». Не выноси даты в графику и не "
        "оставляй только годы без месяцев.",
    )


def check_sections(pages: Sequence[PageFacts]) -> Finding | None:
    """Are the standard section headings there to be found?"""
    text = "\n".join(page.text for page in pages)
    missing = sorted(name for name, pattern in SECTION_PATTERNS.items() if not pattern.search(text))
    if not missing:
        return None

    russian = {"experience": "опыт работы", "education": "образование", "skills": "навыки"}
    return _finding(
        FindingCode.MISSING_SECTIONS,
        Severity.WARNING,
        "Нет узнаваемых заголовков разделов",
        "Не найдены заголовки: " + ", ".join(russian[name] for name in missing) + ". "
        "Робот разбирает резюме по секциям; без заголовков он не отличит "
        "даты работы от дат учёбы.",
        "Добавь простые заголовки «Опыт работы», «Образование», «Навыки» — "
        "именно словами, а не только визуальным выделением.",
    )


#: Checks that need a PDF's geometry, in the order they are reported.
PDF_CHECKS = (
    check_text_layer,
    check_columns,
    check_unmapped_fonts,
    check_glyphs,
    check_contacts,
    check_dates,
    check_tables,
    check_sections,
)
#: Checks that work on any text, whatever produced it.
#: What can be asked of text with no page behind it.
TEXT_CHECKS = (check_contacts, check_glyphs, check_dates, check_sections)

#: A DOCX adds the one layout question worth asking of it. Kept separate from
#: TEXT_CHECKS so a plain .txt does not claim to have been checked for tables
#: it cannot have.
DOCX_CHECKS = (*TEXT_CHECKS, check_tables)


def _docx_pages(content: bytes, raw_text: str) -> list[PageFacts]:
    """Facts a DOCX can answer for.

    There is no geometry to read, but there is one thing a DOCX gets wrong more
    often than a PDF: layout built out of tables. Word makes that the path of
    least resistance, and many parsers read a table cell by cell, so a skills
    grid comes out as a shuffled bag of words. Counting table words here is what
    turns "we could not check the layout" into an answer.
    """
    document = docx.Document(io.BytesIO(content))
    in_tables = [
        word
        for table in document.tables
        for row in table.rows
        for cell in row.cells
        for word in cell.text.split()
    ]
    loose = [word for para in document.paragraphs for word in para.text.split()]
    words = [{"x0": 0.0, "top": 0.0, "text": word} for word in loose + in_tables]
    return [
        PageFacts(
            words=words,
            text=raw_text,
            width=0.0,
            columns=[],
            mixed_lines=[],
            line_count=len(raw_text.splitlines()),
            table_words=len(in_tables),
            chars=[],
        )
    ]


def _text_only_pages(text: str) -> list[PageFacts]:
    """Wrap plain text so the text-level checks can run on a DOCX or a TXT."""
    return [
        PageFacts(
            words=[],
            text=text,
            width=0.0,
            columns=[],
            mixed_lines=[],
            line_count=len(text.splitlines()),
            table_words=0,
            chars=[],
        )
    ]


# ── the two readings ──────────────────────────────────────────────────
#
# Everything above judges the file. This judges the *loss*: the structure the
# model read out of the resume, against the structure still recoverable from
# the text layer alone. The model saw the page and got it right; the text layer
# is all an employer's parser is given. The gap between them is the number the
# candidate actually came for — "an ATS will see 2 of your 4 jobs" — and it
# costs no extra model call, because the extraction has already been paid for.

#: Month names as resumes write them, by the prefix that identifies them.
MONTH_NAMES: dict[str, int] = {
    "янв": 1,
    "фев": 2,
    "мар": 3,
    "апр": 4,
    "мая": 5,
    "май": 5,
    "июн": 6,
    "июл": 7,
    "авг": 8,
    "сен": 9,
    "окт": 10,
    "ноя": 11,
    "дек": 12,
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

#: Characters that differ between how a resume prints a name and how the model
#: reads it back. Stripped from both sides before comparing.
NOISE = re.compile(r"[«»\"'`()\[\]{},.;:!?—–\-\s]+")


def _flatten(value: str) -> str:
    """Casefold and drop punctuation, so two spellings of one name compare equal."""
    return NOISE.sub(" ", value).casefold().strip()


def year_month(token: str) -> tuple[int, int] | None:
    """Normalise one date as the text layer writes it to ``(year, month)``."""
    digits = re.findall(r"\d+", token)
    if len(digits) == 2:
        first, second = int(digits[0]), int(digits[1])
        if first > MONTHS_IN_YEAR:  # YYYY-MM
            return (first, second) if 1 <= second <= MONTHS_IN_YEAR else None
        return (second, first) if second > MONTHS_IN_YEAR else None
    if len(digits) == 1:
        head = _flatten(token)[:3]
        month = MONTH_NAMES.get(head)
        return (int(digits[0]), month) if month else None
    return None


def readable_lines(pages: Sequence[PageFacts]) -> list[str]:
    """Lines a sectioning parser would attribute to the right place.

    A line that mixes two columns is dropped, and so is one carrying a section
    heading somewhere other than its start: both are lines where the words are
    present but filed under the wrong job, which is worse than losing them —
    the candidate's freelance stint gets read as part of their skills list.
    """
    dropped = {line for page in pages for line in page.mixed_lines}
    clean: list[str] = []
    for page in pages:
        for line in page.text.splitlines():
            stripped = line.strip()
            if not stripped or stripped in dropped:
                continue
            heading = next(
                (m for p in SECTION_PATTERNS.values() if (m := p.search(stripped))), None
            )
            if heading is not None and heading.start() > 0:
                continue
            clean.append(stripped)
    return clean


def _recoverable(items: Sequence[str], lines: Sequence[str]) -> Recoverable:
    """How many of these survive into text a parser reads correctly."""
    haystack = " " + " ".join(_flatten(line) for line in lines) + " "
    named = [item for item in items if item and item.strip()]
    lost = [item for item in named if _flatten(item) not in haystack]
    return Recoverable(total=len(named), recovered=len(named) - len(lost), lost=lost[:10])


def _dates_recoverable(extraction: ProfileExtraction, lines: Sequence[str]) -> Recoverable:
    """Employment periods whose start date survives as a readable date.

    Compared as (year, month) rather than as strings: the model normalises every
    date to YYYY-MM and the resume prints 04.2022, «апрель 2022» or 2022-04, and
    all three mean the same thing to an ATS.
    """
    present = {
        parsed
        for line in lines
        for token in DATE_TOKEN.findall(line)
        if (parsed := year_month(token))
    }
    dated = [period for period in extraction.work_periods if period.start]
    lost = [
        f"{period.company}: {period.start}"
        for period in dated
        if (ym := year_month(str(period.start))) is None or ym not in present
    ]
    return Recoverable(total=len(dated), recovered=len(dated) - len(lost), lost=lost[:10])


def measure_coverage(extraction: ProfileExtraction, pages: Sequence[PageFacts]) -> ATSCoverage:
    """Compare what the model read with what a text-layer parser recovers."""
    lines = readable_lines(pages)
    return ATSCoverage(
        work_periods=_recoverable([p.company for p in extraction.work_periods], lines),
        dates=_dates_recoverable(extraction, lines),
        skills=_recoverable([skill.name for skill in extraction.skills], lines),
    )


def _coverage_finding(coverage: ATSCoverage) -> Finding | None:
    """Report the loss, in the terms the candidate cares about."""
    if coverage.is_complete:
        return None

    jobs, skills = coverage.work_periods, coverage.skills
    parts = [
        f"мест работы — {part.recovered} из {part.total}"
        if label == "jobs"
        else f"{label} — {part.recovered} из {part.total}"
        for label, part in (
            ("jobs", jobs),
            ("дат", coverage.dates),
            ("навыков", skills),
        )
        if not part.is_complete
    ]
    example = "; ".join(jobs.lost or coverage.dates.lost or skills.lost)
    return _finding(
        FindingCode.CONTENT_LOST,
        Severity.CRITICAL if jobs.recovered == 0 and jobs.total else Severity.WARNING,
        "Часть резюме не доходит до робота",
        "Из того, что видно человеку на странице, машинному чтению доступно: "
        + ", ".join(parts)
        + ". Остальное либо теряется, либо приклеивается к чужому разделу — "
        "робот отнесёт этот опыт не туда, где он есть.",
        "Убери двухколоночную вёрстку и таблицы: почти всегда расхождение "
        "берётся оттуда. После правки прогони проверку ещё раз — число должно "
        "стать полным.",
        example[:200] or None,
    )


def audit(
    content: bytes,
    *,
    source_format: str,
    raw_text: str = "",
    extraction: ProfileExtraction | None = None,
) -> ATSReport:
    """Judge how a machine will read this file.

    A PDF gets every check. A DOCX gets the text-level ones plus the table
    check, which is the layout mistake DOCX resumes actually make. Anything
    else gets the text-level ones and says so, rather than reporting a clean
    bill of health it did not earn.

    ``extraction`` is what the model read out of the same file. Passing it turns
    on the half of the report that matters most — how much of that survives into
    the text layer — and it is optional because the report is produced twice:
    once at upload, when no extraction exists yet and the structural findings
    are already worth showing, and again when parsing finishes.
    """
    checks: tuple[Check, ...]
    extra: list[Finding] = []
    if source_format == "pdf":
        pages = gather(content)
        checks = PDF_CHECKS
    else:
        docx_file = source_format == "docx"
        pages = _docx_pages(content, raw_text) if docx_file else _text_only_pages(raw_text)
        checks = DOCX_CHECKS if docx_file else TEXT_CHECKS
        extra = [
            _finding(
                FindingCode.FORMAT_NOT_PDF,
                Severity.INFO,
                f"Проверен как {source_format.upper()}, не как PDF",
                "Проверки вёрстки, которым нужна геометрия страницы — колонки, "
                "шрифты, глифы — относятся к PDF и здесь не выполнялись. "
                "Большинство работодателей ждёт PDF.",
                "Если отправляешь в PDF — прогони проверку ещё раз на самом PDF: "
                "вёрстка при экспорте меняется.",
            )
        ]

    findings = [*extra]
    for check in checks:
        found = check(pages)
        if found is not None:
            findings.append(found)

    coverage: ATSCoverage | None = None
    if extraction is not None:
        coverage = measure_coverage(extraction, pages)
        lost = _coverage_finding(coverage)
        if lost is not None:
            findings.append(lost)

    # A file with no text layer cannot fail the checks that read the text; they
    # would all fire at once and bury the one finding that matters.
    if any(f.code is FindingCode.NO_TEXT_LAYER for f in findings):
        findings = [f for f in findings if f.code is FindingCode.NO_TEXT_LAYER]

    report = ATSReport(
        score=max(0, 100 - sum(f.penalty for f in findings)),
        findings=findings,
        checks_run=_codes_for(checks, bool(extra), coverage is not None),
        sections_detected=detect_sections(pages),
        coverage=coverage,
        source_format=source_format,
        page_count=len(pages) if source_format == "pdf" else 0,
        word_count=sum(len(page.words) for page in pages) or len(raw_text.split()),
    )
    logger.info(
        "resume.ats_audited",
        source_format=source_format,
        score=report.score,
        overall=report.overall.value,
        critical=len(report.critical),
        findings=len(report.findings),
        jobs_recovered=coverage.work_periods.recovered if coverage else None,
        jobs_total=coverage.work_periods.total if coverage else None,
    )
    return report


#: Which finding each check can raise, so a report can say what it looked at.
CHECK_CODES: dict[Check, FindingCode] = {
    check_text_layer: FindingCode.NO_TEXT_LAYER,
    check_columns: FindingCode.COLUMN_INTERLEAVING,
    check_unmapped_fonts: FindingCode.UNMAPPED_FONT,
    check_glyphs: FindingCode.BROKEN_GLYPHS,
    check_contacts: FindingCode.CONTACTS_NOT_TEXT,
    check_dates: FindingCode.DATES_NOT_EXTRACTABLE,
    check_tables: FindingCode.TEXT_IN_TABLES,
    check_sections: FindingCode.MISSING_SECTIONS,
}


def _codes_for(checks: Sequence[Check], format_noted: bool, compared: bool) -> list[FindingCode]:
    """The codes a given run was actually able to look for."""
    codes = [CHECK_CODES[check] for check in checks]
    if format_noted:
        codes.append(FindingCode.FORMAT_NOT_PDF)
    if compared:
        codes.append(FindingCode.CONTENT_LOST)
    return codes


def median_column_gap(pages: Sequence[PageFacts]) -> float:
    """Typical gutter width, for diagnostics. Zero when there is one column."""
    gaps = [
        page.columns[i + 1].left - page.columns[i].right
        for page in pages
        for i in range(len(page.columns) - 1)
    ]
    return statistics.median(gaps) if gaps else 0.0
