import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from 'react'

import { ApiError } from '@/api/client'
import { Freshness } from '@/components/Freshness'
import { Button, Field, Pill } from '@/components/ui'
import { useConfirm, useConfirmationCard, useWithdraw } from '@/hooks/useConfirmations'
import { useOperations, useStartOperation } from '@/hooks/useOperations'
import { explain } from '@/lib/errors'
import { count, dateTime, plural, score as formatScore } from '@/lib/format'
import { ATS_OVERALL, BUCKETS } from '@/lib/labels'
import type { ConfirmationCard, QueueItem } from '@/types/confirmations'

/**
 * Откликнуться: the confirmation the terminal used to ask for, in a modal.
 *
 * The card is the queue item the agent would be handed — the whole letter, the
 * score with its reasons, the ATS summary, what hh said before — and the
 * confirmation is bound to a digest of it on the server. Nothing leaves from
 * here: a confirmed application is sent by the agent on the owner's machine,
 * after it reads the vacancy page again.
 *
 * Two deliberate acts, like the terminal's flag plus typed word: tick that the
 * letter was read, then confirm. For any source but hh the card only links to
 * the original posting.
 */
export function ApplyConfirm({ vacancyId }: { vacancyId: string }) {
  const [open, setOpen] = useState(false)
  const close = useCallback(() => {
    setOpen(false)
  }, [])
  return (
    <>
      <Button
        onClick={() => {
          setOpen(true)
        }}
      >
        Откликнуться…
      </Button>
      {open ? (
        <Modal onClose={close}>
          <CardBody vacancyId={vacancyId} />
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
        className="sheet w-full max-w-3xl border border-ink bg-paper p-card text-ink outline-none"
      >
        <div className="mb-6 flex items-baseline justify-between gap-6 border-b border-hairline pb-4">
          <h2 id={titleId} className="text-heading font-semibold">
            Подтверждение отклика
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

function CardBody({ vacancyId }: { vacancyId: string }) {
  const card = useConfirmationCard(vacancyId, true)

  if (card.isPending) {
    return <p className="text-small text-muted">Собираем карточку — то же, что получит агент…</p>
  }
  if (card.isError) {
    const { reason, remedy } = explain(card.error)
    return (
      <div role="alert">
        <p className="font-semibold">{reason}</p>
        <p className="mt-1 text-small text-muted">{remedy}</p>
      </div>
    )
  }
  const data = card.data
  return (
    <div>
      <Head card={data} />
      {data.blockers.length > 0 || data.item === null || data.card_digest === null ? (
        <Blocked card={data} />
      ) : (
        <Confirmable card={data} item={data.item} digest={data.card_digest} />
      )}
    </div>
  )
}

function Head({ card }: { card: ConfirmationCard }) {
  return (
    <div className="mb-6">
      <p className="break-anywhere text-heading font-light">{card.title}</p>
      <p className="break-anywhere mt-1 text-small text-muted">
        {card.company ?? 'без компании'}
        {card.external_id ? ` · ${card.source ?? ''} ${card.external_id}` : ''}
      </p>
      {card.url ? (
        <a
          href={card.url}
          target="_blank"
          rel="noreferrer noopener"
          className="break-anywhere mt-2 block text-small underline underline-offset-4"
        >
          {card.url}
        </a>
      ) : null}
      <div className="mt-3">
        <Freshness
          publishedAt={card.published_at}
          lastSeenAt={card.last_seen_at}
          active={card.is_active}
        />
      </div>
      {card.resume_notice ? (
        <div className="invert-surface mt-4 p-4 text-small">
          <p className="font-semibold">
            hh предупреждает о видимости резюме — это касается всех откликов
          </p>
          <p className="break-anywhere mt-1">«{card.resume_notice}»</p>
          <p className="mt-1 opacity-80">
            Отклик уйдёт — hh такие принимает, — но работодатель может не увидеть резюме.
          </p>
        </div>
      ) : null}
    </div>
  )
}

function Blocked({ card }: { card: ConfirmationCard }) {
  return (
    <div className="border border-ink p-4" role="status">
      <p className="font-semibold">Этот отклик сейчас нельзя подтвердить</p>
      <ul className="mt-2 list-disc space-y-1 pl-5 text-small">
        {card.blockers.map((line) => (
          <li key={line} className="break-anywhere">
            {line}
          </li>
        ))}
      </ul>
      {card.url && card.external_id === null ? (
        <div className="mt-4">
          <a
            href={card.url}
            target="_blank"
            rel="noreferrer noopener"
            className="inline-block rounded-pill border border-ink bg-ink px-6 py-2 text-small text-paper transition-colors duration-800 ease-slow hover:bg-paper hover:text-ink"
          >
            Открыть вакансию на {card.source ?? 'сайте'}
          </a>
        </div>
      ) : null}
    </div>
  )
}

function Confirmable({
  card,
  item,
  digest,
}: {
  card: ConfirmationCard
  item: QueueItem
  digest: string
}) {
  const [read, setRead] = useState(false)
  const confirm = useConfirm(card.vacancy_id)
  const withdraw = useWithdraw(card.vacancy_id)
  const operations = useOperations()
  const start = useStartOperation()
  const state = card.state
  const sending = operations.data?.busy.includes('send') ?? false

  return (
    <div>
      <ScoreBlock item={item} />
      <AtsBlock item={item} />
      {item.hh_lines.length > 0 ? (
        <div className="mt-6">
          <Field label="hh уже говорил об этой вакансии">
            <ul className="space-y-1 text-small">
              {item.hh_lines.map((line) => (
                <li key={line} className="break-anywhere">
                  «{line}»
                </li>
              ))}
            </ul>
          </Field>
        </div>
      ) : null}
      {item.anonymous || item.employer_on_additional_check ? (
        <p className="mt-4 flex flex-wrap gap-2">
          {item.anonymous ? <Pill strong>работодатель скрыт</Pill> : null}
          {item.employer_on_additional_check ? <Pill strong>hh проверяет работодателя</Pill> : null}
        </p>
      ) : null}

      <div className="mt-6">
        <Field
          label={
            item.letter === null
              ? 'без сопроводительного письма'
              : `письмо целиком · ${count(item.letter.length)} ${plural(item.letter.length, 'знак', 'знака', 'знаков')}`
          }
        >
          {item.letter !== null ? (
            <p className="break-anywhere max-h-80 overflow-y-auto whitespace-pre-wrap border border-hairline p-4 text-small">
              {item.letter}
            </p>
          ) : null}
        </Field>
      </div>

      <div className="mt-8 border-t border-ink pt-6">
        {state?.valid ? (
          <div>
            <p className="font-semibold">Подтверждено {dateTime(state.confirmed_at)}.</p>
            <p className="mt-1 text-small text-muted">
              Отправит агент на вашем компьютере: он ещё раз откроет вакансию и не отправит, если
              отклик уже есть. Подтверждение действует {card.ttl_hours} ч.
            </p>
            <div className="mt-4 flex flex-wrap gap-3">
              <Button
                disabled={sending || start.isPending}
                onClick={() => {
                  start.mutate('send')
                }}
              >
                {sending ? 'Отправка идёт…' : 'Отправить подтверждённые сейчас'}
              </Button>
              <Button
                outline
                disabled={withdraw.isPending}
                onClick={() => {
                  withdraw.mutate()
                }}
              >
                Отозвать подтверждение
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
            {state && !state.valid ? (
              <p className="mb-4 text-small font-semibold" role="status">
                {state.reason}
              </p>
            ) : null}
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
                Я прочитал(а) карточку и письмо целиком. Отправить именно этот текст на эту
                вакансию от моего имени.
              </span>
            </label>
            <div className="mt-4 flex flex-wrap gap-3">
              <Button
                disabled={!read || confirm.isPending}
                onClick={() => {
                  confirm.mutate(digest)
                }}
              >
                {confirm.isPending ? 'Записываем…' : 'Подтвердить отклик'}
              </Button>
            </div>
            {confirm.isError ? (
              <p className="mt-3 text-small font-semibold" role="alert">
                {confirm.error instanceof ApiError && confirm.error.status === 409
                  ? (confirm.error.detail ?? 'Карточка изменилась — перечитайте её.')
                  : explain(confirm.error).reason}
              </p>
            ) : null}
          </div>
        )}
      </div>
    </div>
  )
}

function ScoreBlock({ item }: { item: QueueItem }) {
  const match = item.match
  return (
    <div className="grid gap-6 sm:grid-cols-[auto_minmax(0,1fr)]">
      <div>
        <div className="tnum text-title font-light">{formatScore(item.score)}</div>
        <div className="text-micro uppercase text-muted">
          {match ? BUCKETS[match.bucket] : 'не оценено'}
        </div>
      </div>
      <div className="min-w-0 text-small">
        {item.score_explanation ? (
          <p className="break-anywhere">{item.score_explanation}</p>
        ) : (
          <p className="text-muted">Объяснения к оценке нет.</p>
        )}
        {match && match.matched_skills.length > 0 ? (
          <p className="break-anywhere mt-2 text-muted">
            совпало: {match.matched_skills.map((skill) => skill.canonical_name).join(', ')}
          </p>
        ) : null}
        {match && match.missing_required.length > 0 ? (
          <p className="break-anywhere mt-1 font-semibold">
            не хватает обязательного:{' '}
            {match.missing_required.map((skill) => skill.canonical_name).join(', ')}
          </p>
        ) : null}
      </div>
    </div>
  )
}

function AtsBlock({ item }: { item: QueueItem }) {
  const ats = item.ats
  if (ats === null) {
    return <p className="mt-4 text-small text-muted">Проверка письма роботом-фильтром не выполнялась.</p>
  }
  return (
    <div className="mt-4 text-small">
      <p>
        Робот-фильтр: <strong className="font-semibold">{ATS_OVERALL[ats.overall] ?? ats.overall}</strong>
        {`, ${String(ats.score)} из 100`}
        {ats.requirements_total > 0
          ? ` · требований названо ${String(ats.requirements_present)} из ${String(ats.requirements_total)}`
          : ''}
      </p>
      {ats.unstated.length > 0 ? (
        <p className="break-anywhere mt-1 text-muted">
          есть в профиле, но не названо в письме: {ats.unstated.join(', ')}
        </p>
      ) : null}
      {ats.critical.map((line) => (
        <p key={line} className="mt-1 font-semibold">
          {line}
        </p>
      ))}
    </div>
  )
}
