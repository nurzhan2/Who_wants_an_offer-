"""Measure hh's catalogue by profession, and check the plan built on it.

    uv run python scripts/probe_hh_roles.py --slug programmist
    uv run python scripts/probe_hh_roles.py --keyword python --keyword docker
    uv run python scripts/probe_hh_roles.py --files 2 --no-roles
    uv run python scripts/probe_hh_roles.py --json docs/hh-probe.json

This measured the way in by profession on 2026-09-08 and the crawl was built on
what it found; see ``app/sources/hh_probe.py`` for the numbers and for the two
jobs it has now that the answer is known -- watching those facts stay true, and
printing the profile-to-slugs plan the production code would build so that the
transliteration step can be checked against the live site.

Pick the slug deliberately. The first run of this opened ``digital-analitik``, a
profession the city barely hires for, and concluded from that one page that
catalogue pages do not list vacancies; ``programmist`` answered the other way.

It changes nothing and stores nothing. Every request goes through the hh
connector's own client, so robots.txt, the ban on query strings and the measured
rate of one page every four to five seconds all apply: a full run reads the
sitemap index, every catalogue file, one catalogue page and one dictionary,
which is about a minute of polite crawling. Nothing here goes near
``/search/vacancy``, and nothing here goes faster.
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
    CrawlPlan,
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
    parser.add_argument(
        "--keyword",
        action="append",
        dest="keywords",
        help="a keyword the planner would send; repeatable, turns on the plan section",
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


def show_plan(plan: CrawlPlan | None) -> None:
    """What the crawl would open for this profile, role by role."""
    print()
    print(RULE)
    print("4. ПЛАН ОБХОДА ДЛЯ ЭТОГО ПРОФИЛЯ")
    print(RULE)
    if plan is None:
        print("  не построен: нужны --keyword и прочитанный список слагов (см. ЗАМЕЧАНИЯ)")
        return
    print(f"  ключевые слова: {', '.join(plan.keywords)}")
    print(f"  семейства из hh_roles.yaml: {', '.join(plan.families) or 'ни одного'}")
    print(f"  всего страниц каталога в плане: {plan.total}")
    if not plan.roles:
        print("  ролей не выбрано: слаги берутся только по ключевым словам профиля")
    for role in plan.roles:
        print(f"    {role.id:>5}  {role.name} -- слагов {len(role.slugs)}")
        for slug in role.slugs[:5]:
            print(f"           {slug}")
        if not role.slugs:
            # The finding this section exists to surface: hh names that work in
            # a way the transliteration did not recognise.
            print("           НИ ОДНОГО: проверить термины в hh_roles.yaml")
    print(f"  слагов по ключевым словам, без роли: {len(plan.by_keyword)}")
    for slug in plan.by_keyword[:MAX_LINES]:
        print(f"    {slug}")


def verdict(page: CatalogPage | None) -> str:
    """Whether the catalogue still works, decided by the rule printed beside it.

    Deliberately three answers and not two. "Not a listing" is a real outcome --
    a rare profession has almost no postings, and the first run of this probe
    read exactly that on ``digital-analitik`` and reported the catalogue as a
    dead end. It was not; ``programmist`` carried 50. So the middle answer sends
    the reader back to a profession the city actually hires for instead of
    letting one page decide.
    """
    if page is None:
        return "не измерено: страница каталога не прочитана"
    ids = len(page.ids_in_document)
    if ids >= IDS_FOR_A_LIST:
        paging = (
            "нашлась пагинация без строки запроса -- это новость, "
            "глубину можно брать не только широтой слагов"
            if page.links_without_query
            else "пагинации без строки запроса нет, как и было замерено 2026-09-08"
        )
        return (
            f"страница отдаёт список вакансий ({ids} id, порог {IDS_FOR_A_LIST}); {paging}. "
            "Обход по профессии работает: слаг -> страница каталога -> id -> /vacancy/{id}"
        )
    if ids == 0:
        return (
            "на странице нет ни одного id вакансии. Если это не редкая профессия "
            "(перепроверить на programmist), то каталог перестал отдавать списки, "
            "обход по профессии молча выродился в обход по дате -- чинить hh.py"
        )
    return (
        f"на странице {ids} id вакансий при пороге {IDS_FOR_A_LIST}: это не список. "
        "Скорее всего редкая профессия -- повторить на programmist, прежде чем решать"
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
    show_plan(report.plan)

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
            keywords=tuple(args.keywords or ()),
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
