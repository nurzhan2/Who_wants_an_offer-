"""Generate the resume fixtures the extractor tests read.

Run with ``uv run python scripts/make_resume_fixtures.py``.

The fixtures are checked into the repository; this script exists so they can be
regenerated when the tests need a new shape, not so they can be built on the
fly. Six files, each aimed at one thing that breaks a naive extractor:

``single_column_ru.pdf``
    The easy case. If this one fails, nothing else is worth debugging.
``two_column_ru.pdf``
    The case the whole extraction phase is designed around. A narrow sidebar
    holds contacts, skills and languages; the wide column holds the jobs. Any
    line-oriented text extraction reads straight across the page and shuffles
    the sidebar into the work history, which is why resumes are handed to the
    model as PDF documents rather than as pre-extracted text.
``english.pdf`` / ``mixed_ru_en.pdf``
    Language handling, including the common Russian resume that writes prose in
    Russian and job titles and technology names in English.
``with_table.docx``
    Most of the content, the skills list included, lives inside a table. This
    is the fixture that catches an extractor which only walks ``doc.paragraphs``.
``plain.txt``
    UTF-8 text, no layout at all.

Every fixture carries the same four traps on purpose, because the tests assert
on them: overlapping employment (a full-time job and a freelance one running at
the same time, so summing durations gives the wrong total), a job that is still
current, near-duplicate skills that canonicalise to one entry ("Python" and
"Python 3", "Postgres" and "PostgreSQL"), and a city, salary expectation and
languages with levels.

Every person, phone number, address and employer here is invented. Emails use
the reserved ``example.com`` domain and phone numbers are all-zero placeholders.

**Deterministic.** Two runs produce byte-identical files, so regenerating never
shows up as a diff. That takes explicit work in both writers: reportlab stamps a
creation date and a document id into every PDF (defused with ``invariant``), and
python-docx writes the current wall clock both into the core properties and into
the zip entry headers (defused by rewriting the archive with a frozen date).

``two_column_ru.expected.json`` sits beside the fixtures and is deliberately
NOT written here: it is the expected extraction result, maintained by hand.
"""

# The fixtures are Russian resumes, so most string literals below are Cyrillic and
# RUF001 flags every letter that has a Latin lookalike. The confusion it guards
# against cannot happen here: this file writes text, it never compares it.
# ruff: noqa: RUF001

import io
import json
import zipfile
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
from docx import Document
from docx.document import Document as DocxDocument
from docx.shared import Pt
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import simpleSplit
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

logger = structlog.get_logger(__name__)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "backend" / "tests" / "fixtures" / "resumes"

PAGE_WIDTH, PAGE_HEIGHT = A4

FONT = "ResumeSans"
FONT_BOLD = "ResumeSans-Bold"

#: Regular/bold pairs to look for, in preference order. The built-in Type 1
#: faces are Latin-only, so a Cyrillic-capable TTF has to be embedded; the file
#: differs per platform but the glyphs are what matter, not the typeface.
FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "DejaVuSans-Bold.ttf"),
    ("C:/Windows/Fonts/arial.ttf", "arialbd.ttf"),
    ("/Library/Fonts/Arial.ttf", "Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "Arial Bold.ttf"),
)

#: Frozen clock for the DOCX core properties and zip entries.
FIXED_TIME = datetime(2024, 1, 1, tzinfo=UTC)
ZIP_TIME = (2024, 1, 1, 0, 0, 0)


# ── page model ────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Block:
    """One paragraph of a PDF column, wrapped to the column width when drawn."""

    text: str
    bold: bool = False
    size: float = 9.0
    space_before: float = 0.0


def head(text: str, *, size: float = 9.5, space_before: float = 8.0) -> Block:
    """A bold section heading."""
    return Block(text, bold=True, size=size, space_before=space_before)


def line(text: str, *, space_before: float = 0.0) -> Block:
    """A body line."""
    return Block(text, space_before=space_before)


def draw_column(
    canvas: Canvas,
    blocks: Iterable[Block],
    *,
    x: float,
    top: float,
    width: float,
    leading: float,
) -> float:
    """Draw wrapped blocks down a column and return the y the column ended at."""
    y = top
    for block in blocks:
        y -= block.space_before
        font = FONT_BOLD if block.bold else FONT
        for text in simpleSplit(block.text, font, block.size, width):
            canvas.setFont(font, block.size)
            canvas.drawString(x, y, text)
            y -= leading
    return y


def render_pdf(draw: Callable[[Canvas], None]) -> bytes:
    """Render one page to bytes with the creation date and document id frozen."""
    buffer = io.BytesIO()
    # invariant=1 replaces the wall clock and the random /ID with fixed values.
    canvas = Canvas(buffer, pagesize=A4, invariant=1, pageCompression=1)
    draw(canvas)
    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def register_fonts() -> None:
    """Embed the first Cyrillic-capable TTF pair found on this machine."""
    for regular, bold_name in FONT_CANDIDATES:
        regular_path = Path(regular)
        bold_path = regular_path.with_name(bold_name)
        if regular_path.is_file() and bold_path.is_file():
            pdfmetrics.registerFont(TTFont(FONT, str(regular_path)))
            pdfmetrics.registerFont(TTFont(FONT_BOLD, str(bold_path)))
            return
    tried = ", ".join(candidate for candidate, _ in FONT_CANDIDATES)
    raise SystemExit(f"No Cyrillic-capable TTF found. Looked for: {tried}")


# ── fixture content ───────────────────────────────────────────────────
# Invented people, invented employers. See the module docstring.

SINGLE_COLUMN_RU: tuple[Block, ...] = (
    Block("Игорь Образцов", bold=True, size=16),
    Block("Data Engineer", size=11),
    line("Алматы, Казахстан  ·  i.obraztsov@example.com  ·  +7 700 000 00 11"),
    line("Готов к переезду: да  ·  Формат работы: гибрид"),
    line("Зарплатные ожидания: от 1 500 000 KZT"),
    head("О себе"),
    line(
        "Инженер данных с опытом в логистике и ритейле. Строю пайплайны сбора "
        "и нормализации данных, отвечаю за качество витрин и за то, чтобы "
        "аналитики получали цифры утром, а не через день."
    ),
    head("Опыт работы"),
    Block("ООО «Северный Компас» — Алматы", bold=True, space_before=2),
    line("Senior Data Engineer"),
    line("04.2022 — по настоящее время"),
    line("Витрины продаж на PostgreSQL, оркестрация на Airflow, потоковая"),
    line("загрузка из Kafka. Сократил время ночного пересчёта с 6 до 2 часов."),
    line("Стек: Python 3, Airflow, PostgreSQL, Kafka, dbt, Docker"),
    Block("Мастерская «Тихий Пеликан» (фриланс, part-time) — удалённо", bold=True, space_before=6),
    line("Data Engineer"),
    line("01.2021 — 09.2023"),
    line("Параллельно с основной работой: выгрузки для маркетплейсов, отчётность"),
    line("на Postgres, разовые миграции данных."),
    line("Стек: Python, Postgres, Pandas"),
    Block("ТОО «Гранит-Логистика» — Караганда", bold=True, space_before=6),
    line("Data Engineer"),
    line("08.2019 — 03.2022"),
    line("ETL из 1С и телеметрии транспорта, первая версия хранилища."),
    line("Стек: Python, PostgreSQL, Airflow"),
    Block("ООО «Ясный Ветер» — Караганда", bold=True, space_before=6),
    line("Аналитик данных"),
    line("06.2018 — 07.2019"),
    line("Отчётность в SQL, поддержка выгрузок для отдела закупок."),
    head("Навыки"),
    line("Python, Python 3, SQL, PostgreSQL, Postgres, Airflow, Kafka, dbt,"),
    line("Pandas, Docker, Git, Grafana"),
    head("Языки"),
    line("Русский — родной, Английский — B2, Казахский — A2"),
    head("Образование"),
    line("Северный политехнический институт, прикладная математика, 2018"),
)

TWO_COLUMN_SIDEBAR: tuple[Block, ...] = (
    head("КОНТАКТЫ", space_before=0),
    line("d.vymyslov@example.com"),
    line("+7 700 000 00 12"),
    line("Астана, Казахстан"),
    line("Переезд: нет"),
    line("Формат: гибрид"),
    head("ЗАРПЛАТА"),
    line("от 1 800 000 KZT"),
    head("НАВЫКИ"),
    line("Python"),
    line("Python 3"),
    line("FastAPI"),
    line("Django"),
    line("Postgres"),
    line("PostgreSQL"),
    line("SQLAlchemy"),
    line("Redis"),
    line("Kafka"),
    line("Docker"),
    line("Kubernetes"),
    line("Pytest"),
    line("Git"),
    line("CI/CD"),
    head("ЯЗЫКИ"),
    line("Русский — родной"),
    line("Английский — B2"),
    line("Казахский — B1"),
)

TWO_COLUMN_BODY: tuple[Block, ...] = (
    head("ОПЫТ РАБОТЫ", size=11, space_before=0),
    Block("ООО «Орбита-Технологии» — Астана", bold=True, space_before=4),
    line("Senior Python Developer"),
    line("03.2022 — по настоящее время"),
    line(
        "Биллинговый сервис на FastAPI и PostgreSQL: перевёл синхронный монолит "
        "на асинхронный стек, вынес расчёт тарифов в отдельный воркер, поднял "
        "покрытие тестами с 30 до 78 процентов."
    ),
    line("Стек: Python 3, FastAPI, PostgreSQL, Redis, Kafka, Docker"),
    Block("Студия «Пиксель-Мираж» (фриланс, part-time) — удалённо", bold=True, space_before=6),
    line("Backend-разработчик"),
    line("06.2021 — 12.2023"),
    line(
        "Параллельно с основной работой, 10-15 часов в неделю: небольшие "
        "магазины на Django, интеграции с платёжными шлюзами, поддержка."
    ),
    line("Стек: Django, Postgres, Docker"),
    Block("ТОО «Вымпел-Софт» — Караганда", bold=True, space_before=6),
    line("Python-разработчик"),
    line("09.2019 — 02.2022"),
    line(
        "Внутренний портал складского учёта: REST API, отчёты, миграция данных со старой системы."
    ),
    line("Стек: Python, Django, PostgreSQL, Redis"),
    Block("ООО «Кибер-Пасека» — Караганда", bold=True, space_before=6),
    line("Junior Python Developer"),
    line("04.2018 — 08.2019"),
    line("Скрипты сбора данных, доработки админки, дежурства по инцидентам."),
    head("ОБРАЗОВАНИЕ", size=11),
    line("Северный политехнический институт"),
    line("Информационные системы, бакалавр, 2018"),
)

ENGLISH: tuple[Block, ...] = (
    Block("Avery Testwood", bold=True, size=16),
    Block("Backend Engineer", size=11),
    line("Tbilisi, Georgia  ·  a.testwood@example.com  ·  +995 500 00 00 13"),
    line("Willing to relocate: yes  ·  Work format: fully remote"),
    line("Salary expectation: from 5 500 USD per month"),
    head("Summary"),
    line(
        "Backend engineer working on payment and marketplace systems. I like "
        "boring services: clear contracts, real tests, and dashboards that "
        "someone actually looks at."
    ),
    head("Experience"),
    Block("Fabrikam Labs — Tbilisi", bold=True, space_before=2),
    line("Senior Backend Engineer"),
    line("May 2022 — present"),
    line("Payments API on FastAPI and PostgreSQL, event delivery over Kafka."),
    line("Stack: Python 3, FastAPI, PostgreSQL, Kafka, Docker, Kubernetes"),
    Block("Northwind Puzzle Works (freelance, part-time) — remote", bold=True, space_before=6),
    line("Backend Developer"),
    line("February 2021 — November 2023"),
    line("Alongside the full-time role: small Django shops and data exports."),
    line("Stack: Django, Postgres, Celery"),
    Block("Contoso Analytics — Batumi", bold=True, space_before=6),
    line("Backend Developer"),
    line("July 2019 — April 2022"),
    line("Reporting service, SQL tuning, migration off a legacy PHP monolith."),
    line("Stack: Python, PostgreSQL, Redis"),
    Block("Acme Robotics — Batumi", bold=True, space_before=6),
    line("Junior Developer"),
    line("March 2018 — June 2019"),
    line("Internal tooling, scheduled jobs, on-call rotation."),
    head("Skills"),
    line("Python, Python 3, FastAPI, Django, Postgres, PostgreSQL, SQLAlchemy,"),
    line("Redis, Kafka, Celery, Docker, Kubernetes, Git, CI/CD"),
    head("Languages"),
    line("English — C1, Russian — native, Georgian — A2"),
    head("Education"),
    line("Riverside Institute of Technology, Computer Science, 2018"),
)

MIXED_RU_EN: tuple[Block, ...] = (
    Block("Елена Примерова", bold=True, size=16),
    Block("Senior Python Developer / Team Lead", size=11),
    line("Тбилиси, Грузия  ·  e.primerova@example.com  ·  +995 500 00 00 14"),
    line("Готова к переезду: да  ·  Формат работы: remote"),
    line("Зарплатные ожидания: от 6 000 USD"),
    head("О себе"),
    line(
        "Пишу backend на Python восемь лет, последние два года веду команду из "
        "четырёх человек. Люблю понятные контракты между сервисами и "
        "code review без драмы."
    ),
    head("Опыт работы"),
    Block("Fabrikam Labs — Тбилиси", bold=True, space_before=2),
    line("Team Lead, Backend"),
    line("05.2022 — по настоящее время"),
    line("Отвечаю за payments API: FastAPI, PostgreSQL, доставка событий в Kafka."),
    line("Стек: Python 3, FastAPI, PostgreSQL, Kafka, Docker, Kubernetes"),
    Block("Northwind Puzzle Works (freelance, part-time) — удалённо", bold=True, space_before=6),
    line("Backend Developer"),
    line("02.2021 — 11.2023"),
    line("Параллельно с основной работой: небольшие проекты на Django и Celery."),
    line("Стек: Django, Postgres, Celery"),
    Block("Contoso Analytics — Батуми", bold=True, space_before=6),
    line("Backend Developer"),
    line("07.2019 — 04.2022"),
    line("Сервис отчётности, оптимизация SQL, уход от legacy-монолита."),
    line("Стек: Python, PostgreSQL, Redis"),
    Block("Acme Robotics — Батуми", bold=True, space_before=6),
    line("Junior Python Developer"),
    line("03.2018 — 06.2019"),
    line("Внутренние инструменты, регулярные задачи, дежурства."),
    head("Навыки"),
    line("Python, Python 3, FastAPI, Django, Postgres, PostgreSQL, SQLAlchemy,"),
    line("Redis, Kafka, Celery, Docker, Kubernetes, Git, CI/CD"),
    head("Языки"),
    # Latin and Cyrillic only: the embedded face is whatever Arial-class font
    # the machine has, and Georgian glyphs would come out as empty boxes.
    line("Русский — родной, English — C1, Грузинский — A2"),
    head("Образование"),
    line("Riverside Institute of Technology, Computer Science, 2018"),
)

PLAIN_TXT = """\
Кирилл Макетов
Python-разработчик

Город: Астана, Казахстан
Email: k.maketov@example.com
Телефон: +7 700 000 00 15
Переезд: рассмотрю
Формат работы: удалённо
Зарплатные ожидания: от 1 400 000 KZT

О СЕБЕ
Бэкенд-разработчик, шесть лет в вебе. Пишу сервисы на Python, довожу их
до продакшена и потом сам же их поддерживаю.

ОПЫТ РАБОТЫ

ООО «Медный Грифон» — Астана
Python-разработчик
07.2022 — по настоящее время
Сервис уведомлений на FastAPI, очереди на Redis, интеграции с CRM.
Стек: Python 3, FastAPI, PostgreSQL, Redis, Docker

Агентство «Бумажный Кот» (фриланс, part-time) — удалённо
Backend-разработчик
03.2021 — 05.2023
Параллельно с основной работой: сайты и админки на Django, разовые интеграции.
Стек: Django, Postgres, Docker

ТОО «Сонный Экскаватор» — Павлодар
Python-разработчик
02.2020 — 06.2022
Внутренний биллинг, отчёты, миграция данных.
Стек: Python, Django, PostgreSQL

ООО «Ржавый Компас» — Павлодар
Junior Python Developer
09.2018 — 01.2020
Поддержка legacy-кода, скрипты выгрузок, мониторинг.

НАВЫКИ
Python, Python 3, Django, FastAPI, Postgres, PostgreSQL, SQLAlchemy, Redis,
Docker, Git, Pytest, Linux

ЯЗЫКИ
Русский — родной
Английский — B1
Казахский — B2

ОБРАЗОВАНИЕ
Северный политехнический институт, программная инженерия, 2018
"""

#: The DOCX body. Everything here goes into a two-column table, including the
#: skills row: an extractor that only walks ``doc.paragraphs`` sees the name
#: and nothing else.
DOCX_ROWS: tuple[tuple[str, str], ...] = (
    ("Должность", "Python-разработчик / Backend Developer"),
    ("Город", "Караганда, Казахстан"),
    ("Email", "n.shablonova@example.com"),
    ("Телефон", "+7 700 000 00 16"),
    ("Переезд", "готова к переезду"),
    ("Формат работы", "гибрид"),
    ("Зарплатные ожидания", "от 1 200 000 KZT"),
    (
        "О себе",
        "Бэкенд-разработчик, шесть лет коммерческого опыта. Веб-сервисы на "
        "Python, интеграции с внешними API, поддержка продакшена.",
    ),
    (
        "ООО «Бронзовый Улей» — Караганда\nPython-разработчик\n08.2022 — по настоящее время",
        "Сервис заказов на FastAPI и PostgreSQL, асинхронные интеграции с "
        "платёжными шлюзами, перенос отчётов в отдельный воркер.\n"
        "Стек: Python 3, FastAPI, PostgreSQL, Redis, Docker",
    ),
    (
        "Студия «Лунный Огурец» (фриланс, part-time) — удалённо\n"
        "Backend-разработчик\n04.2021 — 10.2023",
        "Параллельно с основной работой, около 12 часов в неделю: интернет-"
        "магазины на Django, доработки админок, разовые выгрузки.\n"
        "Стек: Django, Postgres, Celery",
    ),
    (
        "ТОО «Хрустальный Молот» — Астана\nPython-разработчик\n05.2020 — 07.2022",
        "Внутренняя CRM: REST API, права доступа, отчёты для отдела продаж.\n"
        "Стек: Python, Django, PostgreSQL, Redis",
    ),
    (
        "ООО «Тёплый Радиатор» — Астана\nJunior Python Developer\n09.2018 — 04.2020",
        "Поддержка legacy-кода, скрипты обмена данными, мониторинг задач.\n"
        "Стек: Python, PostgreSQL",
    ),
    (
        "Навыки",
        "Python, Python 3, Django, FastAPI, Postgres, PostgreSQL, SQLAlchemy, "
        "Celery, Redis, Docker, Git, Pytest, Linux",
    ),
    ("Языки", "Русский — родной\nАнглийский — B2\nКазахский — C1"),
    ("Образование", "Северный политехнический институт, информатика, 2018"),
)


# ── builders ──────────────────────────────────────────────────────────


def build_single_page(blocks: Sequence[Block]) -> bytes:
    """One full-width column on one page."""

    def draw(canvas: Canvas) -> None:
        draw_column(
            canvas,
            blocks,
            x=48,
            top=PAGE_HEIGHT - 60,
            width=PAGE_WIDTH - 96,
            leading=12.4,
        )

    return render_pdf(draw)


def build_two_column() -> bytes:
    """Narrow sidebar plus wide body, both starting at the same height.

    The leadings differ by a fraction of a point on purpose. Sidebar and body
    lines therefore drift in and out of alignment down the page, so a
    line-oriented reader sometimes glues a sidebar entry onto a body line and
    sometimes emits it as its own line between two body lines. Both failure
    shapes appear in one fixture.
    """

    def draw(canvas: Canvas) -> None:
        canvas.setFont(FONT_BOLD, 16)
        canvas.drawString(48, PAGE_HEIGHT - 56, "Дмитрий Вымыслов")
        canvas.setFont(FONT, 11)
        canvas.drawString(48, PAGE_HEIGHT - 73, "Senior Python Developer")
        canvas.line(48, PAGE_HEIGHT - 82, PAGE_WIDTH - 48, PAGE_HEIGHT - 82)

        top = PAGE_HEIGHT - 102
        draw_column(canvas, TWO_COLUMN_SIDEBAR, x=48, top=top, width=140, leading=12.0)
        draw_column(canvas, TWO_COLUMN_BODY, x=214, top=top, width=333, leading=12.6)

    return render_pdf(draw)


def build_docx() -> bytes:
    """A resume whose content lives in a table, skills row included."""
    document: DocxDocument = Document()
    document.styles["Normal"].font.size = Pt(10)

    heading = document.add_paragraph()
    run = heading.add_run("Наталья Шаблонова")
    run.bold = True
    run.font.size = Pt(16)

    table = document.add_table(rows=0, cols=2)
    table.style = "Table Grid"
    for left, right in DOCX_ROWS:
        cells = table.add_row().cells
        cells[0].text = left
        cells[1].text = right

    document.core_properties.author = "resume fixture generator"
    document.core_properties.created = FIXED_TIME
    document.core_properties.modified = FIXED_TIME
    document.core_properties.last_modified_by = "resume fixture generator"

    buffer = io.BytesIO()
    document.save(buffer)
    return freeze_zip(buffer.getvalue())


def freeze_zip(raw: bytes) -> bytes:
    """Rewrite a zip with a fixed entry date so two runs produce equal bytes."""
    out = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(raw)) as source,
        zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as target,
    ):
        for name in sorted(source.namelist()):
            info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o600 << 16
            target.writestr(info, source.read(name))
    return out.getvalue()


def write(name: str, data: bytes) -> None:
    """Write a fixture, skipping the write when the bytes are unchanged."""
    path = OUT_DIR / name
    if path.is_file() and path.read_bytes() == data:
        logger.info("fixture.unchanged", file=name, bytes=len(data))
        return
    path.write_bytes(data)
    logger.info("fixture.written", file=name, bytes=len(data))


def main() -> None:
    """Regenerate every fixture in place."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    register_fonts()

    write("single_column_ru.pdf", build_single_page(SINGLE_COLUMN_RU))
    write("two_column_ru.pdf", build_two_column())
    write("english.pdf", build_single_page(ENGLISH))
    write("mixed_ru_en.pdf", build_single_page(MIXED_RU_EN))
    write("with_table.docx", build_docx())
    write("plain.txt", PLAIN_TXT.encode("utf-8"))

    expected = OUT_DIR / "two_column_ru.expected.json"
    if not expected.is_file():
        raise SystemExit(f"Missing hand-maintained reference: {expected}")
    json.loads(expected.read_text(encoding="utf-8"))
    logger.info("fixtures.done", directory=str(OUT_DIR))


if __name__ == "__main__":
    main()
