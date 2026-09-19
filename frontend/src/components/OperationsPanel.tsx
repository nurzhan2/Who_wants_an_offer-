import { ApiError } from '@/api/client'
import { Button, Counted, Section } from '@/components/ui'
import {
  isFinished,
  useCancelOperation,
  useOperations,
  useStartOperation,
} from '@/hooks/useOperations'
import { useConfirmedList } from '@/hooks/useConfirmations'
import { useCompletion, useNow } from '@/hooks/useMotion'
import { explain } from '@/lib/errors'
import { ago, count, dateTime } from '@/lib/format'
import type { ConfirmedList } from '@/types/confirmations'
import type { Operation, OperationKind, OperationsState } from '@/types/operations'

/**
 * Операции: the daily routine, one button per step.
 *
 * Every step takes minutes, so a button starts an operation on the server and
 * the row below it follows the operation to its end — no reload, no terminal.
 * While a step runs its button is disabled rather than starting a second copy;
 * the server refuses a second copy anyway, and a button that says so before it
 * is pressed is the honest version.
 *
 * The last two steps need the owner's own browser and hh login, which the
 * server does not have. Their rows say whether the local watcher is running,
 * and what to start if it is not.
 */

interface Step {
  kind: OperationKind
  label: string
  /** What the button says while this step is running. */
  doing: string
  what: string
  /** Needs the local watcher on the owner's machine. */
  agent?: boolean
}

const STEPS: Step[] = [
  {
    kind: 'crawl',
    doing: 'Собираем…',
    label: 'Собрать вакансии',
    what: 'Обходит включённые источники. hh — около двадцати минут, с вежливой паузой между запросами.',
  },
  {
    kind: 'embed',
    doing: 'Считаем векторы…',
    label: 'Посчитать эмбеддинги',
    what: 'Векторы для вакансий, у которых их ещё нет. На процессоре — до нескольких минут на пачку.',
  },
  {
    kind: 'match',
    doing: 'Пересчитываем…',
    label: 'Пересчитать подбор',
    what: 'Оценивает все вакансии против активного резюме.',
  },
  {
    kind: 'letters',
    doing: 'Пишем письма…',
    label: 'Написать письма',
    what: 'Пять писем для лучших вакансий, у которых письма ещё нет.',
  },
  {
    kind: 'outcomes',
    doing: 'Читаем ответы…',
    label: 'Обновить исходы откликов',
    what: 'Агент открывает ваши отправленные отклики на hh и читает ответ. Ничего не отправляет.',
    agent: true,
  },
  {
    kind: 'send',
    doing: 'Отправляем…',
    label: 'Отправить подтверждённые',
    what: 'Агент отправляет только отклики, которые вы подтвердили в карточке вакансии.',
    agent: true,
  },
]

export function OperationsPanel() {
  const state = useOperations()
  const start = useStartOperation()

  return (
    <Section
      title="Операции"
      note="Вся ежедневная работа — здесь. Каждая операция идёт на сервере; строка под кнопкой показывает, что происходит, и обновляется сама."
    >
      {state.isError ? (
        <p className="mb-6 text-small" role="alert">
          {explain(state.error).reason} {explain(state.error).remedy}
        </p>
      ) : null}
      {start.isError ? (
        <p className="mb-6 text-small" role="alert">
          {startFailure(start.error)}
        </p>
      ) : null}
      <ol className="rise-list border-t border-hairline">
        {STEPS.map((step, index) => (
          <StepRow
            key={step.kind}
            step={step}
            position={index + 1}
            state={state.data}
            starting={start.isPending && start.variables === step.kind}
            onStart={() => {
              start.mutate(step.kind)
            }}
          />
        ))}
      </ol>
    </Section>
  )
}

function startFailure(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return error.detail ?? 'Эта операция уже идёт.'
  }
  const { reason, remedy } = explain(error)
  return `${reason} ${remedy}`
}

/**
 * One link of the chain.
 *
 * The left column says where the step stands — «пройден», «идёт», «впереди» —
 * by word and weight, the way this system says every status: the running step
 * is the one in semibold, a finished one is in plain ink, one ahead is muted.
 * A running step also moves: its clock ticks every second, its 1px gauge
 * follows the count, and its latest line fades in as it changes, so a crawl two
 * hours long never looks frozen between two polls.
 */
function StepRow({
  step,
  position,
  state,
  starting,
  onStart,
}: {
  step: Step
  position: number
  state: OperationsState | undefined
  starting: boolean
  onStart: () => void
}) {
  const busy = state?.busy.includes(step.kind) ?? false
  const last = state?.operations.find((operation) => operation.kind === step.kind) ?? null
  const agentAlive = watcherAlive(state)
  const confirmed = useConfirmedList()
  const nothingToSend = step.kind === 'send' && (confirmed.data?.items.length ?? 0) === 0
  // A crawl embeds what it fetched; a second pass alongside it is refused.
  const crawlEmbeds = step.kind === 'embed' && (state?.busy.includes('crawl') ?? false)
  const why = nothingToSend
    ? 'Нет подтверждённых откликов: подтвердите их в карточках вакансий'
    : crawlEmbeds
      ? 'Идёт обход — он сам посчитает векторы новых вакансий'
      : undefined
  const standing = standingOf(busy, last)
  const finishedWell = useCompletion(busy, last?.status === 'success')

  return (
    <li className="grid gap-4 border-b border-hairline py-6 md:grid-cols-[2.5rem_minmax(0,1fr)_auto]">
      <div
        className={`text-small ${standing === 'running' ? 'font-semibold' : standing === 'ahead' ? 'text-muted' : ''}`}
      >
        <span className="tnum">{String(position).padStart(2, '0')}</span>
        <span className="sr-only"> — {STANDING[standing]}</span>
      </div>
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-3">
          <span className="font-semibold">{step.label}</span>
          <span
            key={standing}
            className={`swap text-micro uppercase ${standing === 'running' || standing === 'failed' ? 'font-semibold' : 'text-muted'}`}
            aria-hidden
          >
            {STANDING[standing]}
          </span>
        </div>
        <p className="mt-1 max-w-2xl text-small text-muted">{step.what}</p>
        {step.agent ? (
          <p className="mt-1 text-small text-muted">
            {agentAlive
              ? 'Локальный агент на связи.'
              : 'Локальный агент не запущен: запустите приложение через start.cmd — он поднимется вместе с ним.'}
          </p>
        ) : null}
        {step.kind === 'send' ? <ConfirmedSummary list={confirmed.data} /> : null}
        {crawlEmbeds ? (
          <p className="mt-2 text-small text-muted">
            Сейчас идёт обход — векторы новых вакансий он посчитает сам.
          </p>
        ) : null}
        {last ? <LastRun operation={last} /> : null}
      </div>
      <div className="flex items-start md:justify-end">
        <Button
          onClick={onStart}
          disabled={state === undefined || nothingToSend || crawlEmbeds}
          busy={busy ? statusWord(last, step.doing) : starting ? 'Запускаем…' : false}
          done={finishedWell ? 'Готово' : false}
          title={why}
        >
          {step.label}
        </Button>
      </div>
    </li>
  )
}

function LastRun({ operation }: { operation: Operation }) {
  const cancel = useCancelOperation()
  const finished = isFinished(operation)
  const failed = operation.status === 'failed'
  const running = operation.status === 'running'
  const now = useNow(!finished)
  const since = operation.started_at ?? operation.queued_at

  return (
    <div className="mt-4 border-l border-ink pl-4" aria-live="polite">
      <p
        key={operation.message}
        className={`swap break-anywhere text-small ${failed ? 'font-semibold' : ''}`}
      >
        {operation.message}
      </p>
      {running ? <Gauge done={operation.done} total={operation.total} /> : null}
      <p className="mt-1 text-small text-muted">
        {finished ? (
          `${dateTime(operation.finished_at)} · ${ago(operation.finished_at)}`
        ) : (
          <>
            {operation.status === 'running' ? 'идёт ' : 'ждёт '}
            <span className="tnum">{clock(now - new Date(since).getTime())}</span>
          </>
        )}
        {operation.done !== null ? (
          <>
            {' · сделано '}
            <Counted value={operation.done} />
            {operation.total !== null ? ` из ${count(operation.total)}` : ''}
          </>
        ) : (
          ''
        )}
        {operation.duration_seconds !== null && finished
          ? ` · ${seconds(operation.duration_seconds)}`
          : ''}
      </p>
      {operation.report.length > 0 ? (
        <ul className="rise-list mt-2 space-y-1 text-small">
          {operation.report.map((line, index) => (
            <li key={index} className="break-anywhere">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
      {operation.status === 'waiting_agent' ? (
        <div className="mt-3">
          <Button
            outline
            busy={cancel.isPending ? 'Отменяем…' : false}
            onClick={() => {
              cancel.mutate(operation.id)
            }}
          >
            Отменить запрос
          </Button>
        </div>
      ) : null}
    </div>
  )
}

/**
 * How far a running step has got, as a 1px line: filled to the count when the
 * operation can count, running along itself when it cannot. Never a filled bar
 * — the system allows a hairline and nothing heavier.
 */
function Gauge({ done, total }: { done: number | null; total: number | null }) {
  const counted = done !== null && total !== null && total > 0
  return (
    <div
      className="gauge mt-3 max-w-md"
      role="progressbar"
      aria-label="Ход операции"
      {...(counted ? { 'aria-valuemin': 0, 'aria-valuemax': total, 'aria-valuenow': done } : {})}
    >
      {counted ? (
        <div className="gauge-fill" style={{ transform: `scaleX(${String(Math.min(1, done / total))})` }} />
      ) : (
        <div className="gauge-run" />
      )}
    </div>
  )
}

function ConfirmedSummary({ list }: { list: ConfirmedList | undefined }) {
  if (!list) return null
  if (list.items.length === 0) {
    return (
      <p className="mt-2 text-small text-muted">
        Подтверждённых откликов нет. Откройте вакансию и нажмите «Откликнуться…».
        {list.no_longer_valid > 0
          ? ` Устаревших подтверждений: ${String(list.no_longer_valid)} — их нужно дать заново.`
          : ''}
      </p>
    )
  }
  return (
    <div className="mt-2 text-small">
      <p className="font-semibold">
        Подтверждено к отправке: {list.items.length}
        {list.no_longer_valid > 0 ? ` · устарело: ${String(list.no_longer_valid)}` : ''}
      </p>
      <ul className="mt-1 space-y-1">
        {list.items.map((item) => (
          <li key={item.external_id} className="break-anywhere text-muted">
            {item.title} — {item.company ?? 'без компании'} · подтверждено {ago(item.confirmed_at)}
          </li>
        ))}
      </ul>
    </div>
  )
}

/** What a busy step's button says: the step's own verb, or what it waits on. */
function statusWord(operation: Operation | null, doing: string): string {
  if (operation?.status === 'waiting_agent') return 'Ждёт агента…'
  if (operation?.status === 'queued') return 'В очереди…'
  return doing
}

/** Where a step stands in the chain, in a word. */
type Standing = 'running' | 'done' | 'failed' | 'ahead'

const STANDING: Record<Standing, string> = {
  running: 'идёт',
  done: 'пройден',
  failed: 'ошибка',
  ahead: 'впереди',
}

function standingOf(busy: boolean, last: Operation | null): Standing {
  if (busy) return 'running'
  if (last === null || last.status === 'cancelled') return 'ahead'
  if (last.status === 'failed') return 'failed'
  return last.status === 'success' ? 'done' : 'ahead'
}

/** An elapsed time as a clock, «4:07» or «1:02:15»: it visibly moves. */
function clock(milliseconds: number): string {
  const total = Math.max(0, Math.floor(milliseconds / 1000))
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const secs = String(total % 60).padStart(2, '0')
  return hours > 0
    ? `${String(hours)}:${String(minutes).padStart(2, '0')}:${secs}`
    : `${String(minutes)}:${secs}`
}

function seconds(value: number): string {
  if (value < 60) return `${String(Math.round(value))} с`
  const minutes = Math.floor(value / 60)
  return `${String(minutes)} мин ${String(Math.round(value - minutes * 60))} с`
}

/** Mirrors the server's ninety-second rule, measured on this clock. */
function watcherAlive(state: OperationsState | undefined): boolean {
  if (!state?.agent_seen_at) return false
  return Date.now() - new Date(state.agent_seen_at).getTime() < 90_000
}
