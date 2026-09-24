import { ApiError } from '@/api/client'
import { SendAll } from '@/components/SendAll'
import { Button, Section } from '@/components/ui'
import { useSetAside } from '@/hooks/useBatch'
import { useCompletion, useNow } from '@/hooks/useMotion'
import { isFinished, useOperations, useStartOperation } from '@/hooks/useOperations'
import { explain } from '@/lib/errors'
import { ago, dateTime, plural } from '@/lib/format'
import type { ChainStep, Operation } from '@/types/operations'

/**
 * Автопилот: one press for the whole day, one confirmation for the whole batch.
 *
 * The two buttons are deliberately not one. «Цепочка» collects, computes,
 * scores, writes the letters and builds the queue — none of which needs a
 * person, all of which takes the better part of an hour. «Отправить все» is
 * where a person reads what is about to go out under their name and says yes.
 * A single button doing both would be the thing this whole phase refuses to
 * build, and there is no setting anywhere that joins them.
 *
 * The steps are drawn as a list rather than as a bar because they are minutes
 * apart and of wildly different lengths: the crawl is about twenty minutes and
 * the queue is seconds, so one percentage would sit at nothing for a third of
 * an hour and then jump. The same reason the server reports per step.
 */
export function Autopilot() {
  const state = useOperations()
  const start = useStartOperation()
  const batch = useSetAside()
  const chain = state.data?.operations.find((operation) => operation.kind === 'chain') ?? null
  const busy = state.data?.busy.includes('chain') ?? false
  const finishedWell = useCompletion(busy, chain?.status === 'success')
  const ready = batch.data?.items.length ?? 0

  return (
    <Section
      title="Автопилот"
      note="Цепочка делает всю подготовку подряд и ничего не отправляет. Отправка начинается только с вашего подтверждения — одного на всю пачку."
      action={<SendAll />}
    >
      <div className="grid gap-6 md:grid-cols-[minmax(0,1fr)_auto]">
        <div className="min-w-0">
          <p className="max-w-2xl text-small text-muted">
            Собрать вакансии → досчитать эмбеддинги → пересчитать подбор → перечитать страницы →
            написать письма прошедшим отбор → собрать очередь. Идёт долго: замеренный обход hh
            занимал здесь от часа до трёх, эмбеддинги на процессоре — ещё до получаса. Окно можно
            закрыть, работа идёт на сервере.
          </p>
          <p className="mt-1 max-w-2xl text-small text-muted">
            То же самое из терминала: <code>python -m wwao chain</code>. Как запускать её по
            расписанию — в README.
          </p>
          {batch.data ? (
            <p className="mt-3 text-small">
              {ready > 0 ? (
                <>
                  Готово к отправке: <strong className="font-semibold">{ready}</strong>{' '}
                  {plural(ready, 'отклик', 'отклика', 'откликов')}
                  {batch.data.set_aside.length > 0
                    ? ` · в «посмотреть руками»: ${String(batch.data.set_aside.length)}`
                    : ''}
                  .
                </>
              ) : (
                <>
                  Готовых к отправке нет
                  {batch.data.set_aside.length > 0
                    ? `, в «посмотреть руками» — ${String(batch.data.set_aside.length)}`
                    : ''}
                  . Нажмите «Отправить все», чтобы увидеть причины.
                </>
              )}
            </p>
          ) : null}
          {start.isError ? (
            <p className="mt-3 text-small font-semibold" role="alert">
              {startFailure(start.error)}
            </p>
          ) : null}
          {chain ? <ChainRun operation={chain} /> : null}
        </div>
        <div className="flex items-start md:justify-end">
          <Button
            onClick={() => {
              start.mutate('chain')
            }}
            disabled={state.data === undefined}
            busy={busy ? 'Цепочка идёт…' : start.isPending ? 'Запускаем…' : false}
            done={finishedWell ? 'Готово' : false}
          >
            Запустить цепочку
          </Button>
        </div>
      </div>
    </Section>
  )
}

function startFailure(error: unknown): string {
  if (error instanceof ApiError && error.status === 409) {
    return error.detail ?? 'Цепочка уже идёт.'
  }
  const { reason, remedy } = explain(error)
  return `${reason} ${remedy}`
}

/** The last chain: its own line, then one row per step. */
function ChainRun({ operation }: { operation: Operation }) {
  const finished = isFinished(operation)
  const now = useNow(!finished)
  const since = operation.started_at ?? operation.queued_at

  return (
    <div className="mt-6 border-l border-ink pl-4" aria-live="polite">
      <p
        key={operation.message}
        className={`swap break-anywhere text-small ${operation.status === 'failed' ? 'font-semibold' : ''}`}
      >
        {operation.message}
      </p>
      <p className="mt-1 text-small text-muted">
        {finished ? (
          `${dateTime(operation.finished_at)} · ${ago(operation.finished_at)}`
        ) : (
          <>
            {operation.status === 'running' ? 'идёт ' : 'ждёт '}
            <span className="tnum">{clock(now - new Date(since).getTime())}</span>
          </>
        )}
      </p>
      {operation.steps.length > 0 ? (
        <ol className="rise-list mt-4 border-t border-hairline">
          {operation.steps.map((step, index) => (
            <StepLine key={step.key} step={step} position={index + 1} />
          ))}
        </ol>
      ) : null}
      {operation.report.length > 0 ? (
        <ul className="rise-list mt-4 space-y-1 text-small">
          {operation.report.map((line, index) => (
            <li key={index} className="break-anywhere">
              {line}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

/**
 * One step.
 *
 * Standing is said by word and weight, never by colour — the running step is
 * the one in semibold, a finished one is plain ink, one still ahead is muted.
 * The same rule the operations panel below follows, because they are the same
 * five steps seen twice.
 */
function StepLine({ step, position }: { step: ChainStep; position: number }) {
  const running = step.status === 'running'
  const ahead = step.status === 'pending'
  return (
    <li className="grid gap-3 border-b border-hairline py-3 md:grid-cols-[2.5rem_minmax(0,1fr)_auto]">
      <div className={`text-small ${running ? 'font-semibold' : ahead ? 'text-muted' : ''}`}>
        <span className="tnum">{String(position).padStart(2, '0')}</span>
      </div>
      <div className="min-w-0">
        <span className={`text-small ${running ? 'font-semibold' : ahead ? 'text-muted' : ''}`}>
          {step.title}
        </span>
        {step.note ? (
          <p key={step.note} className="swap break-anywhere mt-1 text-small text-muted">
            {step.note}
          </p>
        ) : null}
        {step.report.length > 0 ? (
          <ul className="mt-1 space-y-1 text-small text-muted">
            {step.report.map((line, index) => (
              <li key={index} className="break-anywhere">
                {line}
              </li>
            ))}
          </ul>
        ) : null}
      </div>
      <div
        className={`text-micro uppercase md:justify-self-end ${running || step.status === 'failed' ? 'font-semibold' : 'text-muted'}`}
      >
        {STEP_STANDING[step.status]}
      </div>
    </li>
  )
}

const STEP_STANDING: Record<ChainStep['status'], string> = {
  pending: 'впереди',
  running: 'идёт',
  done: 'пройден',
  failed: 'ошибка',
  skipped: 'уже сделан',
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
