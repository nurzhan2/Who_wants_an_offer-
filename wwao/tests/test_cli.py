"""What the one entry point does, and the two things it must never do.

Nothing here starts a process, opens a browser, touches a database or reaches
the network: the runner and the queue transport are arguments to
:func:`wwao.cli.main`, so every route can be watched by handing it a list to
append to. The one thing that would talk to a backend — the HTTP transport
itself — is exercised against ``respx`` at the bottom of this file, so even that
never leaves the process. The separation between the two worlds is asserted next
door, in ``test_separation.py``, because it is a different kind of claim and is
proved a different way.
"""

import io
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final, final

import httpx
import pytest
import respx

from wwao import cli, queue_view
from wwao.cli import Fetcher, Runner
from wwao.console import printable
from wwao.queue_view import QueueNotBuiltError, QueueUnavailableError, fetch_over_http

pytestmark = pytest.mark.unit

VACANCY: Final[str] = "136773120"
PAGE_URL: Final[str] = f"https://almaty.hh.kz/vacancy/{VACANCY}"


@final
class Recorder:
    """Stands in for starting a child process, and remembers what it was asked."""

    def __init__(self, code: int = 0) -> None:
        self.commands: list[list[str]] = []
        self.code = code

    def __call__(self, command: Sequence[str]) -> int:
        self.commands.append(list(command))
        return self.code


@final
class Terminal(io.StringIO):
    """A stream that claims to be a terminal. What a person at a keyboard has."""

    def isatty(self) -> bool:
        """Yes."""
        return True


def a_queue(**overrides: object) -> dict[str, object]:
    """One queue entry in the shape the contract describes."""
    entry: dict[str, object] = {
        "vacancy_id": VACANCY,
        "url": PAGE_URL,
        "title": "Python-разработчик",
        "company": "Inspire",
        "letter": "Здравствуйте! Опыт — Python, FastAPI.",
        "score": 82.5,
        "score_explanation": "закрыто: Python, FastAPI\nне закрыто: Kubernetes",
    }
    entry.update(overrides)
    return entry


def write_queue(path: Path, *items: dict[str, object], version: int = 1) -> Path:
    """A queue file at ``path``. Returns it, so a call reads as an argument."""
    path.write_text(
        json.dumps({"version": version, "items": list(items)}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def run_cli(
    argv: Sequence[str],
    *,
    run: Runner | None = None,
    fetch: Fetcher | None = None,
    tty: bool = False,
) -> tuple[int, str, str]:
    """Drive the CLI with everything it talks to replaced. Returns code, out, err."""
    out = Terminal() if tty else io.StringIO()
    err = io.StringIO()
    stdin = Terminal() if tty else io.StringIO()
    code = cli.main(
        argv,
        run=run or Recorder(),
        fetch=fetch or _no_network,
        stdin=stdin,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def _no_network(base_url: str, limit: int) -> str:
    """The default transport in these tests: there isn't one."""
    raise AssertionError(f"тест не должен ходить в сеть: {base_url} limit={limit}")


# ── routing ───────────────────────────────────────────────────────────


def test_crawl_hands_the_rest_of_the_command_line_to_the_script_that_owns_it() -> None:
    """The CLI does not re-declare run_pipeline's flags, so they cannot drift.

    A router that copies the options of the thing it routes to is a router that
    goes stale on the first flag somebody adds to the script.
    """
    runner = Recorder()

    code = run_cli(["crawl", "--source", "hh", "--dry-run"], run=runner)[0]

    assert code == 0
    assert runner.commands == [
        [sys.executable, str(cli.SCRIPTS / "run_pipeline.py"), "--source", "hh", "--dry-run"]
    ]


def test_letters_runs_the_letter_script_and_passes_its_flags_through() -> None:
    """Same rule, and the script it names is the one the repository has."""
    runner = Recorder()

    run_cli(["letters", "--limit", "5", "--show"], run=runner)

    assert runner.commands == [
        [sys.executable, str(cli.SCRIPTS / "generate_letters.py"), "--limit", "5", "--show"]
    ]


def test_the_exit_code_of_the_wrapped_script_is_the_exit_code_of_the_cli() -> None:
    """Otherwise a nightly `wwao crawl` reports success on a failed crawl."""
    assert run_cli(["crawl"], run=Recorder(code=2))[0] == 2


def test_match_runs_the_scoring_script_and_passes_its_flags_through() -> None:
    """`match` was the honest hole in this pipeline until the scorer was written.

    The CLI used to answer it with EXIT_MISSING_PIECE and a paragraph naming
    the file it wanted, which was the right behaviour while there was nothing
    to run. There is now: scripts/run_matching.py scores the corpus against the
    active profile. The mechanism that reported the hole is still in place for
    the next one — this test only asserts that `match` is no longer it.
    """
    runner = Recorder()

    code, _, _ = run_cli(["match", "--limit", "50"], run=runner)

    assert code == 0
    assert runner.commands == [
        [sys.executable, str(cli.SCRIPTS / "run_matching.py"), "--limit", "50"]
    ]


def test_a_step_nobody_has_written_yet_still_says_so_and_runs_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The mechanism, not the step: no wrapped script is missing today.

    A "file not found" for a step nobody has written is a puzzle, so the
    message names the file expected and the exit code is its own — the answer
    is "write it", not "look at the logs". Asserted against a wrapper pointed
    at a path that does not exist, because asserting it against whichever step
    happens to be unwritten is a test that dies the moment somebody writes it.
    """
    runner = Recorder()
    monkeypatch.setattr(
        cli,
        "WRAPPED",
        (
            cli.Wrapped(
                name="crawl",
                script=cli.SCRIPTS / "no-such-step.py",
                summary="шаг, которого нет",
                missing="Этот шаг живёт в scripts/no-such-step.py.",
            ),
        ),
    )

    code, _, err = run_cli(["crawl"], run=runner)

    assert code == cli.EXIT_MISSING_PIECE
    assert runner.commands == []
    assert "no-such-step.py" in err


def test_no_subcommand_but_apply_can_reach_the_agent() -> None:
    """The one that sends is the one that is named, and nothing else names it."""
    for argv in (["crawl"], ["letters"], ["match"], ["queue", "--from", "нет-такого.json"]):
        runner = Recorder()
        run_cli(argv, run=runner)
        for command in runner.commands:
            assert cli.AGENT_MODULE not in command
            assert "--send" not in command


# ── apply: the only thing that sends ──────────────────────────────────


def test_apply_starts_the_agent_as_its_own_process() -> None:
    """Its own process, not an import. The two worlds do not share one.

    ``agent/tests/test_isolation.py`` already proves the agent imports nothing
    from ``app``; spawning it as ``python -m agent.run`` is what makes that
    proof cover this command as well.
    """
    runner = Recorder()

    code = run_cli(["apply", "--send"], run=runner, tty=True)[0]

    assert code == 0
    assert runner.commands == [[sys.executable, "-m", "agent.run", "--send"]]


def test_apply_without_send_is_a_dry_run() -> None:
    """The agent's own default, preserved rather than re-decided here."""
    runner = Recorder()

    run_cli(["apply"], run=runner, tty=True)

    assert runner.commands == [[sys.executable, "-m", "agent.run"]]


def test_apply_forwards_only_the_flags_it_declares() -> None:
    """A closed set: where the queue comes from, the manual file, requeue.

    ``--from`` and ``--no-backend`` say which list the agent is offered — the
    database's, or one file — and neither says anything about consent. They are
    forwarded rather than resolved here because the agent holds the same default,
    and one address kept in two places is how two places come to disagree.
    """
    runner = Recorder()

    run_cli(
        [
            "apply",
            "--send",
            "--from",
            "http://127.0.0.1:9000",
            "--queue",
            "q.json",
            "--requeue",
            "1",
            "2",
        ],
        run=runner,
        tty=True,
    )

    assert runner.commands == [
        [
            sys.executable,
            "-m",
            "agent.run",
            "--send",
            "--from",
            "http://127.0.0.1:9000",
            "--queue",
            "q.json",
            "--requeue",
            "1",
            "2",
        ]
    ]


def test_apply_can_be_told_to_work_off_the_file_alone() -> None:
    """The escape hatch reaches the agent, and it is the only way to the file.

    A run that fell back to ``agent/queue.json`` on its own would put a
    hand-written row in front of a person as though the pipeline had produced
    it, which is the defect this whole arrangement replaced.
    """
    runner = Recorder()

    run_cli(["apply", "--no-backend", "--queue", "q.json"], run=runner, tty=True)

    assert runner.commands == [
        [sys.executable, "-m", "agent.run", "--no-backend", "--queue", "q.json"]
    ]


def test_apply_refuses_a_flag_it_does_not_know_instead_of_forwarding_it() -> None:
    """This is what makes the set closed, and it is the whole guard.

    The wrapped subcommands forward everything, which is right for them: their
    flags belong to the script. For the subcommand that sends applications it
    would mean that the day somebody adds a way to skip the confirmation to the
    agent, it is reachable from here by default. It has to be added to this file
    instead — in front of the test below that forbids the vocabulary.
    """
    runner = Recorder()

    with pytest.raises(SystemExit) as exit_info:
        run_cli(["apply", "--send", "--yes"], run=runner)

    assert exit_info.value.code == 2
    assert runner.commands == []


def test_apply_will_not_start_where_nobody_can_answer_it() -> None:
    """A cron job, a pipe, a CI runner: no terminal, no card, no confirmation.

    ``agent.run`` already treats a closed stdin as a refusal, so nothing would
    have been sent either way. This refuses before the browser opens and says
    why, which is the difference between a nightly job that fails on its first
    line and one that fails after logging into somebody's hh account.
    """
    runner = Recorder()

    code, _, err = run_cli(["apply", "--send"], run=runner, tty=False)

    assert code == cli.EXIT_NO_HUMAN
    assert runner.commands == []
    assert "человека за клавиатурой" in err


def test_a_readable_stdout_alone_is_not_a_person() -> None:
    """Both streams. A card written to a file is a card nobody read.

    The confirmation is consent to a specific text; if the text went to a log
    and the answer came from a pipe, the word typed means nothing.
    """
    runner = Recorder()
    only_input = cli.main(
        ["apply", "--send"],
        run=runner,
        stdin=Terminal(),
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )
    only_output = cli.main(
        ["apply", "--send"],
        run=runner,
        stdin=io.StringIO(),
        stdout=Terminal(),
        stderr=io.StringIO(),
    )

    assert (only_input, only_output) == (cli.EXIT_NO_HUMAN, cli.EXIT_NO_HUMAN)
    assert runner.commands == []


def test_the_refusal_is_the_same_for_a_dry_run() -> None:
    """One rule a person can hold in their head, with no "unless" in it.

    A dry run sends nothing and would be harmless; «apply needs a terminal
    unless you left off the flag that sends» is not a rule anybody carries
    correctly. The unattended view of the same question is `queue`, which needs
    neither the agent's dependencies nor a browser.
    """
    assert run_cli(["apply"], tty=False)[0] == cli.EXIT_NO_HUMAN


def test_no_flag_anywhere_in_this_cli_could_answer_a_confirmation() -> None:
    """The vocabulary, not the behaviour: a guard against the flag being added.

    The task is explicit that if such a switch starts to look necessary the task
    has been misunderstood, so the refusal is written down where the next person
    to reach for one will run into it.
    """
    forbidden = (
        "yes",
        "all",
        "unattended",
        "auto",
        "force",
        "confirm",
        "skip",
        "batch",
        "headless",
        "daemon",
        "assume",
        "noninteractive",
    )

    options = _option_strings(cli.build_parser())

    assert options, "разбор командной строки не нашёлся — тест ничего не проверяет"
    for option in options:
        name = option.lstrip("-").replace("_", "-")
        assert not [word for word in forbidden if word in name], (
            f"флаг {option} умеет то, чего в этом CLI быть не должно: "
            "подтверждение отправки набирается руками и заменить его флагом нельзя"
        )


def _option_strings(parser: object) -> set[str]:
    """Every option of the parser and of every subparser under it.

    Reaches into argparse's private ``_actions`` because there is no public way
    to walk a parser, and a test that walked ``--help`` output would be testing
    the formatter.
    """
    found: set[str] = set()
    for action in getattr(parser, "_actions", ()):
        found.update(action.option_strings)
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for sub in choices.values():
                found |= _option_strings(sub)
    return found


# ── queue: what is ready, and why the rest is not ─────────────────────


def test_the_queue_shows_the_score_with_its_explanation(tmp_path: Path) -> None:
    """The number alone is not a reason to write to an employer."""
    queue = write_queue(tmp_path / "queue.json", a_queue())

    code, out, _ = run_cli(["queue", "--from", str(queue)])

    assert code == 0
    assert "82.5" in out
    assert "не закрыто: Kubernetes" in out
    assert "ГОТОВО К ОТКЛИКУ: 1" in out


def test_a_queue_with_no_scores_says_so_rather_than_showing_a_list(tmp_path: Path) -> None:
    """«Not scored» and «scored badly» must not look the same.

    A queue with no scores in it means the scoring step has not run, and then
    the order of the list means nothing — which is exactly the thing a person
    reading a ranked list will assume it does not mean.
    """
    queue = write_queue(tmp_path / "queue.json", a_queue(score=None, score_explanation=None))

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "оценка соответствия не посчитана" in out


def test_a_score_the_backend_sent_as_a_string_is_still_a_score(tmp_path: Path) -> None:
    """``Numeric(5, 2)`` arrives as a number or as "82.50" depending on the encoder."""
    queue = write_queue(tmp_path / "queue.json", a_queue(score="82.50"))

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "82.5" in out
    assert "не посчитана" not in out


def test_a_nonsense_score_reads_as_no_score_and_does_not_stop_the_report(
    tmp_path: Path,
) -> None:
    """One bad row must not be able to hide everything that is ready."""
    queue = write_queue(tmp_path / "queue.json", a_queue(score=120))

    code, out, _ = run_cli(["queue", "--from", str(queue)])

    assert code == 0
    assert "120.0" not in out
    assert "оценка соответствия не посчитана" in out


def test_the_prefilter_flags_are_the_reason_a_vacancy_is_not_ready(tmp_path: Path) -> None:
    """Three different reasons, three different sentences, all of them shown."""
    queue = write_queue(
        tmp_path / "queue.json",
        a_queue(vacancy_id="1", url="https://hh.kz/vacancy/1", archived=True),
        a_queue(vacancy_id="2", url="https://hh.kz/vacancy/2", closed_for_applicants=True),
        a_queue(vacancy_id="3", url="https://hh.kz/vacancy/3", external_application=True),
    )

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "НЕ ГОТОВО: 3" in out
    assert "вакансия в архиве" in out
    assert "работодатель закрыл приём откликов" in out
    assert "на сайте работодателя" in out


def test_every_reason_is_listed_rather_than_the_first_one_found(tmp_path: Path) -> None:
    """Fixing one of two blockers must not look like it should have helped."""
    queue = write_queue(tmp_path / "queue.json", a_queue(archived=True, closed_for_applicants=True))

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "вакансия в архиве" in out
    assert "работодатель закрыл приём откликов" in out


def test_what_the_last_run_concluded_is_why_a_vacancy_is_not_ready_now(
    tmp_path: Path,
) -> None:
    """Including hh's own sentence, which is the most specific thing anybody has.

    hh names the requirement that is unmet; this project's own score does not.
    So it is quoted, attributed, and kept out of the "ready" list.
    """
    queue = write_queue(tmp_path / "queue.json", a_queue())
    (tmp_path / "queue-results.json").write_text(
        json.dumps(
            {
                "version": 1,
                "results": [
                    {
                        "vacancy_id": VACANCY,
                        "status": "needs_manual",
                        "reason": "форма требует сопроводительное",
                        "hh_warning": "Такой отклик может получить отказ",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "НЕ ГОТОВО: 1" in out
    assert "нужен человек" in out
    assert "форма требует сопроводительное" in out
    assert "hh сказал: Такой отклик может получить отказ" in out


def test_a_vacancy_already_applied_to_is_not_offered_again(tmp_path: Path) -> None:
    """The queue file is hand-maintained, so a sent vacancy stays in it for ever."""
    queue = write_queue(tmp_path / "queue.json", a_queue())
    (tmp_path / "queue-results.json").write_text(
        json.dumps({"version": 1, "results": [{"vacancy_id": VACANCY, "status": "sent"}]}),
        encoding="utf-8",
    )

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "отклик уже отправлен" in out
    assert "ГОТОВО К ОТКЛИКУ: 0" in out


def test_the_report_says_when_it_has_no_run_history_to_go_on(tmp_path: Path) -> None:
    """Otherwise "everything is ready" reads as a fact rather than as an absence."""
    queue = write_queue(tmp_path / "queue.json", a_queue())

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "результатов прошлых прогонов нет" in out


def test_a_missing_letter_is_a_note_and_not_a_refusal(tmp_path: Path) -> None:
    """hh accepts an application without a cover letter; the agent measured it."""
    queue = write_queue(tmp_path / "queue.json", a_queue(letter=None))

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert "ГОТОВО К ОТКЛИКУ: 1" in out
    assert "письма нет" in out


def test_a_queue_file_that_is_not_there_says_where_the_format_is_written_down(
    tmp_path: Path,
) -> None:
    """The first thing anybody runs, and the first thing that is not set up yet."""
    code, _, err = run_cli(["queue", "--from", str(tmp_path / "nope.json")])

    assert code == cli.EXIT_FAILED
    assert "agent/README.md" in err


def test_a_queue_from_another_contract_version_is_reported_not_guessed_at(
    tmp_path: Path,
) -> None:
    """Reading version 2 as if it were version 1 is how a field silently moves."""
    queue = write_queue(tmp_path / "queue.json", a_queue(), version=2)

    code, _, err = run_cli(["queue", "--from", str(queue)])

    assert code == cli.EXIT_MISSING_PIECE
    assert "Версия очереди 2" in err


def test_the_queue_can_come_from_the_endpoint_instead_of_the_file() -> None:
    """The transport is a swap; everything below it is the same shape."""
    asked: list[tuple[str, int]] = []

    def fetch(base_url: str, limit: int) -> str:
        asked.append((base_url, limit))
        return json.dumps({"version": 1, "items": [a_queue()]})

    code, out, _ = run_cli(
        ["queue", "--from", "http://localhost:8000", "--limit", "7", "--no-manual"], fetch=fetch
    )

    assert code == 0
    assert asked == [("http://localhost:8000", 7)]
    assert "ГОТОВО К ОТКЛИКУ: 1" in out


def test_an_endpoint_that_is_not_written_yet_points_at_the_file(tmp_path: Path) -> None:
    """A 404 here means the backend half of task 6 has not landed, not a crash."""

    def fetch(base_url: str, limit: int) -> str:
        raise QueueNotBuiltError("404: эндпоинта очереди в этом бэкенде нет")

    code, _, err = run_cli(
        ["queue", "--from", "https://example.invalid", "--no-manual"], fetch=fetch
    )

    assert code == cli.EXIT_MISSING_PIECE
    assert "эндпоинта очереди" in err


def test_a_backend_that_is_down_is_not_the_same_as_a_backend_without_the_endpoint() -> None:
    """Two different answers, so two different exit codes.

    «Start the backend» and «somebody has to write that endpoint» send whoever
    is reading a nightly log to different places, and a single non-zero code
    sends them to the wrong one half the time.
    """

    def unreachable(base_url: str, limit: int) -> str:
        raise QueueUnavailableError("Не удалось спросить: соединение отклонено")

    code, _, err = run_cli(
        ["queue", "--from", "http://localhost:8000", "--no-manual"], fetch=unreachable
    )

    assert code == cli.EXIT_FAILED
    assert "соединение отклонено" in err


def test_an_empty_queue_is_a_sentence_rather_than_an_empty_table(tmp_path: Path) -> None:
    """The state a fresh checkout is in."""
    queue = write_queue(tmp_path / "queue.json")

    code, out, _ = run_cli(["queue", "--from", str(queue)])

    assert code == 0
    assert "Очередь пуста" in out


def test_the_whole_report_survives_the_console_it_is_printed_on(tmp_path: Path) -> None:
    """This is hh.KZ: Kazakh letters in an employer's name, emoji in a title.

    None of them exist in cp1251, and a character outside the codepage does not
    degrade — it raises UnicodeEncodeError at the moment the report is shown,
    after the work is done. It has happened here before, twice.
    """
    company = "Қазақстан Темір Жолы"
    title = "Python-разработчик 🙂"
    for hazard in (company, title):
        with pytest.raises(UnicodeEncodeError):
            hazard.encode("cp1251")
    queue = write_queue(tmp_path / "queue.json", a_queue(company=company, title=title))

    out = run_cli(["queue", "--from", str(queue)])[1]

    out.encode("cp1251")
    assert printable("Темір", "cp1251") in out


def test_the_report_never_hides_the_link_a_person_would_check(tmp_path: Path) -> None:
    """Titles are clipped to keep the table a table; the url is on its own line."""
    queue = write_queue(tmp_path / "queue.json", a_queue(company="К" * 60))

    out = run_cli(["queue", "--from", str(queue)])[1]

    assert PAGE_URL in out


def test_a_queue_file_that_is_not_the_contract_is_reported_readably(tmp_path: Path) -> None:
    """A hand-maintained JSON file is a hand-maintained JSON file.

    Pydantic's own message names the field and the row, which is what somebody
    editing the file needs; what must not happen is a traceback, because then
    the answer looks like a bug in the CLI rather than a typo in the queue.
    """
    broken = tmp_path / "queue.json"
    broken.write_text('{"version": 1, "items": [{"url": "https://hh.kz/vacancy/1"}]}', "utf-8")

    code, _, err = run_cli(["queue", "--from", str(broken)])

    assert code == cli.EXIT_FAILED
    assert "не в том формате" in err


def test_a_broken_results_file_does_not_hide_the_queue(tmp_path: Path) -> None:
    """The results are the second half of the report, not a precondition for it.

    Refusing to show what is ready because the file of last time's outcomes is
    unreadable would trade the useful half of the answer for the incidental one.
    """
    queue = write_queue(tmp_path / "queue.json", a_queue())
    (tmp_path / "queue-results.json").write_text("{не json", encoding="utf-8")

    code, out, _ = run_cli(["queue", "--from", str(queue)])

    assert code == 0
    assert "ГОТОВО К ОТКЛИКУ: 1" in out


def test_a_stream_that_cannot_answer_at_all_counts_as_nobody() -> None:
    """A closed stdin raises rather than returning False, and the answer is still no.

    Every refusal in this project leans the same way: when it cannot be
    established that a person is there, nothing is sent.
    """
    closed = io.StringIO()
    closed.close()
    runner = Recorder()

    code = cli.main(
        ["apply", "--send"],
        run=runner,
        stdin=closed,
        stdout=Terminal(),
        stderr=io.StringIO(),
    )

    assert code == cli.EXIT_NO_HUMAN
    assert runner.commands == []


# ── the queue is the database's, and the file adds to it ──────────────
#
# ``agent/queue.json`` was the whole queue until 2026-09-09. A night of
# crawling, scoring and letter writing therefore reached this report as one row
# somebody had typed in weeks earlier, with no score — and the header named the
# file, so nothing about the output said what was missing. These tests are about
# the arrangement that replaced it.
#
# Note what every test above had to start doing: naming ``--no-manual`` or a
# temporary file. The default manual path is the owner's real
# ``agent/queue.json``, which is gitignored and exists on their machine, and a
# test that reads it would pass in CI and fail on the laptop it matters on.


def _serves(*items: dict[str, object]) -> Fetcher:
    """A backend that answers with these items."""

    def fetch(base_url: str, limit: int) -> str:
        return json.dumps({"version": 1, "items": list(items)}, ensure_ascii=False)

    return fetch


def test_the_queue_comes_from_the_database_without_being_asked_to(tmp_path: Path) -> None:
    """The default source is the backend. That is the whole defect, in one line.

    Nothing but this decides which question ``wwao queue`` answers: "what did
    the pipeline produce" or "what is in a file somebody edits by hand".
    """
    asked: list[str] = []

    def fetch(base_url: str, limit: int) -> str:
        asked.append(base_url)
        return json.dumps({"version": 1, "items": [a_queue()]})

    code, out, _ = run_cli(["queue", "--manual", str(tmp_path / "nothing.json")], fetch=fetch)

    assert code == 0
    assert asked == [cli.DEFAULT_BACKEND]
    assert "ГОТОВО К ОТКЛИКУ: 1" in out


def test_a_hand_written_row_is_added_to_the_queue_and_marked_as_one(tmp_path: Path) -> None:
    """Both halves, in order, and each readable as what it is.

    The scored row leads because the score is the only reason to prefer one
    vacancy over another, and the hand-added one says where it came from — its
    empty score column would otherwise read as "scoring has not run".
    """
    manual = write_queue(tmp_path / "queue.json", a_queue(vacancy_id="222222222", score=None))

    code, out, _ = run_cli(["queue", "--manual", str(manual)], fetch=_serves(a_queue()))

    assert code == 0
    assert "ГОТОВО К ОТКЛИКУ: 2" in out
    assert out.index(VACANCY) < out.index("222222222")
    assert "добавлено вручную" in out
    # And the header says both sources rather than one path.
    assert "(база)" in out and str(manual) in out


def test_a_vacancy_in_both_halves_is_shown_once_as_the_database_has_it(tmp_path: Path) -> None:
    """The file is where a person adds what the queue missed; they overlap.

    The database's row wins: it is the one carrying the score and the letter the
    pipeline wrote.
    """
    manual = write_queue(tmp_path / "queue.json", a_queue(title="набрано руками", score=None))

    out = run_cli(["queue", "--manual", str(manual)], fetch=_serves(a_queue()))[1]

    assert "ГОТОВО К ОТКЛИКУ: 1" in out
    assert "набрано руками" not in out
    assert "добавлено вручную" not in out


def test_a_backend_that_is_down_does_not_let_the_file_pass_for_the_queue(
    tmp_path: Path,
) -> None:
    """The failure is loud, the hand-written rows are still shown, and the code is not 0.

    Showing the file alone and exiting 0 is precisely the behaviour that made a
    month-old row look like this morning's queue. Hiding the file instead would
    lose rows somebody asked for. So: both, and a warning naming which half is
    missing.
    """

    def unreachable(base_url: str, limit: int) -> str:
        raise QueueUnavailableError("Не удалось спросить: соединение отклонено")

    manual = write_queue(tmp_path / "queue.json", a_queue(vacancy_id="222222222"))

    code, out, err = run_cli(["queue", "--manual", str(manual)], fetch=unreachable)

    assert code == cli.EXIT_FAILED
    assert "соединение отклонено" in err
    assert "список ниже собран без неё" in out
    assert "222222222" in out


def test_a_missing_results_file_is_only_worth_saying_when_it_explains_something(
    tmp_path: Path,
) -> None:
    """Everything the backend serves is already filtered by what the tracker knows.

    A vacancy already applied to is not in the queue at all, so for those rows
    ``queue-results.json`` explains nothing — and a warning printed on every
    single run is a warning nobody reads by the third day.
    """
    without = run_cli(["queue", "--manual", str(tmp_path / "none.json")], fetch=_serves(a_queue()))[
        1
    ]
    manual = write_queue(tmp_path / "queue.json", a_queue(vacancy_id="222222222"))
    with_manual = run_cli(["queue", "--manual", str(manual)], fetch=_serves(a_queue()))[1]

    assert "результатов прошлых прогонов нет" not in without
    assert "результатов прошлых прогонов нет" in with_manual


def test_no_manual_shows_the_database_and_nothing_else(tmp_path: Path) -> None:
    """For the run that wants to see exactly what the pipeline produced."""
    manual = write_queue(tmp_path / "queue.json", a_queue(vacancy_id="222222222"))

    out = run_cli(["queue", "--manual", str(manual), "--no-manual"], fetch=_serves(a_queue()))[1]

    assert "222222222" not in out
    assert "ГОТОВО К ОТКЛИКУ: 1" in out


def test_naming_a_file_reads_that_file_and_nothing_underneath_it(tmp_path: Path) -> None:
    """``--from`` with a path is the no-server transport, and it stays literal.

    A second source appearing under a file somebody named would be the same
    surprise in the other direction.
    """
    queue = write_queue(tmp_path / "queue.json", a_queue())

    def never(base_url: str, limit: int) -> str:
        raise AssertionError(f"the backend was asked anyway: {base_url}")

    code, out, _ = run_cli(["queue", "--from", str(queue)], fetch=never)

    assert code == 0
    assert str(queue) in out
    assert "(база)" not in out


# ── the transport, the one thing that would talk to a backend ─────────

BASE: Final[str] = "http://localhost:8000"
ENDPOINT: Final[str] = f"{BASE}/api/v1/applications/queue"


@respx.mock
def test_the_queue_request_carries_the_local_token(monkeypatch: pytest.MonkeyPatch) -> None:
    """``/api/v1/applications`` is behind a shared token, and this is the client.

    Both processes are the owner's own, on one machine, so there is nothing to
    authenticate between them; what the token buys is that nothing else on the
    host reaches somebody's cover letters by guessing a URL.
    """
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret-token")
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"version": 1, "items": []})
    )

    raw = fetch_over_http(BASE, 5)

    assert json.loads(raw) == {"version": 1, "items": []}
    assert route.calls[0].request.headers["authorization"] == "Bearer s3cret-token"
    assert route.calls[0].request.url.params["limit"] == "5"


@respx.mock
def test_a_refused_queue_names_the_variable_and_never_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The name of the setting is the useful half; the value must not be printed.

    An error message goes to a terminal, into a scrollback and often into a
    paste. A secret has no business in any of those, and the person reading it
    needs to know which variable to fix, not what is currently in it.
    """
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret-token")
    respx.get(ENDPOINT).mock(return_value=httpx.Response(401))

    with pytest.raises(QueueUnavailableError) as raised:
        fetch_over_http(BASE, 5)

    assert "AGENT_API_TOKEN" in str(raised.value)
    assert "s3cret-token" not in str(raised.value)


@respx.mock
def test_a_missing_token_is_said_out_loud(monkeypatch: pytest.MonkeyPatch) -> None:
    """«Wrong token» and «no token» are different five-minute problems."""
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    respx.get(ENDPOINT).mock(return_value=httpx.Response(401))

    with pytest.raises(QueueUnavailableError) as raised:
        fetch_over_http(BASE, 5)

    assert "не задан" in str(raised.value)


@respx.mock
def test_a_backend_with_no_token_configured_says_which_side_to_fix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """503 is the backend refusing to serve an unprotected queue, not an outage."""
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret-token")
    respx.get(ENDPOINT).mock(return_value=httpx.Response(503))

    with pytest.raises(QueueUnavailableError) as raised:
        fetch_over_http(BASE, 5)

    assert "у бэкенда не задан" in str(raised.value)


@respx.mock
def test_a_backend_without_the_endpoint_points_back_at_the_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An older backend is a state this repository was in yesterday."""
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret-token")
    respx.get(ENDPOINT).mock(return_value=httpx.Response(404))

    with pytest.raises(QueueUnavailableError) as raised:
        fetch_over_http(BASE, 5)

    assert "--from agent/queue.json" in str(raised.value)


@respx.mock
def test_a_backend_that_is_not_running_is_reported_as_such(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The most common failure of all, and it must not arrive as a traceback."""
    monkeypatch.setenv("AGENT_API_TOKEN", "s3cret-token")
    respx.get(ENDPOINT).mock(side_effect=httpx.ConnectError("connection refused"))

    with pytest.raises(QueueUnavailableError) as raised:
        fetch_over_http(BASE, 5)

    assert "Не удалось спросить" in str(raised.value)


# ── outcomes: the account without the person ──────────────────────────


def test_outcomes_starts_the_read_only_walk_as_its_own_process() -> None:
    """A module of its own rather than a mood of the one that sends.

    ``agent.outcomes`` opens pages and reads them; ``agent.run`` sends
    applications. Making the second do the first under a flag would put the
    apply flow one argument away from a run that was meant to read.
    """
    runner = Recorder()

    code, _, _ = run_cli(["outcomes"], run=runner)

    assert code == 0
    assert runner.commands == [[sys.executable, "-m", "agent.outcomes"]]


def test_outcomes_forwards_only_the_flags_it_declares() -> None:
    """A closed set, like ``apply``'s and for the same reason.

    This subcommand runs under the owner's login too. Nothing is handed through
    blindly, so a flag added to the walk one day has to be added here as well,
    in front of the tests in this file.
    """
    runner = Recorder()

    run_cli(["outcomes", "--limit", "5", "--to", "http://localhost:8000"], run=runner)

    assert runner.commands == [
        [
            sys.executable,
            "-m",
            "agent.outcomes",
            "--limit",
            "5",
            "--to",
            "http://localhost:8000",
        ]
    ]


def test_outcomes_refuses_a_flag_it_does_not_know_instead_of_forwarding_it() -> None:
    """What makes the set closed."""
    runner = Recorder()

    with pytest.raises(SystemExit) as exit_info:
        run_cli(["outcomes", "--send"], run=runner)

    assert exit_info.value.code == 2
    assert runner.commands == []


def test_outcomes_runs_where_nobody_is_watching_and_apply_does_not() -> None:
    """The third category, and the line between it and ``apply``.

    There is no card to read and no word to type here: the walk confirms
    nothing because it sends nothing. So the terminal check that guards
    ``apply`` would be a refusal with no rule behind it — and one rule fewer
    that a person has to hold in their head. What it does still need is the
    account, which is why it is a child process of its own.
    """
    runner = Recorder()

    walked, _, _ = run_cli(["outcomes"], run=runner, tty=False)
    applied, _, _ = run_cli(["apply"], run=Recorder(), tty=False)

    assert walked == 0
    assert applied == cli.EXIT_NO_HUMAN
    assert runner.commands == [[sys.executable, "-m", "agent.outcomes"]]


def test_the_walk_the_cli_names_is_a_file_the_repository_can_show_you() -> None:
    """A router's whole content is where each step lives."""
    assert (cli.REPO_ROOT / "agent" / "outcomes.py").is_file()


@respx.mock
def test_the_token_written_into_dotenv_is_used_when_the_environment_has_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``.env.example`` says to put the token in ``.env``; that has to be enough.

    Until 2026-09-16 only the backend read the file, so the CLI answered 401 and
    blamed the environment. The environment still wins when both are set.
    """
    dotenv = tmp_path / ".env"
    dotenv.write_text('# local\nexport AGENT_API_TOKEN="from-file"\nOTHER=1\n', encoding="utf-8")
    monkeypatch.setattr(queue_view, "DOTENV", dotenv)
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    route = respx.get(ENDPOINT).mock(
        return_value=httpx.Response(200, json={"version": 1, "items": []})
    )

    fetch_over_http(BASE, 5)
    assert route.calls[0].request.headers["authorization"] == "Bearer from-file"

    monkeypatch.setenv("AGENT_API_TOKEN", "from-environment")
    fetch_over_http(BASE, 5)
    assert route.calls[1].request.headers["authorization"] == "Bearer from-environment"


@respx.mock
def test_a_refused_token_from_dotenv_says_where_it_was_read(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text("AGENT_API_TOKEN=stale-token\n", encoding="utf-8")
    monkeypatch.setattr(queue_view, "DOTENV", dotenv)
    monkeypatch.delenv("AGENT_API_TOKEN", raising=False)
    respx.get(ENDPOINT).mock(return_value=httpx.Response(401))

    with pytest.raises(QueueUnavailableError) as raised:
        fetch_over_http(BASE, 5)

    assert ".env" in str(raised.value)
    assert "перезапустите бэкенд" in str(raised.value)
    assert "stale-token" not in str(raised.value)
