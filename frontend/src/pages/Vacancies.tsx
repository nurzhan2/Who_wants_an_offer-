import { useRef, useState } from 'react'

import { href } from '@/app/routes'
import { VacancyDetail } from '@/pages/VacancyDetail'
import { Empty, Failure, Loading, NextStep, Pill, Score, Section } from '@/components/ui'
import { useVacancies, type VacancyQuery } from '@/hooks/queries'
import { ago, count, date, plural, salary, score as formatScore } from '@/lib/format'
import { freshnessOf, OLD_AFTER_DAYS } from '@/lib/freshness'
import { REMOTE } from '@/lib/labels'
import type { Facets, MatchMode, VacancyListItem } from '@/types/api'

/**
 * Вакансии: the ranked list, and one vacancy opened.
 *
 * The salary filter is the one with a measured default. Five postings in six on
 * this corpus advertise nothing, so a floor that dropped them would answer with
 * the sixth and make the market look empty — the backend keeps them by default
 * (`include_unpriced`) and the checkbox below is how somebody asks the other
 * question deliberately.
 */
export function Vacancies({ selected }: { selected: string | null }) {
  const [filters, setFilters] = useState<Filters>(EMPTY)
  const [cursor, setCursor] = useState<string | null>(null)
  const [mode, setMode] = useState<MatchMode>(storedMode)
  const listTop = useRef<HTMLDivElement>(null)

  // A new page replaces the rows in place, so the reader would be left at the
  // bottom of it (seen walking the dashboard: «Дальше» and nothing visible
  // changed). Take them to the first row of the page they asked for.
  function turn(next: string | null): void {
    setCursor(next)
    listTop.current?.scrollIntoView({ block: 'start' })
  }

  const query: VacancyQuery = {
    ...(filters.city ? { city: filters.city } : {}),
    ...(filters.source ? { source: [filters.source] } : {}),
    ...(filters.bucket ? { bucket: [filters.bucket] } : {}),
    ...(filters.remote ? { remote: [filters.remote] } : {}),
    ...(filters.scoreMin ? { score_min: Number(filters.scoreMin) } : {}),
    ...(filters.salaryMin ? { salary_min: Number(filters.salaryMin) } : {}),
    include_unpriced: filters.includeUnpriced,
    mode,
    ...(cursor ? { cursor } : {}),
    limit: 50,
  }
  const page = useVacancies(query)

  if (selected !== null) {
    return <VacancyDetail id={selected} />
  }

  function change(next: Partial<Filters>): void {
    // Any change to the filter invalidates the cursor: a keyset position is
    // only meaningful inside the query that produced it, and reusing one across
    // filters silently skips rows.
    setCursor(null)
    setFilters((current) => ({ ...current, ...next }))
  }

  function rank(next: MatchMode): void {
    // Same reason as a filter change: the cursor holds a position in the old
    // order, and read against the new one it skips rows.
    setCursor(null)
    setMode(next)
    rememberMode(next)
  }

  return (
    <div className="rise">
      <Section
        title="Вакансии"
        note={`${MODES[mode].note} У каждой строки — что из требований закрыто, а что нет.`}
        action={
          page.data?.total !== null && page.data?.total !== undefined ? (
            <span className="tnum text-small text-muted">
              {count(page.data.total)} {plural(page.data.total, 'вакансия', 'вакансии', 'вакансий')}
            </span>
          ) : null
        }
      >
        <ModeSwitch mode={mode} onChange={rank} />
        <FilterBar
          filters={filters}
          facets={page.data?.facets ?? null}
          onChange={change}
          onReset={() => {
            setCursor(null)
            setFilters(EMPTY)
          }}
        />

        {page.isPending ? <Loading what="вакансии" /> : null}
        {page.isError ? (
          <Failure
            error={page.error}
            what="вакансии"
            onRetry={() => {
              void page.refetch()
            }}
          />
        ) : null}
        {page.data ? (
          page.data.items.length === 0 ? (
            filters === EMPTY ? (
              <NextStep
                title="Вакансий пока нет"
                action={
                  <a
                    href={href('overview')}
                    className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper"
                  >
                    К операциям
                  </a>
                }
              >
                На «Обзоре» нажмите «Собрать вакансии», затем «Пересчитать подбор» — список
                появится здесь, лучшие сверху.
              </NextStep>
            ) : (
              <Empty>Под эти фильтры ничего не подходит. Снимите часть условий.</Empty>
            )
          ) : (
            <>
              <div ref={listTop} className="scroll-mt-4 border-t border-hairline">
                {page.data.items.map((item) => (
                  <Row key={item.id} item={item} mode={mode} />
                ))}
              </div>
              <div className="mt-8 flex items-center gap-6">
                <button
                  type="button"
                  className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow enabled:hover:bg-ink enabled:hover:text-paper disabled:border-hairline disabled:text-muted"
                  disabled={page.data.next_cursor === null}
                  onClick={() => {
                    turn(page.data.next_cursor)
                  }}
                >
                  Дальше
                </button>
                {cursor !== null ? (
                  <button
                    type="button"
                    className="text-small text-muted underline-offset-4 hover:underline"
                    onClick={() => {
                      turn(null)
                    }}
                  >
                    В начало
                  </button>
                ) : null}
              </div>
            </>
          )
        ) : null}
      </Section>
    </div>
  )
}

interface ModeInfo {
  label: string
  /** The line under the heading while this mode ranks the list. */
  note: string
}

/**
 * The four orders of one list. Each is a number the scoring pass already
 * stored; switching reads another one and recomputes nothing.
 */
const MODES: Record<MatchMode, ModeInfo> = {
  combined: {
    label: 'совмещённо',
    note: 'По убыванию балла: 0,7 близости названия и 0,3 близости описания.',
  },
  title: {
    label: 'по названию',
    note: 'По близости названия вакансии к заголовку профиля.',
  },
  description: {
    label: 'по описанию',
    note: 'По близости текста вакансии к профилю.',
  },
  skills: {
    label: 'по навыкам',
    note: 'По доле требований вакансии, которые закрывают навыки профиля.',
  },
}

const SIGNAL_MODES: MatchMode[] = ['combined', 'title', 'description']

const MODE_KEY = 'vacancies.mode'

function isMode(value: string | null): value is MatchMode {
  return value !== null && Object.hasOwn(MODES, value)
}

/**
 * The mode chosen last time. Browser storage is a convenience here and nothing
 * more: private windows and blocked site data throw or come back empty, and the
 * list then simply opens in the combined order.
 */
function storedMode(): MatchMode {
  try {
    const value = window.localStorage.getItem(MODE_KEY)
    return isMode(value) ? value : 'combined'
  } catch {
    return 'combined'
  }
}

function rememberMode(mode: MatchMode): void {
  try {
    window.localStorage.setItem(MODE_KEY, mode)
  } catch {
    // Not remembered; the choice still holds until the page is closed.
  }
}

/**
 * The switch between the four orders.
 *
 * The skills mode stands apart on purpose and carries its caveat whenever it is
 * chosen. Measured 9 Sep 2026: with coverage at the heart of the score, the top
 * filled with postings that matched one requirement — «Бариста» on go, five
 * network engineers on linux — because 893 employers in 1958 leave the skills
 * field empty, and a share of what was named cannot tell one of one from ten of
 * ten. The mode stays, and it is not presented as an equal of the other three.
 */
function ModeSwitch({ mode, onChange }: { mode: MatchMode; onChange: (next: MatchMode) => void }) {
  return (
    <div className="mb-6">
      <div className="text-micro uppercase text-muted">порядок</div>
      <div
        role="radiogroup"
        aria-label="Порядок списка"
        className="mt-2 flex flex-wrap items-center gap-2"
      >
        {SIGNAL_MODES.map((option) => (
          <ModeButton key={option} option={option} mode={mode} onChange={onChange} />
        ))}
        <span className="mx-2 hidden h-6 border-l border-hairline sm:inline-block" aria-hidden />
        <ModeButton option="skills" mode={mode} onChange={onChange} />
      </div>
      <p className="mt-2 max-w-prose text-small text-muted">
        {mode === 'skills'
          ? 'Показывает пересечение требований вакансии с навыками профиля, а не пригодность. У вакансий с коротким списком требований завышает. По замеру 17 сентября верх держится на общих инструментах — linux, git, docker, CI/CD, — и в первую двадцатку попали три вакансии Linux-администратора. Вакансии, где требований нет вовсе, — в конце.'
          : 'Меняется только порядок: состав списка и фильтры те же, ничего не пересчитывается.'}
      </p>
    </div>
  )
}

function ModeButton({
  option,
  mode,
  onChange,
}: {
  option: MatchMode
  mode: MatchMode
  onChange: (next: MatchMode) => void
}) {
  const chosen = option === mode
  return (
    <button
      type="button"
      role="radio"
      aria-checked={chosen}
      className={`rounded-pill border px-5 py-2 text-small transition-colors duration-800 ease-slow ${
        chosen
          ? 'border-ink bg-ink text-paper'
          : `${option === 'skills' ? 'border-dashed' : ''} border-hairline hover:border-ink`
      }`}
      onClick={() => {
        onChange(option)
      }}
    >
      {MODES[option].label}
    </button>
  )
}

/**
 * The number the list is ranked by, with the combined one under it — so that a
 * reader sees how far the two orders disagree on this row. The bucket word is
 * not repeated here: buckets are drawn on the combined scale only.
 */
function ModeScore({ item, mode }: { item: VacancyListItem; mode: MatchMode }) {
  return (
    <div className="text-right">
      <div className="tnum text-heading font-semibold leading-none">
        {formatScore(item.mode_score)}
      </div>
      <div className="mt-1 text-micro uppercase text-muted">
        {mode === 'skills' && item.mode_score === null ? 'требований нет · ' : ''}
        совмещённый <span className="tnum">{formatScore(item.score)}</span>
      </div>
    </div>
  )
}

interface Filters {
  city: string
  source: string
  bucket: string
  remote: string
  scoreMin: string
  salaryMin: string
  includeUnpriced: boolean
}

const EMPTY: Filters = {
  city: '',
  source: '',
  bucket: '',
  remote: '',
  scoreMin: '',
  salaryMin: '',
  // The default the backend also holds: a salary floor keeps the postings that
  // name no salary. Stated here as well so the checkbox starts consistent with
  // what the list is actually doing.
  includeUnpriced: true,
}

function FilterBar({
  filters,
  facets,
  onChange,
  onReset,
}: {
  filters: Filters
  facets: Facets | null
  onChange: (next: Partial<Filters>) => void
  onReset: () => void
}) {
  return (
    <div className="mb-8 grid gap-4 border-b border-hairline pb-8 sm:grid-cols-2 lg:grid-cols-4">
      <Select
        label="город"
        value={filters.city}
        options={Object.keys(facets?.cities ?? {})}
        counts={facets?.cities}
        onChange={(city) => {
          onChange({ city })
        }}
      />
      <Select
        label="источник"
        value={filters.source}
        options={Object.keys(facets?.sources ?? {})}
        counts={facets?.sources}
        onChange={(source) => {
          onChange({ source })
        }}
      />
      <Select
        label="формат"
        value={filters.remote}
        options={Object.keys(REMOTE)}
        render={(value) => REMOTE[value as keyof typeof REMOTE]}
        onChange={(remote) => {
          onChange({ remote })
        }}
      />
      <NumberField
        label="score от"
        value={filters.scoreMin}
        onChange={(scoreMin) => {
          onChange({ scoreMin })
        }}
      />
      <NumberField
        label="зарплата от, USD"
        value={filters.salaryMin}
        onChange={(salaryMin) => {
          onChange({ salaryMin })
        }}
      />
      <label className="flex items-start gap-3 text-small sm:col-span-2 lg:col-span-3">
        <input
          type="checkbox"
          checked={filters.includeUnpriced}
          className="mt-1 accent-ink"
          onChange={(event) => {
            onChange({ includeUnpriced: event.target.checked })
          }}
        />
        <span>
          оставлять вакансии без зарплаты
          <span className="block text-muted">
            Их пять из шести: hh не требует указывать деньги. Снимите галочку, чтобы спросить
            другое — «только те, где сумма названа».
          </span>
        </span>
      </label>
      <div className="flex items-start">
        <button
          type="button"
          className="text-small text-muted underline-offset-4 hover:underline"
          onClick={onReset}
        >
          Сбросить фильтры
        </button>
      </div>
    </div>
  )
}

function Select({
  label,
  value,
  options,
  counts,
  render,
  onChange,
}: {
  label: string
  value: string
  options: string[]
  counts?: Record<string, number> | undefined
  render?: (value: string) => string
  onChange: (value: string) => void
}) {
  return (
    <label className="block text-micro uppercase text-muted">
      {label}
      <select
        className="mt-1 block w-full border border-hairline bg-paper px-3 py-2 text-body normal-case text-ink"
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
        }}
      >
        <option value="">любой</option>
        {options.map((option) => (
          <option key={option} value={option}>
            {render ? render(option) : option}
            {counts?.[option] === undefined ? '' : ` (${String(counts[option])})`}
          </option>
        ))}
      </select>
    </label>
  )
}

function NumberField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (value: string) => void
}) {
  return (
    <label className="block text-micro uppercase text-muted">
      {label}
      <input
        type="number"
        min={0}
        value={value}
        className="tnum mt-1 block w-full border border-hairline bg-paper px-3 py-2 text-body text-ink"
        onChange={(event) => {
          onChange(event.target.value)
        }}
      />
    </label>
  )
}

/**
 * One row. The whole row opens the card; the original posting is a second,
 * separate link, so the row is a block with a stretched link rather than an
 * anchor — an anchor inside an anchor is not valid HTML and browsers split it.
 */
function Row({ item, mode }: { item: VacancyListItem; mode: MatchMode }) {
  return (
    <div className="relative grid grid-cols-1 items-baseline gap-2 border-b border-hairline py-5 transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper sm:grid-cols-[1fr_auto]">
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <a
            href={href('vacancies', item.id)}
            className="font-semibold after:absolute after:inset-0 after:content-['']"
          >
            {item.title}
          </a>
          <span className="text-small text-muted">{item.company ?? 'без компании'}</span>
          {item.is_applied ? <Pill>в трекере</Pill> : null}
          {item.is_seed ? <Pill>тестовая запись</Pill> : null}
        </div>
        <div className="mt-2 flex flex-wrap items-baseline gap-x-4 gap-y-1 text-small text-muted">
          <span>{item.city ?? 'без города'}</span>
          <span>{REMOTE[item.remote]}</span>
          <span>{salary(item.salary_min, item.salary_max, item.currency)}</span>
          <SourceLink item={item} />
          <FreshnessWord item={item} />
          {item.missing_required_count > 0 ? (
            <span>
              не закрыто {item.missing_required_count}{' '}
              {plural(item.missing_required_count, 'требование', 'требования', 'требований')}
            </span>
          ) : (
            <span>требования закрыты</span>
          )}
        </div>
      </div>
      {mode === 'combined' ? (
        <Score value={item.score} bucket={item.bucket} />
      ) : (
        <ModeScore item={item} mode={mode} />
      )}
    </div>
  )
}

/**
 * How old the posting is, and a word when it should not be applied to blindly.
 *
 * The list never shows an archived posting — the API leaves inactive rows out —
 * so the words here are «давно не видели» and «старше 30 дней». A fresh posting
 * shows its date and nothing else; the card says «в архиве» where it applies.
 */
function FreshnessWord({ item }: { item: VacancyListItem }) {
  const verdict = freshnessOf(item.published_at, item.last_seen_at, item.is_active)
  const published = item.published_at
    ? `${date(item.published_at)} (${ago(item.published_at)})`
    : 'без даты'
  if (verdict === 'unseen') return <span>{published} · давно не видели на сайте</span>
  if (verdict === 'old') return <span>{published} · старше {OLD_AFTER_DAYS} дней</span>
  return <span>{published}</span>
}

/**
 * Where the vacancy was found, and the way to its original page.
 *
 * The link is `source_url` exactly as stored — never an address rebuilt from an
 * id — and it belongs to the first source listed, the one holding the most
 * data. A seed row gets no link: its address is example.test.
 */
function SourceLink({ item }: { item: VacancyListItem }) {
  const sources = item.source_slugs.join(' · ') || 'без источника'
  if (item.source_url === null || item.is_seed) {
    return <span>{sources}</span>
  }
  return (
    <span>
      {sources} ·{' '}
      <a
        href={item.source_url}
        target="_blank"
        rel="noreferrer"
        className="relative z-10 underline underline-offset-4"
      >
        оригинал ↗
      </a>
    </span>
  )
}
