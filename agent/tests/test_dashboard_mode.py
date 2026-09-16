"""``agent.run --send --dashboard``: send only what the owner confirmed in the browser.

The invariant, tested at the one place the agent can act: **no application
leaves without a person's confirmation of that exact card.** In dashboard mode
that confirmation arrives from the backend instead of the keyboard, and the
tests below attack every way it could be stretched:

* an item nobody confirmed is not sent, and the terminal is not asked either;
* a confirmation given to a different letter mints nothing;
* the mandate is bound to the card digest the owner confirmed;
* without ``--dashboard`` a confirmation changes nothing: the person at the
  keyboard is still asked;
* a confirmation is not something a queue file can forge into a malformed
  shape — anything unreadable is no confirmation at all.
"""

import io
import json
from pathlib import Path
from typing import Any

import pytest

from agent import run
from agent.gate import SubmitGate
from agent.human import CancelledError, Candidate, accept_dashboard_confirmations
from agent.journal import Entry, Journal
from agent.letter import check as check_letter
from agent.mandate import SendMandate, digest, verify
from agent.queue import DashboardConfirmation, QueueItem
from agent.state import Actor, Status
from agent.tests.test_apply_flow import (
    PAGE_URL,
    VACANCY,
    FakePage,
    _install,
    a_state,
    ready,
)

__all__ = ["ready"]

LETTER = "Здравствуйте!"
CARD = "c" * 64


def _confirmation(letter: str | None = LETTER, card: str = CARD) -> dict[str, str]:
    return {
        "confirmed_at": "2026-09-16T13:00:00+00:00",
        "letter_digest": digest(letter),
        "card_digest": card,
    }


def _queue(tmp_path: Path, **item: Any) -> Journal:
    payload = {
        "vacancy_id": VACANCY,
        "url": PAGE_URL,
        "title": "Python-разработчик",
        "company": "Inspire",
        "letter": LETTER,
        **item,
    }
    (tmp_path / "queue.json").write_text(
        json.dumps({"version": 1, "items": [payload]}, ensure_ascii=False), encoding="utf-8"
    )
    return Journal(tmp_path / "agent.sqlite3")


def _results(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "queue-results.json"
    if not path.is_file():
        return []
    results: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["results"]
    return results


def _main(tmp_path: Path, *extra: str) -> int:
    return run.main(["--send", *extra, "--no-backend", "--queue", str(tmp_path / "queue.json")])


def _no_terminal(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Fail loudly if anything asks the person at the keyboard."""
    asked: list[str] = []

    def refuse(candidates: list[Candidate]) -> list[SendMandate]:
        asked.append("confirm")
        raise AssertionError("dashboard mode must not ask the terminal")

    monkeypatch.setattr(run, "confirm", refuse)
    return asked


def _page(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, journal: Journal) -> FakePage:
    gate = SubmitGate()
    page = FakePage(gate, a_state(), state_after_send=a_state(applied=True))
    _install(tmp_path, monkeypatch, journal, [page])
    monkeypatch.setattr(run, "SubmitGate", lambda: gate)
    return page


def test_a_confirmed_card_is_sent_without_asking_the_terminal(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _queue(tmp_path, confirmation=_confirmation())
    page = _page(monkeypatch, tmp_path, journal)
    asked = _no_terminal(monkeypatch)
    minted: list[SendMandate] = []
    real = accept_dashboard_confirmations

    def spy(*args: Any, **kwargs: Any) -> list[SendMandate]:
        mandates = real(*args, **kwargs)
        minted.extend(mandates)
        return mandates

    monkeypatch.setattr(run, "accept_dashboard_confirmations", spy)

    assert _main(tmp_path, "--dashboard") == 0

    assert asked == []
    assert page.sent
    assert [mandate.form_digest for mandate in minted] == [CARD]
    [result] = _results(tmp_path)
    assert result["status"] == "sent"
    assert result["sent_letter"] == LETTER


def test_an_item_nobody_confirmed_is_not_sent(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _queue(tmp_path)
    page = _page(monkeypatch, tmp_path, journal)
    asked = _no_terminal(monkeypatch)

    assert _main(tmp_path, "--dashboard") == 0

    assert asked == []
    assert not page.sent
    assert page.clicks == []
    assert page.visited == []
    assert "подтверждённых на дашборде откликов нет" in capsys.readouterr().out


def test_a_confirmation_of_another_letter_sends_nothing(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    journal = _queue(tmp_path, confirmation=_confirmation(letter="Другое письмо"))
    page = _page(monkeypatch, tmp_path, journal)
    _no_terminal(monkeypatch)

    assert _main(tmp_path, "--dashboard") == 0

    assert not page.sent
    assert page.visited == []
    out = capsys.readouterr().out
    assert "письмо не то" in out
    assert "Ничего не отправлено" in out


def test_without_the_flag_a_confirmation_still_asks_the_person(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _queue(tmp_path, confirmation=_confirmation())
    page = _page(monkeypatch, tmp_path, journal)
    asked: list[list[Candidate]] = []

    def declined(candidates: list[Candidate]) -> list[SendMandate]:
        asked.append(list(candidates))
        raise CancelledError("подтверждение не получено")

    monkeypatch.setattr(run, "confirm", declined)

    assert _main(tmp_path) == 0

    assert len(asked) == 1
    assert not page.sent


def test_a_vacancy_set_aside_earlier_is_requeued_as_the_persons_move(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dashboard confirmation served now was given after the agent's last report."""
    journal = _queue(tmp_path, confirmation=_confirmation())
    journal.record(Entry(VACANCY, Status.QUEUED), actor=Actor.AGENT)
    journal.record(Entry(VACANCY, Status.NEEDS_MANUAL, reason="нужен человек"), actor=Actor.AGENT)
    page = _page(monkeypatch, tmp_path, journal)
    _no_terminal(monkeypatch)

    assert _main(tmp_path, "--dashboard") == 0

    assert page.sent
    entry = journal.get(VACANCY)
    assert entry is not None
    assert entry.status is Status.SENT


def test_a_sent_vacancy_is_not_requeued_by_a_confirmation(
    ready: None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    journal = _queue(tmp_path, confirmation=_confirmation())
    journal.record(Entry(VACANCY, Status.QUEUED), actor=Actor.AGENT)
    journal.record(Entry(VACANCY, Status.CONFIRMED), actor=Actor.HUMAN)
    journal.record(Entry(VACANCY, Status.SENT), actor=Actor.AGENT)
    page = _page(monkeypatch, tmp_path, journal)
    _no_terminal(monkeypatch)

    assert _main(tmp_path, "--dashboard") == 0

    assert not page.sent


# ── the minting itself ────────────────────────────────────────────────


def _candidate(vacancy_id: str = VACANCY, letter: str | None = LETTER) -> Candidate:
    return Candidate(
        vacancy_id=vacancy_id,
        title="Python-разработчик",
        company="Inspire",
        url=f"https://almaty.hh.kz/vacancy/{vacancy_id}",
        letter=check_letter(letter, required=False),
    )


def _parsed(letter: str | None = LETTER, card: str = CARD) -> DashboardConfirmation:
    confirmation = DashboardConfirmation.from_json(_confirmation(letter, card))
    assert confirmation is not None
    return confirmation


def test_mandates_are_real_bound_to_the_card_and_spent_once() -> None:
    [mandate] = accept_dashboard_confirmations(
        [_candidate()], {VACANCY: _parsed()}, stream_out=io.StringIO()
    )

    assert mandate.form_digest == CARD
    assert mandate.letter == LETTER
    verify(mandate)
    with pytest.raises(PermissionError):
        verify(mandate)


def test_a_duplicate_is_still_a_cancellation() -> None:
    with pytest.raises(CancelledError):
        accept_dashboard_confirmations(
            [_candidate(), _candidate()], {VACANCY: _parsed()}, stream_out=io.StringIO()
        )


def test_only_confirmed_candidates_get_a_mandate() -> None:
    other = str(int(VACANCY) + 1)
    out = io.StringIO()

    mandates = accept_dashboard_confirmations(
        [_candidate(), _candidate(other)], {VACANCY: _parsed()}, stream_out=out
    )

    assert [mandate.vacancy_id for mandate in mandates] == [VACANCY]
    assert f"{other}: на дашборде не подтверждена" in out.getvalue()


def test_a_letterless_application_needs_a_confirmation_of_no_letter() -> None:
    [mandate] = accept_dashboard_confirmations(
        [_candidate(letter=None)], {VACANCY: _parsed(letter=None)}, stream_out=io.StringIO()
    )
    assert mandate.letter is None
    assert (
        accept_dashboard_confirmations(
            [_candidate(letter=None)], {VACANCY: _parsed()}, stream_out=io.StringIO()
        )
        == []
    )


@pytest.mark.parametrize(
    "payload",
    [
        None,
        "yes",
        {},
        {"confirmed_at": "x", "letter_digest": "short", "card_digest": CARD},
        {"confirmed_at": "x", "letter_digest": CARD, "card_digest": "Z" * 64},
        {"confirmed_at": 5, "letter_digest": CARD, "card_digest": CARD},
        {"confirmed_at": "x", "letter_digest": True, "card_digest": CARD},
    ],
)
def test_an_unreadable_confirmation_is_no_confirmation(payload: object) -> None:
    assert DashboardConfirmation.from_json(payload) is None
    item = QueueItem.from_json({"vacancy_id": VACANCY, "url": PAGE_URL, "confirmation": payload})
    assert item.confirmation is None
