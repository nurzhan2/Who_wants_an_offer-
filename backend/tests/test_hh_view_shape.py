"""hh moved the posting under ``vacancyView.vacancyFull.vacancy`` (7 Oct 2026).

Measured on live pages 137625078 and 136105998. Before the move the fields sat
directly in ``vacancyView``; every other fixture in this suite has that shape,
so these tests pin both readings rather than replace one with the other.
"""

from app.sources.hh import HHVacancyView, _vacancy_view

INNER = {
    "vacancyId": 136105998,
    "name": "Golang разработчик (senior)",
    "publicationTimeIso": "2026-08-11T08:34:36.363+03:00",
    "validToTimeIso": "2026-09-10T08:34:42.54+03:00",
    "status": {"archived": True, "disabled": False},
    "closedForApplicants": False,
    "workExperience": "between3And6",
}


def nested(inner: dict[str, object]) -> dict[str, object]:
    return {"vacancyView": {"translations": {"x": 1}, "vacancyFull": {"vacancy": inner}}}


def test_the_nested_posting_is_unwrapped_and_validates() -> None:
    view = HHVacancyView.model_validate(_vacancy_view(nested(dict(INNER))))
    assert view.vacancy_id == 136105998
    assert view.status is not None and view.status.archived is True
    assert view.published_at is not None and view.published_at.year == 2026
    assert view.expires_at is not None and view.expires_at.month == 9


def test_translations_one_level_up_are_carried_into_the_view() -> None:
    assert _vacancy_view(nested(dict(INNER)))["translations"] == {"x": 1}


def test_the_flat_shape_from_before_7_october_is_returned_unchanged() -> None:
    flat = {"vacancyId": 1, "name": "x", "publicationDate": "2026-01-01T00:00:00+00:00"}
    assert _vacancy_view({"vacancyView": flat}) is flat


def test_an_empty_or_absent_view_stays_empty_so_the_quiet_path_still_runs() -> None:
    assert _vacancy_view({}) is None
    assert _vacancy_view({"vacancyView": {}}) == {}


def test_a_vacancy_full_without_a_posting_is_not_guessed_at() -> None:
    odd = {"vacancyFull": {"vacancy": None}, "name": "x"}
    assert _vacancy_view({"vacancyView": odd}) is odd


def test_an_old_spelling_present_is_not_overwritten_by_the_new_one() -> None:
    inner = dict(INNER, publicationDate="2020-01-01T00:00:00+00:00")
    assert _vacancy_view(nested(inner))["publicationDate"] == "2020-01-01T00:00:00+00:00"
