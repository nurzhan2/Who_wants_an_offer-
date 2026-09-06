"""Logging in, once, by hand — and learning what "logged in" looks like.

The agent never sees the password. This script opens a visible browser on a
persistent profile, waits while the owner logs in themselves, and then records
one thing: which top-level keys of hh's page state appear for an authenticated
visitor and not for an anonymous one.

That recording is the point, and it is not obvious. Session cookies expire on
their own schedule, and the symptom is nasty: most of the site keeps working
while applications quietly stop going through. A health check therefore cannot
be "did the page load" — it has to be a fact that is *only* true when signed in.
Rather than guess which fact that is, this script measures it: it reads the
state of a vacancy page before the login and after it, and writes down what
appeared. ``agent/session.py`` then asserts exactly that.

Nothing here stores a credential. The profile directory holds the session,
Chromium owns it, and this program has no code that reads a cookie — deliberately
not ``context.cookies()``, not ``storage_state()``, not once.

    uv run python -m agent.login
"""

import json
import sys
from pathlib import Path
from typing import Any, Final

from agent.browser import open_browser
from agent.config import AGENT_DIR
from agent.state_page import read_state

#: A vacancy page is used rather than the account page because it is the page
#: the agent actually works on: the signal has to be one that is present exactly
#: where it will be checked.
PROBE_URL: Final[str] = "https://hh.kz/vacancy/136773120"

#: What the health check reads back. Gitignored: it names hh's internal keys and
#: is specific to one account's session shape.
SIGNAL_PATH: Final[Path] = AGENT_DIR / "session_signal.json"


def _top_level_keys(state: dict[str, Any]) -> set[str]:
    """The keys present and non-empty. Emptiness matters: hh ships null shells."""
    return {key for key, value in state.items() if value not in (None, {}, [], "")}


def main() -> int:
    """Open a browser, wait for the human, and record the authenticated signal."""
    print(
        "Откроется окно браузера. Войдите в аккаунт hh руками — пароль этот "
        "агент не видит и не хранит.\n"
        "Когда войдёте, вернитесь сюда и нажмите Enter.",
    )
    with open_browser() as context:
        page = context.new_page()
        page.goto(PROBE_URL, wait_until="domcontentloaded")
        before = _top_level_keys(read_state(page.content()) or {})
        print(f"\nДо входа страница отдала {len(before)} непустых ключей состояния.")
        print("Войдите в аккаунт в открытом окне, потом нажмите Enter здесь: ", end="")
        sys.stdout.flush()
        sys.stdin.readline()

        page.goto(PROBE_URL, wait_until="domcontentloaded")
        state = read_state(page.content())
        if state is None:
            print("Не удалось прочитать состояние страницы — hh изменил разметку.")
            return 2
        after = _top_level_keys(state)
        appeared = sorted(after - before)

        if not appeared:
            print(
                "\nНичего нового в состоянии не появилось. Похоже, вход не выполнен —\n"
                "проверьте, что в окне браузера вы действительно вошли, и запустите снова."
            )
            return 1

        SIGNAL_PATH.write_text(
            json.dumps(
                {
                    "probe_url": PROBE_URL,
                    "appeared_after_login": appeared,
                    "note": (
                        "Ключи, которых нет у анонимного посетителя. agent/session.py "
                        "проверяет их наличие перед каждым прогоном: если они пропали, "
                        "сессия протухла и отклики молча перестанут отправляться."
                    ),
                },
                ensure_ascii=False,
                indent=1,
            ),
            encoding="utf-8",
        )
        print(f"\nВход виден. Появились ключи: {', '.join(appeared[:8])}")
        print(f"Записано в {SIGNAL_PATH.name}. Профиль сохранён, повторный вход не нужен.")
    return 0


if __name__ == "__main__":  # pragma: no cover - a console entry point
    raise SystemExit(main())
