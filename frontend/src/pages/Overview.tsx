import type { UseQueryResult } from '@tanstack/react-query'

import { href } from '@/app/routes'
import { OperationsPanel } from '@/components/OperationsPanel'
import {
  Card,
  Empty,
  Failure,
  Field,
  Loading,
  NextStep,
  Pill,
  Ratio,
  Score,
  Section,
  Stat,
  Stats,
} from '@/components/ui'
import { ago, count, date, dateTime, NOTHING, plural } from '@/lib/format'
import { RUN_STATUS } from '@/lib/labels'
import type { CrawlPosition, Overview as OverviewData, SourceRunState } from '@/types/api'

/**
 * Обзор: what is in the database, where the crawl is, and what has gone out.
 *
 * Every number here is computed in SQL and rendered as it arrived. Nothing on
 * this screen is summed in the browser, and nothing that came back as `null` is
 * drawn as a zero — the two are different facts everywhere on this page, and
 * this is the page where confusing them would be most expensive: "0 осталось"
 * on a corpus of thirteen thousand postings reads as a finished backfill.
 */
export function Overview({ query }: { query: UseQueryResult<OverviewData> }) {
  const { data, isPending, isError, error, refetch } = query

  if (isPending) return <Loading what="обзор" />
  if (isError) {
    return (
      <Failure
        error={error}
        what="обзор"
        onRetry={() => {
          void refetch()
        }}
      />
    )
  }

  return (
    <div className="rise">
      <FirstSteps data={data} />
      <OperationsPanel />
      <Corpus data={data} />
      <Crawl data={data} />
      <Runs runs={data.runs} />
      <HarvestPanel data={data} />
      <Applications data={data} />
    </div>
  )
}

/**
 * What to do first, when the database cannot answer anything yet.
 *
 * Shown only while something foundational is missing — no resume, or no
 * vacancies — and gone as soon as it is not.
 */
function FirstSteps({ data }: { data: OverviewData }) {
  if (data.profile === null) {
    return (
      <div className="mb-section">
        <NextStep
          title="Начните с резюме"
          action={
            <a
              href={href('profile')}
              className="rounded-pill border border-ink bg-ink px-6 py-2 text-small text-paper transition-colors duration-800 ease-slow hover:bg-paper hover:text-ink"
            >
              Загрузить резюме
            </a>
          }
        >
          Без резюме вакансии не с чем сравнивать. Загрузите PDF или DOCX на странице «Мои данные»
          — разбор займёт около трёх минут, потом вернитесь сюда и нажмите «Собрать вакансии».
        </NextStep>
      </div>
    )
  }
  if (data.vacancies.total === 0) {
    return (
      <div className="mb-section">
        <NextStep title="В базе пока нет вакансий">
          Нажмите «Собрать вакансии» ниже. Когда обход закончится, посчитайте эмбеддинги и
          пересчитайте подбор — список появится на странице «Вакансии».
        </NextStep>
      </div>
    )
  }
  return null
}

function Corpus({ data }: { data: OverviewData }) {
  const { vacancies } = data
  return (
    <Section
      title="База"
      note="Сколько постингов собрано и какая часть из них пригодна для семантического сравнения. Разрыв между «в базе» и «оценено» — это очередь скоринга, а не потеря."
    >
      <Stats>
        <Stat value={count(vacancies.total)} label="вакансий в базе" note={`активных ${count(vacancies.active)}`} />
        <Stat
          value={count(vacancies.embedded)}
          label="с эмбеддингами"
          note={
            vacancies.needs_embedding > 0
              ? `ждут вектора ${count(vacancies.needs_embedding)}`
              : 'весь корпус векторизован'
          }
        />
        <Stat value={count(vacancies.scored)} label="посчитанных match" note="для активного резюме" />
        <Stat value={count(vacancies.skill_rows)} label="строк навыков" note="то, из чего считается покрытие" />
      </Stats>
    </Section>
  )
}

/**
 * Where the walk got to, per city and per sitemap file.
 *
 * Two counts, and neither is derived from the other: a stretch is an interval
 * over `(lastmod, id)` and counts nothing, so "сколько осталось" comes from a
 * census the crawl records while it holds the file. A file no run has counted
 * since that was added says «не измерено» — never «0 осталось», which would
 * announce a completed backfill.
 */
function Crawl({ data }: { data: OverviewData }) {
  return (
    <Section
      title="Обход"
      note="Позиция по городам и файлам sitemap. Отрезков больше одного — значит прогон прерывался: покрытая часть корпуса перестала быть непрерывной."
    >
      {data.crawl.length === 0 ? (
        <Empty>Ни один источник ещё не записал позицию — обхода не было.</Empty>
      ) : (
        data.crawl.map((source) => (
          <div key={source.slug} className="mb-8 last:mb-0">
            <h3 className="mb-4 text-small font-semibold uppercase tracking-widest">{source.slug}</h3>
            <div className="border-t border-hairline">
              {source.positions.map((position) => (
                <CrawlRow key={`${position.scope}/${position.label}`} position={position} />
              ))}
            </div>
          </div>
        ))
      )}
    </Section>
  )
}

function CrawlRow({ position }: { position: CrawlPosition }) {
  const covered =
    position.total !== null && position.outstanding !== null
      ? position.total - position.outstanding
      : null

  return (
    <div className="grid grid-cols-2 gap-4 border-b border-hairline py-4 sm:grid-cols-5">
      <div>
        <div className="font-semibold">{position.title ?? position.scope}</div>
        <div className="text-small text-muted">
          {position.scope} · {position.label}
        </div>
      </div>
      <Field label="пройдено">
        <Ratio part={covered} of={position.total} />
      </Field>
      <Field label="осталось">
        {position.outstanding === null ? (
          <span className="text-muted">не измерено</span>
        ) : (
          <span className="tnum">{count(position.outstanding)}</span>
        )}
      </Field>
      <Field label="отрезков">
        <span className="tnum">{count(position.stretches)}</span>
      </Field>
      <Field label="свежесть">
        <div className="text-small">
          {position.newest ? `до ${date(position.newest)}` : 'ничего не покрыто'}
        </div>
        <div className="text-small text-muted">записано {ago(position.updated_at)}</div>
      </Field>
    </div>
  )
}

/**
 * The last run of every source, and the one outcome that needs its own word.
 *
 * «Остановлен проверкой на робота» is not a failure and not a half-working
 * connector: hh answered a permitted request by deciding we are a robot, the
 * crawl position was kept, and the next run continues from the same page. It is
 * shown as its own line because the two situations that share the `partial`
 * status need opposite reactions — wait, or go and read a module.
 */
function Runs({ runs }: { runs: SourceRunState[] }) {
  return (
    <Section title="Прогоны" note="Последний прогон каждого источника и чем он кончился.">
      {runs.length === 0 ? (
        <Empty>Пайплайн ещё не запускался.</Empty>
      ) : (
        <div className="grid gap-4 md:grid-cols-2">
          {runs.map((run) => (
            <Card key={run.run_id} inverted={run.stopped_by_robot_check}>
              <div className="flex items-baseline justify-between gap-4">
                <span className="text-heading font-semibold">{run.slug}</span>
                <Pill strong={run.status === 'failed'}>{RUN_STATUS[run.status]}</Pill>
              </div>
              <p className="mt-4 text-small text-muted">
                {dateTime(run.started_at)} · {ago(run.started_at)}
              </p>
              <div className="mt-6 grid grid-cols-3 gap-4">
                <Field label="найдено">
                  <span className="tnum">{count(run.found)}</span>
                </Field>
                <Field label="новых">
                  <span className="tnum">{count(run.new)}</span>
                </Field>
                <Field label="обновлено">
                  <span className="tnum">{count(run.updated)}</span>
                </Field>
              </div>
              {run.stopped_by_robot_check ? (
                <p className="mt-6 border-t border-hairline pt-4 text-small">
                  <strong className="font-semibold">Остановлен проверкой на робота.</strong> Источник
                  цел, чинить нечего: позиция обхода сохранена, следующий прогон продолжит с того же
                  места.
                </p>
              ) : null}
              {run.errors
                .filter((entry) => entry.stage !== 'challenge')
                .map((entry, index) => (
                  <p key={index} className="mt-4 text-small text-muted">
                    {entry.stage ?? 'ошибка'}: {entry.detail ?? entry.error ?? 'без описания'}
                  </p>
                ))}
            </Card>
          ))}
        </div>
      )}
    </Section>
  )
}

/**
 * What the last crawl actually bought, by name.
 *
 * The counters cannot answer this. hh's sitemap carries a URL and a date, so a
 * page is paid for before anyone can tell what it advertises — «41 новая
 * вакансия» says nothing about whether the budget went on this candidate's
 * field. The titles do, and that is the whole reason this panel exists.
 */
function HarvestPanel({ data }: { data: OverviewData }) {
  const { harvest } = data
  return (
    <Section
      title="Что выкупил последний прогон"
      note="Поимённо: у hh нельзя спросить вакансии по ключевым словам — страница покупается до того, как станет ясно, что на ней. Здесь видно, python это или чужое."
    >
      {harvest.since === null ? (
        <Empty>Прогонов ещё не было, окно не с чего открывать.</Empty>
      ) : harvest.items.length === 0 ? (
        <Empty>С {dateTime(harvest.since)} новых постингов не появилось.</Empty>
      ) : (
        <>
          <p className="mb-6 text-small text-muted">
            {count(harvest.total)} {plural(harvest.total, 'постинг', 'постинга', 'постингов')} с{' '}
            {dateTime(harvest.since)}
            {harvest.items.length < harvest.total ? `, показано ${count(harvest.items.length)}` : ''}
          </p>
          <div className="border-t border-hairline">
            {harvest.items.map((item) => (
              <a
                key={item.id}
                href={href('vacancies', item.id)}
                className="flex items-baseline justify-between gap-6 border-b border-hairline py-3 transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper"
              >
                <span className="min-w-0">
                  <span className="block truncate font-semibold">{item.title}</span>
                  <span className="block truncate text-small text-muted">
                    {item.company ?? 'без компании'} · {item.source_slug ?? NOTHING}
                  </span>
                </span>
                <Score value={item.score} bucket={item.bucket} />
              </a>
            ))}
          </div>
        </>
      )}
    </Section>
  )
}

function Applications({ data }: { data: OverviewData }) {
  const { applications } = data
  return (
    <Section
      title="Отклики"
      note="«Отправлено» — только отклики, которые подтвердил сам hh: его счётчик откликов на вакансии не ноль или он сообщил состояние переписки. Отправку, о которой отчитался агент, но hh её ещё не подтвердил, видно отдельно."
    >
      <Stats>
        <Stat
          value={count(applications.sent_confirmed)}
          label="отправлено, hh подтвердил"
          note={
            applications.sent > applications.sent_confirmed
              ? `ещё ${count(applications.sent - applications.sent_confirmed)} без подтверждения — нажмите «Обновить исходы откликов»`
              : null
          }
        />
        <Stat value={count(applications.queued)} label="в очереди" note="письмо есть, ждёт подтверждения" />
        <Stat
          value={count(applications.needs_manual)}
          label="needs_manual"
          note="агент остановился и оставил причину"
        />
        <Stat
          value={count(applications.answered)}
          label="с ответом от hh"
          note={`писем написано ${count(applications.with_letter)}`}
        />
      </Stats>
    </Section>
  )
}
