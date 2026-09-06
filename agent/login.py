"""Logging in, once, by hand — and learning what "logged in" looks like.

The agent never sees the password. This script opens a visible browser on a
persistent profile, waits while the owner logs in themselves, and then records
one thing: which top-level keys of hh's page state mean the visitor has an
account.

That recording is the point, and it is not obvious. Session cookies expire on
their own schedule, and the symptom is nasty: most of the site keeps working
while applications quietly stop going through. A health check therefore cannot
be "did the page load" — it has to be a fact that is *only* true when signed in.
Rather than guess which fact that is, this script measures it and
``agent/session.py`` asserts exactly what was written down. The two agree by
construction: this module owns both the field name (:data:`SIGNAL_FIELD`) and
the function that decides its contents (:func:`choose_signal`), and the health
check reads them rather than repeating them.

**Why there is a fallback at all.** The original design was purely differential:
read the state before the login and after it, and record what appeared. On a
profile that is ALREADY signed in there is no difference — both reads come from
the same session — so the diff is empty for the exact opposite of the reason it
was checking for, and sign-in was never detected. :data:`AUTH_MARKERS` closes
that, and its own uncertainty is written down beside it rather than implied.

**The host is not written into this file any more.** It used to be a city
subdomain, hardcoded as a stopgap, and hh's redirect to the regional subdomain
after sign-in raised out of ``page.goto`` and killed the script. Both halves are
``agent/hosts.py``'s job now.

Nothing here stores a credential. The profile directory holds the session,
Chromium owns it, and this program has no code that reads a cookie — deliberately
not ``context.cookies()``, not ``storage_state()``, not once.

    uv run python -m agent.login
"""

import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, final

from agent.browser import open_browser
from agent.config import AGENT_DIR
from agent.hosts import NavigatedElsewhereError, open_hh_page, vacancy_url
from agent.state_page import read_state

#: The vacancy the signal is measured on. A vacancy page rather than the account
#: page because it is the page the agent actually works on: the signal has to be
#: one that is present exactly where it will be checked. Overridable from the
#: environment because any single vacancy is archived sooner or later, and an
#: archived one boots a different page — read here rather than through
#: ``agent/config.py`` because it is not a limit and not a secret, it is which
#: page to look at.
PROBE_VACANCY_ID: Final[str] = os.getenv("AGENT_PROBE_VACANCY", "").strip() or "136773120"

#: Built from ``hh_sites.yaml`` rather than written down. Kept as a module
#: constant because ``agent/session.py`` falls back to it for signal files
#: written before the URL was recorded in them.
PROBE_URL: Final[str] = vacancy_url(PROBE_VACANCY_ID)

#: What the health check reads back. Gitignored: it names hh's internal keys and
#: is specific to one account's session shape.
SIGNAL_PATH: Final[Path] = AGENT_DIR / "session_signal.json"

#: The one field ``agent/session.py`` asserts. Named here, and imported there,
#: so the writer and the reader cannot drift apart silently.
SIGNAL_FIELD: Final[str] = "appeared_after_login"

#: Keys that carry content only for a signed-in applicant.
#:
#: EVIDENCE. Measured 2026-09-06 on the owner's profile, on live vacancy pages:
#: anonymously hh ships these as empty shells, which :func:`_top_level_keys`
#: already drops, and under the account they carry the applicant's own data —
#: ``applicantVacancyResponseStatuses`` holds the negotiation records for the
#: vacancy on screen, ``userLabelsForVacancies`` the owner's marks on it.
#:
#: UNCERTAINTY, written down because it is real. This was chosen from ONE
#: account on ONE day. There is no second account to cross-check it against, so
#: what is actually established is "these two keys distinguish signed-in from
#: anonymous for this profile", not "these two keys are hh's authentication
#: signal". If hh renames one, the health check reports an expired session for a
#: perfectly good login — which is the safe direction to be wrong in, and is why
#: :func:`choose_signal` prefers a measured before/after difference over this
#: constant whenever a difference exists.
AUTH_MARKERS: Final[frozenset[str]] = frozenset(
    {"applicantVacancyResponseStatuses", "userLabelsForVacancies"}
)


@final
@dataclass(frozen=True, slots=True)
class Signal:
    """What will be asserted before every run, and how it was arrived at."""

    #: The keys ``agent/session.py`` will require to be present and non-empty.
    keys: tuple[str, ...]
    #: ``difference`` (measured against an anonymous read), ``markers`` (the
    #: profile was already signed in, so there was nothing to diff), or
    #: ``none`` (no sign of an account at all).
    method: str
    #: Whether :data:`AUTH_MARKERS` actually turned up in this measurement.
    markers_confirmed: bool
    #: Everything else that appeared, recorded for the person reading the file
    #: and deliberately NOT asserted: hh ships plenty of per-page keys that
    #: differ between two loads of the same URL.
    also_appeared: tuple[str, ...] = ()

    @property
    def signed_in(self) -> bool:
        """Whether this measurement saw an account at all."""
        return bool(self.keys)


def _top_level_keys(state: dict[str, Any]) -> set[str]:
    """The keys present and non-empty. Emptiness matters: hh ships null shells."""
    return {key for key, value in state.items() if value not in (None, {}, [], "")}


def choose_signal(before: set[str], after: set[str]) -> Signal:
    """Decide what "signed in" means for this profile, from what was measured.

    A pure function so the decision can be tested without a browser, which is
    the only way this particular piece of reasoning ever gets exercised: the
    measurement itself needs an account and a window.

    The order is deliberate. A before/after difference is a real measurement and
    wins when there is one; :data:`AUTH_MARKERS` is a named guess with evidence
    behind it and is used only when there is nothing to diff, which happens
    whenever the profile was already signed in. When both are available the
    difference is narrowed to the markers it confirms — a whole diff would
    include per-page keys that legitimately vary between two loads, and
    asserting those turns an ordinary page change into "your session expired".
    """
    appeared = after - before
    if appeared:
        confirmed = AUTH_MARKERS & appeared
        keys = sorted(confirmed) if confirmed else sorted(appeared)
        return Signal(
            keys=tuple(keys),
            method="difference",
            markers_confirmed=bool(confirmed),
            also_appeared=tuple(sorted(appeared - set(keys))),
        )
    markers = sorted(AUTH_MARKERS & after)
    return Signal(
        keys=tuple(markers),
        method="markers" if markers else "none",
        markers_confirmed=bool(markers),
    )


def signal_payload(signal: Signal, probe_url: str = PROBE_URL) -> dict[str, Any]:
    """The file ``agent/session.py`` reads back, contents and provenance.

    ``Any`` because this is a JSON document with mixed value types and its one
    load-bearing field is named by :data:`SIGNAL_FIELD` rather than guessed at
    by the reader.
    """
    return {
        "probe_url": probe_url,
        SIGNAL_FIELD: list(signal.keys),
        "how_chosen": signal.method,
        "auth_markers_confirmed": signal.markers_confirmed,
        "also_appeared_not_asserted": list(signal.also_appeared),
        "note": (
            "Ключи, которых нет у анонимного посетителя. agent/session.py "
            "проверяет их наличие перед каждым прогоном: если они пропали, "
            "сессия протухла и отклики молча перестанут отправляться."
        ),
        "uncertainty": (
            "how_chosen=markers означает, что профиль уже был авторизован и "
            "сравнивать было не с чем — признак взят из измеренного списка "
            "AUTH_MARKERS, проверенного на одном аккаунте. Второго аккаунта "
            "для перепроверки нет."
        ),
    }


def main() -> int:
    """Open a browser, wait for the human, and record the authenticated signal."""
    print(
        "Откроется окно браузера. Войдите в аккаунт hh руками — пароль этот "
        "агент не видит и не хранит.\n"
        "Когда войдёте, вернитесь сюда и нажмите Enter.",
    )
    with open_browser() as context:
        page = context.new_page()
        try:
            open_hh_page(page, PROBE_URL, expect_vacancy=PROBE_VACANCY_ID)
        except NavigatedElsewhereError as error:
            print(f"\n{error}")
            print(
                "Похоже, вакансия для измерения больше не открывается. Укажите другую:\n"
                "  AGENT_PROBE_VACANCY=<id> uv run python -m agent.login"
            )
            return 2
        before = _top_level_keys(read_state(page.content()) or {})
        print(f"\nДо входа страница отдала {len(before)} непустых ключей состояния.")
        print("Войдите в аккаунт в открытом окне, потом нажмите Enter здесь: ", end="")
        sys.stdout.flush()
        sys.stdin.readline()

        # The navigation that used to end the script: signing in sets a regional
        # cookie, hh redirects to the city subdomain, and Playwright reports the
        # interrupted navigation as an error. agent/hosts.py absorbs exactly
        # that and still checks that the page which loaded is this vacancy.
        try:
            landed = open_hh_page(page, PROBE_URL, expect_vacancy=PROBE_VACANCY_ID)
        except NavigatedElsewhereError as error:
            print(f"\n{error}")
            return 2
        state = read_state(page.content())
        if state is None:
            print("Не удалось прочитать состояние страницы — hh изменил разметку.")
            return 2
        signal = choose_signal(before, _top_level_keys(state))

        if not signal.signed_in:
            print(
                "\nПризнаков аккаунта на странице нет. Похоже, вход не выполнен —\n"
                "проверьте, что в окне браузера вы действительно вошли, и запустите снова."
            )
            return 1

        SIGNAL_PATH.write_text(
            json.dumps(signal_payload(signal, landed), ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        print(f"\nВход виден. Признаки аккаунта: {', '.join(signal.keys[:8])}")
        if signal.method == "markers":
            print(
                "Профиль был авторизован ещё до запуска, сравнивать было не с чем —\n"
                "признак взят из измеренного списка AUTH_MARKERS."
            )
        print(f"Записано в {SIGNAL_PATH.name}. Профиль сохранён, повторный вход не нужен.")
    return 0


if __name__ == "__main__":  # pragma: no cover - a console entry point
    raise SystemExit(main())
