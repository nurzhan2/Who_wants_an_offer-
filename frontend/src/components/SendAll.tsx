import { type ReactNode, useEffect, useId, useMemo, useRef, useState } from 'react'

import { ApiError } from '@/api/client'
import { Button, Field, Pill } from '@/components/ui'
import { useBatchPlan, useConfirmBatch } from '@/hooks/useBatch'
import { useOperations, useStartOperation } from '@/hooks/useOperations'
import { explain } from '@/lib/errors'
import { count, dateTime, plural, score as formatScore } from '@/lib/format'
import { ATS_OVERALL } from '@/lib/labels'
import type { BatchItem, BatchPlan, SetAsideItem } from '@/types/autopilot'
import type { OperationsState } from '@/types/operations'

/**
 * Отправить все: one screen, every letter in full, one confirmation.
 *
 * The change this screen is: the owner no longer answers per vacancy. They read
 * the batch, untick what they do not want, and press once. What it does *not*
 * change is what a confirmation means — each row is confirmed against its own
 * card digest, so a "yes" here is N separate yeses to N texts, and a letter that
 * moves between this screen and the send does not go.
 *
 * The button is deliberately two acts, like the single card's: tick that the
 * letters were read, then confirm. The tick is one for the batch, because the
 * letters are on this page and scrolling past them is the reading.
 *
 * Nothing is sent from here. Confirming records the yeses; the send is the
 * agent on the owner's own machine, started right after, and it re-reads every
 * vacancy page before it types anything.
 */
export function SendAll({ label = 'Отправить все' }: { label?: string }) {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button
        onClick={() => {
          setOpen(true)
        }}
      >
        {label}
      </Button>
      {open ? (
        <Modal
          onClose={() => {
            setOpen(false)
          }}
        >
          <Body />
        </Modal>
      ) : null}
    </>
  )
}

function Modal({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  const titleId = useId()
  const sheet = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    sheet.current?.focus()
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') onClose()
    }
    document.addEventListener('keydown', onKey)
    const overflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = overflow
      previous?.focus()
    }
  }, [onClose])

  return (
    <div
      className="veil fixed inset-0 z-50 flex items-start justify-center overflow-y-auto px-4 py-10"
      style={{ backgroundColor: 'color-mix(in srgb, var(--ink) 55%, transparent)' }}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      <div
        ref={sheet}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        className="sheet w-full max-w-4xl border border-ink bg-paper p-card text-ink outline-none"
      >
        <div className="mb-6 flex items-baseline justify-between gap-6 border-b border-hairline pb-4">
          <h2 id={titleId} className="text-heading font-semibold">
            Отправить все
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="text-small underline underline-offset-4"
          >
            закрыть
          </button>
        </div>
        {children}
      </div>
    </div>
  )
}

function Body() {
  const plan = useBatchPlan(true)

  if (plan.isPending) {
    return <p className="text-small text-muted">Собираем пачку — то же, что получит агент…</p>
  }
  if (plan.isError) {
    const { reason, remedy } = explain(plan.error)
    return (
      <div role="alert">
        <p className="font-semibold">{reason}</p>
        <p className="mt-1 text-small text-muted">{remedy}</p>
      </div>
    )
  }
  return <Batch plan={plan.data} />
}

function Batch({ plan }: { plan: BatchPlan }) {
  const [ticked, setTicked] = useState<Set<string>>(
    () => new Set(plan.items.slice(0, plan.limit).map((row) => row.vacancy_id)),
  )
  const [read, setRead] = useState(false)
  const confirm = useConfirmBatch()
  const start = useStartOperation()
  const operations = useOperations()
  const sending = operations.data?.busy.includes('send') ?? false
  const chosen = useMemo(
    () => plan.items.filter((row) => ticked.has(row.vacancy_id)),
    [plan.items, ticked],
  )
  const tooMany = chosen.length > plan.limit
  const refused = confirm.data?.outcomes.filter((outcome) => !outcome.confirmed) ?? []

  if (plan.items.length === 0) {
    return (
      <div>
        <p className="font-semibold">Отправлять нечего.</p>
        <p className="mt-1 text-small text-muted">
          Ни одна вакансия не прошла отбор целиком. Запустите цепочку на «Обзоре» — она соберёт
          вакансии, напишет письма и перечитает страницы. Что не прошло и почему — ниже.
        </p>
        <SetAside items={plan.set_aside} />
      </div>
    )
  }

  return (
    <div>
      <p className="text-small">
        Готово к отправке: <strong className="font-semibold">{plan.items.length}</strong>. Отмечено:{' '}
        <strong className="font-semibold">{chosen.length}</strong> из {plan.limit} возможных за раз.
      </p>
      <p className="mt-1 text-small text-muted">{plan.limit_reason}</p>
      {plan.last_sent_at ? (
        <p className="mt-1 text-small text-muted">
          Последний отклик ушёл {dateTime(plan.last_sent_at)}.
        </p>
      ) : null}

      <div className="mt-6 rise-list border-t border-hairline">
        {plan.items.map((row) => (
          <Row
            key={row.vacancy_id}
            row={row}
            ticked={ticked.has(row.vacancy_id)}
            onToggle={() => {
              setTicked((current) => {
                const next = new Set(current)
                if (next.has(row.vacancy_id)) next.delete(row.vacancy_id)
                else next.add(row.vacancy_id)
                return next
              })
            }}
          />
        ))}
      </div>

      <div className="mt-8 border-t border-ink pt-6">
        {confirm.isSuccess ? (
          <div>
            <p className="font-semibold">
              Подтверждено: {confirm.data.confirmed} из {chosen.length}.
            </p>
            {refused.length > 0 ? (
              <ul className="mt-2 list-disc space-y-1 pl-5 text-small">
                {refused.map((outcome) => (
                  <li key={outcome.vacancy_id} className="break-anywhere">
                    {outcome.detail ?? 'Эту вакансию подтвердить не удалось.'}
                  </li>
                ))}
              </ul>
            ) : null}
            <p className="mt-2 text-small text-muted">
              Отправляет агент на вашем компьютере: он открывает каждую вакансию заново, выдерживает
              паузы между откликами и не отправляет то, на что отклик уже есть.
            </p>
            {watcherAlive(operations.data) ? null : (
              <p className="mt-2 text-small text-muted">
                Локальный агент сейчас не запущен: запрос подождёт его. Запустите приложение через
                start.cmd — он поднимется вместе с ним, и отправка начнётся сама.
              </p>
            )}
            <div className="mt-4">
              <Button
                disabled={sending || start.isPending}
                onClick={() => {
                  start.mutate('send')
                }}
              >
                {sending ? 'Отправка идёт…' : 'Начать отправку'}
              </Button>
            </div>
            {start.isError ? (
              <p className="mt-3 text-small" role="alert">
                {explain(start.error).reason}
              </p>
            ) : null}
          </div>
        ) : (
          <div>
            <label className="flex cursor-pointer items-start gap-3 text-small">
              <input
                type="checkbox"
                className="mt-1 h-4 w-4 accent-current"
                checked={read}
                onChange={(event) => {
                  setRead(event.target.checked)
                }}
              />
              <span>
                Я прочитал(а) письма отмеченных вакансий целиком. Отправить именно эти тексты от
                моего имени.
              </span>
            </label>
            {tooMany ? (
              <p className="mt-3 text-small font-semibold" role="status">
                Отмечено больше, чем можно подтвердить за раз: снимите {chosen.length - plan.limit}{' '}
                {plural(chosen.length - plan.limit, 'галочку', 'галочки', 'галочек')}.
              </p>
            ) : null}
            <div className="mt-4">
              <Button
                disabled={!read || chosen.length === 0 || tooMany || confirm.isPending}
                onClick={() => {
                  confirm.mutate(
                    chosen.map((row) => ({
                      vacancy_id: row.vacancy_id,
                      card_digest: row.card_digest,
                    })),
                  )
                }}
              >
                {confirm.isPending
                  ? 'Записываем…'
                  : `Подтвердить ${String(chosen.length)} ${plural(chosen.length, 'отклик', 'отклика', 'откликов')}`}
              </Button>
            </div>
            {confirm.isError ? (
              <p className="mt-3 text-small font-semibold" role="alert">
                {confirm.error instanceof ApiError && confirm.error.status === 409
                  ? (confirm.error.detail ?? 'Пачку подтвердить не удалось.')
                  : explain(confirm.error).reason}
              </p>
            ) : null}
          </div>
        )}
      </div>

      <SetAside items={plan.set_aside} />
    </div>
  )
}

function Row({
  row,
  ticked,
  onToggle,
}: {
  row: BatchItem
  ticked: boolean
  onToggle: () => void
}) {
  const item = row.item
  return (
    <div className="border-b border-hairline py-6">
      <div className="flex items-start gap-3">
        <input
          type="checkbox"
          className="mt-1 h-4 w-4 accent-current"
          checked={ticked}
          onChange={onToggle}
          aria-label={`Отправить: ${item.title}`}
        />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1">
            <span className="break-anywhere font-semibold">{item.title}</span>
            <span className="tnum text-small text-muted">{formatScore(item.score)}</span>
            {row.confirmed ? <Pill>уже подтверждено</Pill> : null}
            {item.anonymous ? <Pill strong>работодатель скрыт</Pill> : null}
          </div>
          <p className="break-anywhere mt-1 text-small text-muted">
            {item.company ?? 'без компании'} · {item.source} {item.vacancy_id}
          </p>
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer noopener"
            className="break-anywhere mt-1 block text-small underline underline-offset-4"
          >
            {item.url}
          </a>
          {item.score_explanation ? (
            <p className="break-anywhere mt-2 text-small">{item.score_explanation}</p>
          ) : null}
          {item.ats ? (
            <p className="mt-1 text-small text-muted">
              Робот-фильтр: {ATS_OVERALL[item.ats.overall] ?? item.ats.overall}, {item.ats.score} из
              100
              {item.ats.requirements_total > 0
                ? ` · требований названо ${String(item.ats.requirements_present)} из ${String(item.ats.requirements_total)}`
                : ''}
            </p>
          ) : null}
          {item.hh_lines.length > 0 ? (
            <ul className="mt-2 space-y-1 text-small">
              {item.hh_lines.map((line) => (
                <li key={line} className="break-anywhere">
                  «{line}»
                </li>
              ))}
            </ul>
          ) : null}
          <div className="mt-3">
            <Field
              label={
                item.letter === null
                  ? 'без сопроводительного письма'
                  : `письмо целиком · ${count(item.letter.length)} ${plural(item.letter.length, 'знак', 'знака', 'знаков')}`
              }
            >
              {item.letter !== null ? (
                <p className="break-anywhere max-h-64 overflow-y-auto whitespace-pre-wrap border border-hairline p-4 text-small">
                  {item.letter}
                </p>
              ) : null}
            </Field>
          </div>
        </div>
      </div>
    </div>
  )
}

/** Mirrors the server's ninety-second rule, on this clock — same as the panel's. */
function watcherAlive(state: OperationsState | undefined): boolean {
  if (!state?.agent_seen_at) return false
  return Date.now() - new Date(state.agent_seen_at).getTime() < 90_000
}

/** What the selection did not let through, with the reason for each. */
export function SetAside({ items }: { items: SetAsideItem[] }) {
  if (items.length === 0) return null
  return (
    <div className="mt-8 border-t border-hairline pt-6">
      <p className="font-semibold">Посмотреть руками: {items.length}</p>
      <p className="mt-1 text-small text-muted">
        Эти вакансии агент не отправит. Ничего не выброшено — откройте и решите сами.
      </p>
      <ul className="mt-3 space-y-3">
        {items.map((item) => (
          <li key={item.vacancy_id} className="text-small">
            <a
              href={item.url}
              target="_blank"
              rel="noreferrer noopener"
              className="break-anywhere font-semibold underline underline-offset-4"
            >
              {item.title}
            </a>
            <span className="text-muted">
              {item.company ? ` · ${item.company}` : ''}
              {item.score ? ` · ${formatScore(item.score)}` : ''}
            </span>
            <p className="break-anywhere text-muted">{item.reason}</p>
          </li>
        ))}
      </ul>
    </div>
  )
}
