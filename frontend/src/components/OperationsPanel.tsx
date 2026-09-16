import { ApiError } from '@/api/client'
import { Button, Section } from '@/components/ui'
import {
  isFinished,
  useCancelOperation,
  useOperations,
  useStartOperation,
} from '@/hooks/useOperations'
import { explain } from '@/lib/errors'
import { ago, count, dateTime } from '@/lib/format'
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
  what: string
  /** Needs the local watcher on the owner's machine. */
  agent?: boolean
}

const STEPS: Step[] = [
  {
    kind: 'crawl',
    label: 'Собрать вакансии',
    what: 'Обходит включённые источники. hh — около двадцати минут, с вежливой паузой между запросами.',
  },
  {
    kind: 'embed',
    label: 'Посчитать эмбеддинги',
    what: 'Векторы для вакансий, у которых их ещё нет. На процессоре — до нескольких минут на пачку.',
  },
  {
    kind: 'match',
    label: 'Пересчитать подбор',
    what: 'Оценивает все вакансии против активного резюме.',
  },
  {
    kind: 'letters',
    label: 'Написать письма',
    what: 'Пять писем для лучших вакансий, у которых письма ещё нет.',
  },
  {
    kind: 'outcomes',
    label: 'Обновить исходы откликов',
    what: 'Агент открывает ваши отправленные отклики на hh и читает ответ. Ничего не отправляет.',
    agent: true,
  },
  {
    kind: 'send',
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
      <div className="rise-list border-t border-hairline">
        {STEPS.map((step) => (
          <StepRow
            key={step.kind}
            step={step}
            state={state.data}
            starting={start.isPending && start.variables === step.kind}
            onStart={() => {
              start.mutate(step.kind)
            }}
          />
        ))}
      </div>
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

function StepRow({
  step,
  state,
  starting,
  onStart,
}: {
  step: Step
  state: OperationsState | undefined
  starting: boolean
  onStart: () => void
}) {
  const busy = state?.busy.includes(step.kind) ?? false
  const last = state?.operations.find((operation) => operation.kind === step.kind) ?? null
  const agentAlive = watcherAlive(state)

  return (
    <div className="grid gap-4 border-b border-hairline py-6 md:grid-cols-[minmax(0,1fr)_auto]">
      <div className="min-w-0">
        <div className="font-semibold">{step.label}</div>
        <p className="mt-1 max-w-2xl text-small text-muted">{step.what}</p>
        {step.agent ? (
          <p className="mt-1 text-small text-muted">
            {agentAlive
              ? 'Локальный агент на связи.'
              : 'Локальный агент не запущен: запустите приложение через start.cmd — он поднимется вместе с ним.'}
          </p>
        ) : null}
        {last ? <LastRun operation={last} /> : null}
      </div>
      <div className="flex items-start md:justify-end">
        <Button onClick={onStart} disabled={busy || starting || state === undefined}>
          {busy ? statusWord(last) : starting ? 'Запускаем…' : step.label}
        </Button>
      </div>
    </div>
  )
}

function LastRun({ operation }: { operation: Operation }) {
  const cancel = useCancelOperation()
  const finished = isFinished(operation)
  const failed = operation.status === 'failed'

  return (
    <div className="mt-4 border-l border-ink pl-4" aria-live="polite">
      <p className={`break-anywhere text-small ${failed ? 'font-semibold' : ''}`}>
        {operation.message}
      </p>
      <p className="mt-1 text-small text-muted">
        {finished
          ? `${dateTime(operation.finished_at)} · ${ago(operation.finished_at)}`
          : `начато ${ago(operation.started_at ?? operation.queued_at)}`}
        {operation.done !== null
          ? ` · сделано ${count(operation.done)}${operation.total !== null ? ` из ${count(operation.total)}` : ''}`
          : ''}
        {operation.duration_seconds !== null
          ? ` · ${seconds(operation.duration_seconds)}`
          : ''}
      </p>
      {operation.report.length > 0 ? (
        <ul className="mt-2 space-y-1 text-small">
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
            disabled={cancel.isPending}
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

function statusWord(operation: Operation | null): string {
  if (operation?.status === 'waiting_agent') return 'Ждёт агента…'
  if (operation?.status === 'queued') return 'В очереди…'
  return 'Идёт…'
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
