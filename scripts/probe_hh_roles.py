"""Stage 0 for reading hh by profession: run the measurement, print what it saw.

    uv run python scripts/probe_hh_roles.py
    uv run python scripts/probe_hh_roles.py --files 2 --no-roles
    uv run python scripts/probe_hh_roles.py --slug python-razrabotchik --json probe.json

The crawl is blind to the profession, and the reason is not a bug: a sitemap
entry is a URL and a date, so the role is only known after the request has been
spent. Whether there is a way in by profession turns on one fact nobody has
measured -- what hh's ``vacancies{N}.xml`` catalogue pages actually contain --
and this prints that fact.

It changes nothing and stores nothing. Every request goes through the hh
connector's own client, so robots.txt, the ban on query strings and the measured
rate of one page every four to five seconds all apply: a full run reads the
sitemap index, every catalogue file, one catalogue page and one dictionary,
which is about a minute of polite crawling. Nothing here goes near
``/search/vacancy``, and nothing here goes faster.

``--json`` writes the whole measurement to a file. That is the artefact
docs/SOURCES.md quotes, and it is also how the one question a single run cannot
answer -- whether a catalogue page's composition changes -- gets answered: dump
it, run it again tomorrow, diff the two.
"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from app.core.logging import configure_logging
from app.sources.hh import HHSite, HHSource, load_sites
from app.sources.hh_probe import (
    DEV_TERMS,
    CatalogIndex,
    CatalogPage,
    ProbeReport,
    RoleDirectory,
    probe,
)
from app.sources.http import close_client, get_client

RULE = "-" * 78  # ASCII: this report is printed to a cp1251 console

#: Distinct vacancy ids on one catalogue page above which the page is reporting
#: a list rather than mentioning a posting in passing. Stated as a number here
#: rather than left to the reader's eye, because the verdict below is only
#: honest if the rule that produced it is on the screen next to it.
IDS_FOR_A_LIST = 10

#: Lines of any list this report prints in full. The JSON dump carries the rest.
MAX_LINES = 40


def parse_args() -> argparse.Namespace:
    """Command line."""
    parser = argparse.ArgumentParser(description="Measure hh's catalogue pages by profession.")
    parser.add_argument("--host", help="hh host to read; defaults to the site marked default")
    parser.add_argument("--slug", help="catalogue slug to open instead of the first matched one")
    parser.add_argument("--files", type=int, help="read at most this many vacancies{N}.xml files")
    parser.add_argument(
        "--term",
        action="append",
        dest="terms",
        help="search term for the development family; repeatable, replaces the defaults",
    )
    parser.add_argument("--no-roles", action="store_true", help="skip api.hh.ru/professional_roles")
    parser.add_argument("--json", help="write the whole measurement here")
    return parser.parse_args()


def pick_site(sites: Sequence[HHSite], host: str | None) -> HHSite:
    """The host to measure: the one asked for, or the deployment's default."""
    if host:
        for site in sites:
            if site.host == host:
                return site
        known = ", ".join(site.host for site in sites) or "ни одного"
        raise SystemExit(f"хост {host} не описан в hh_sites.yaml; известны: {known}")
    for site in sites:
        if site.default:
            return site
    if sites:
        return sites[0]
    raise SystemExit("hh_sites.yaml не описывает ни одного сайта")


def show_index(index: CatalogIndex | None) -> None:
    """Question one: what the catalogue sitemaps hold."""
    print(RULE)
    print("1. ФАЙЛЫ vacancies{N}.xml")
    print(RULE)
    if index is None:
        print("  не прочитаны, см. ЗАМЕЧАНИЯ ниже")
        return
    print(f"  {'файл':<14} {'КБ':>8} {'<loc>':>8} {'слагов':>8} {'с lastmod':>10}")
    for entry in index.files:
        print(
            f"  {entry.name:<14} {entry.body_bytes / 1024:>8.0f} {entry.locs:>8} "
            f"{entry.slugs:>8} {entry.with_lastmod:>10}"
        )
    print()
    print(f"  всего слагов: {len(index.slugs)}")
    print(f"  похожих на разработку: {len(index.matched)}")
    if index.matched:
        print(f"  совпавшие слаги (первые {MAX_LINES}):")
        for slug in index.matched[:MAX_LINES]:
            print(f"    {slug}")


def show_page(page: CatalogPage | None) -> None:
    """Question two: what one catalogue page contains."""
    print()
    print(RULE)
    print("2. ОДНА СТРАНИЦА КАТАЛОГА")
    print(RULE)
    if page is None:
        print("  не прочитана, см. ЗАМЕЧАНИЯ ниже")
        return
    print(f"  {page.url}")
    print(f"  размер: {page.body_bytes / 1024:.0f} КБ")
    state = (
        "нет"
        if not page.has_state
        else ("есть, разобрано" if page.state_parsed else "есть, но не разбирается")
    )
    print(f"  HH-Lux-InitialState: {state}")
    if page.state_keys:
        print("  ключи состояния верхнего уровня (по убыванию размера):")
        for key in page.state_keys[:MAX_LINES]:
            size = "-" if key.size is None else str(key.size)
            print(f"    {key.name:<40} {key.kind:<6} {size:>8}")
    print(f"  id вакансий в документе: {len(page.ids_in_document)}")
    print(f"  id вакансий в состоянии: {len(page.ids_in_state)}")
    if page.ids_in_document:
        print(f"    примеры: {', '.join(page.ids_in_document[:10])}")
    print(f"  ссылки на каталог без строки запроса: {len(page.links_without_query)}")
    for link in page.links_without_query[:MAX_LINES]:
        print(f"    {link}")
    print(f"  ссылки на каталог со строкой запроса (нам закрыты): {len(page.links_with_query)}")
    for link in page.links_with_query[:10]:
        print(f"    {link}")


def show_roles(roles: RoleDirectory | None) -> None:
    """Question three: what hh's own directory calls these roles."""
    print()
    print(RULE)
    print("3. СПРАВОЧНИК api.hh.ru/professional_roles")
    print(RULE)
    if roles is None:
        print("  не прочитан, см. ЗАМЕЧАНИЯ ниже")
        return
    print(f"  всего ролей: {roles.total}")
    advertised = roles.advertised
    if advertised is None:
        print("  роли с id 96 в справочнике нет: догадка про roles=96 не подтвердилась")
    else:
        print(f"  id 96 = {advertised.name} (категория: {advertised.category or '-'})")
    print(f"  похожих на разработку: {len(roles.matched)}")
    for role in roles.matched[:MAX_LINES]:
        print(f"    {role.id:>5}  {role.name}")


def verdict(page: CatalogPage | None) -> str:
    """The fork in the brief, decided by the rule printed beside it.

    Deliberately three answers and not two. "Ambiguous" is a real outcome of
    this measurement -- a page carrying two vacancy links is not a listing --
    and collapsing it into either branch would be the guess the whole exercise
    exists to avoid.
    """
    if page is None:
        return "не измерено: страница каталога не прочитана"
    ids = len(page.ids_in_document)
    if ids >= IDS_FOR_A_LIST:
        paging = (
            "и есть пагинация без строки запроса"
            if page.links_without_query
            else ("но ссылок на следующие страницы без строки запроса не найдено")
        )
        return (
            f"страница отдаёт список вакансий ({ids} id, порог {IDS_FOR_A_LIST}) {paging}. "
            "Вход по профессии выглядит реализуемым: слаг -> страница каталога -> id -> "
            "/vacancy/{id}"
        )
    if ids == 0:
        return (
            "на странице нет ни одного id вакансии. Входа по профессии здесь нет; "
            "запасной путь -- обход как сейчас с ранним отсевом по professional_role_ids, "
            "и он НЕ экономит запросов"
        )
    return (
        f"на странице {ids} id вакансий при пороге {IDS_FOR_A_LIST}: это не список. "
        "Нужен человек: посмотреть сохранённый JSON и решить"
    )


def show(report: ProbeReport) -> None:
    """Print the measurement, in the order the brief asks its questions."""
    print(RULE)
    print("ЭТАП 0: ВХОД ПО ПРОФЕССИИ НА hh")
    print(RULE)
    print(f"  хост: {report.host}")
    print(f"  измерено: {report.measured_at.isoformat()}")
    print(f"  термины: {', '.join(report.terms)}")
    print()
    show_index(report.index)
    show_page(report.page)
    show_roles(report.roles)

    if report.notes:
        print()
        print(RULE)
        print("ЗАМЕЧАНИЯ")
        print(RULE)
        for note in report.notes:
            print(f"  {note}")

    print()
    print(RULE)
    print("ВЫВОД")
    print(RULE)
    print(f"  {verdict(report.page)}")
    print(RULE)


async def main() -> int:
    """Run it."""
    args = parse_args()
    configure_logging()
    site = pick_site(load_sites(), args.host)
    terms = tuple(args.terms) if args.terms else DEV_TERMS

    source = HHSource()
    client = get_client()
    source.bind(client.bind(source))
    try:
        report = await probe(
            source.http,
            site,
            terms=terms,
            slug=args.slug,
            max_files=args.files,
            read_roles_directory=not args.no_roles,
        )
    finally:
        await close_client()

    show(report)
    if args.json:
        Path(args.json).write_text(report.model_dump_json(indent=2), encoding="utf-8")
        print(f"  измерение записано в {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
