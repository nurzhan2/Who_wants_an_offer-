import { useState } from 'react'

import { href } from '@/app/routes'
import { ApplyConfirm } from '@/components/ApplyConfirm'
import { Freshness } from '@/components/Freshness'
import { Button, Card, Empty, Failure, Field, Loading, NextStep, Pill, Section } from '@/components/ui'
import { useBoard } from '@/hooks/queries'
import { useOperations, useStartOperation } from '@/hooks/useOperations'
import { count, date, plural, score } from '@/lib/format'
import { APPLICATION_STATUS, OUTCOMES, outcomeLabel, STAGE_NOTES, STAGES } from '@/lib/labels'
import type { BoardCard, BoardColumn } from '@/types/api'

/**
 * Отклики: a kanban on two axes.
 *
 * Where an application is in *this* project (queued, waiting for a person,
 * sent) and what hh has since said about it are different questions, and a sent
 * application has an answer to both — so there are two rows of columns and a
 * sent application appears in one of each. Collapsing them would either lose
 * the send or invent an outcome.
 *
 * **Statuses are encoded by position, label and weight — never by colour.**
 * That is the brief's rule and it costs nothing here: a column already says
 * where a card is, and the one thing worth shouting (an application that
 * actually went out) is said in semibold.
 */
export function Applications() {
  const { data, isPending, isError, error, refetch } = useBoard()

  if (isPending) return <Loading what="отклики" />
  if (isError) {
    return (
      <Failure
        error={error}
        what="отклики"
        onRetry={() => {
          void refetch()
        }}
      />
    )
  }

  const nothing =
    data.stages.every((column) => column.cards.length === 0) && data.other.cards.length === 0

  return (
    <div className="rise">
      <Section
        title="Отклики"
        note="Слева направо — путь письма внутри проекта. Отклик уходит только после вашего подтверждения в карточке вакансии; «отправлено» — только то, что подтвердил сам hh."
        action={<RefreshOutcomes />}
      >
        {nothing ? (
          <NextStep
            title="Трекер пуст"
            action={
              <a
                href={href('overview')}
                className="rounded-pill border border-ink px-6 py-2 text-small transition-colors duration-800 ease-slow hover:bg-ink hover:text-paper"
              >
                К операциям
              </a>
            }
          >
            Писем ещё не писали и откликов не отправляли. На «Обзоре» нажмите «Написать письма» —
            вакансии с письмами появятся здесь в колонке «в очереди».
          </NextStep>
        ) : (
          <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-4">
            {data.stages.map((column) => (
              <Column key={column.key} column={column} label={STAGES[column.key] ?? column.key} />
            ))}
          </div>
        )}
        {data.other.cards.length > 0 ? (
          <div className="mt-8">
            <Column column={data.other} label={STAGES.other ?? 'вне очереди'} />
          </div>
        ) : null}
      </Section>

      <Section
        title="Исходы"
        note="Слова hh, а не наши. Незнакомое состояние показывается как есть: их словарь открыт, и «нет ответа» вместо нового исхода было бы выдумкой."
      >
        {data.outcomes.length === 0 ? (
          <Empty>
            hh пока ничего не ответил ни по одному отклику. Это не «отказы» и не «молчание» — просто
            измерять нечего.
          </Empty>
        ) : (
          <div className="grid gap-6 md:grid-cols-2 xl:grid-cols-4">
            {data.outcomes.map((column) => (
              <Column key={column.key} column={column} label={OUTCOMES[column.key] ?? column.key} />
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}

/** The outcomes walk, from the screen where its result is read. */
function RefreshOutcomes() {
  const operations = useOperations()
  const start = useStartOperation()
  const busy = operations.data?.busy.includes('outcomes') ?? false
  return (
    <Button
      outline
      disabled={busy || start.isPending || operations.data === undefined}
      title="Агент откроет ваши отправленные отклики на hh и прочитает ответ"
      onClick={() => {
        start.mutate('outcomes')
      }}
    >
      {busy ? 'Исходы обновляются…' : 'Обновить исходы'}
    </Button>
  )
}

function Column({ column, label }: { column: BoardColumn; label: string }) {
  return (
    <div>
      <div className="mb-4 flex items-baseline justify-between border-b border-hairline pb-2">
        <h3 className={`text-small uppercase tracking-widest ${column.key === 'sent' ? 'font-semibold' : ''}`}>
          {label}
        </h3>
        <span className="tnum text-small text-muted">{count(column.cards.length)}</span>
      </div>
      {STAGE_NOTES[column.key] ? (
        <p className="mb-4 text-small text-muted">{STAGE_NOTES[column.key]}</p>
      ) : null}
      <div className="rise-list space-y-4">
        {column.cards.length === 0 ? <Empty>Пусто.</Empty> : null}
        {column.cards.map((card) => (
          <ApplicationCard key={`${column.key}-${card.id}`} card={card} />
        ))}
      </div>
    </div>
  )
}

function ApplicationCard({ card }: { card: BoardCard }) {
  const [open, setOpen] = useState(false)
  const letter = card.sent_letter ?? card.cover_letter
  // Read off the row, not off the column it is being drawn in: the same
  // application appears in a stage column and in an outcome column, and a card
  // that decided "was this sent" from its surroundings said different things
  // about one row in two places on the same screen.
  const sent = card.sent_at !== null && card.send_confirmed
  const unconfirmed = card.sent_at !== null && !card.send_confirmed
  const claimsSend = card.agent_status === 'sent' && card.sent_at === null
  const archivedByAgent =
    card.agent_status === 'skipped' && (card.agent_reason ?? '').toLowerCase().includes('архив')

  return (
    <Card className="min-w-0 p-6">
      <a href={href('vacancies', card.vacancy_id)} className="block">
        <div className={`break-anywhere ${sent ? 'font-semibold' : ''}`}>{card.title}</div>
        <div className="break-anywhere text-small text-muted">{card.company ?? 'без компании'}</div>
      </a>
      <div className="mt-3">
        <Freshness
          publishedAt={card.vacancy_published_at}
          lastSeenAt={card.vacancy_last_seen_at}
          active={card.vacancy_active}
          archivedByAgent={archivedByAgent}
        />
      </div>

      <div className="mt-4 grid grid-cols-2 gap-4">
        {/* The snapshot the send took, and only for a row that has one: an
            unsent letter has no "score при отправке", and a dash under that
            label reads as a measurement that failed rather than as one nobody
            has taken yet. */}
        {card.match_score !== null ? (
          <Field label="score при отправке">
            <span className="tnum">{score(card.match_score)}</span>
          </Field>
        ) : null}
        <Field label={card.sent_at !== null ? 'отправлено' : 'статус'}>
          {card.sent_at !== null
            ? date(card.sent_at)
            : card.status === 'saved'
              ? 'не отправлено'
              : APPLICATION_STATUS[card.status]}
        </Field>
      </div>

      {card.agent_reason ? (
        <p className="break-anywhere mt-4 border-t border-hairline pt-4 text-small">
          <span className="text-muted">причина: </span>
          {card.agent_reason}
        </p>
      ) : null}

      {card.hh_warning ? (
        <p className="break-anywhere mt-4 text-small text-muted">
          hh при отправке: «{card.hh_warning}»
        </p>
      ) : null}
      {card.hh_blocking_warning ? (
        <p className="break-anywhere mt-2 text-small text-muted">
          hh про аккаунт: «{card.hh_blocking_warning}»
        </p>
      ) : null}

      {card.hh_last_state ? (
        <p className="mt-4 text-small">
          Исход: <strong className="font-semibold">{outcomeLabel(card.hh_last_state)}</strong>
          {card.hh_last_state_at ? ` · ${date(card.hh_last_state_at)}` : ''}
        </p>
      ) : null}

      {card.vacancy_key_skills !== null ? (
        <p className="break-anywhere mt-4 text-small text-muted">
          вакансия просила: {card.vacancy_key_skills.join(', ') || '— ничего не перечислила'}
        </p>
      ) : null}

      {letter ? (
        <>
          <button
            type="button"
            className="mt-4 text-small underline underline-offset-4"
            onClick={() => {
              setOpen((value) => !value)
            }}
          >
            {open
              ? 'скрыть письмо'
              : `письмо целиком (${count(letter.length)} ${plural(letter.length, 'знак', 'знака', 'знаков')})`}
          </button>
          {open ? (
            <>
              <p className="break-anywhere mt-3 max-h-96 overflow-y-auto whitespace-pre-wrap border-t border-hairline pt-4 text-small">
                {letter}
              </p>
              {card.sent_letter !== null && card.cover_letter !== null &&
              card.sent_letter !== card.cover_letter ? (
                <p className="mt-3 text-small text-muted">
                  Показан текст, который действительно ушёл. В базе лежит и более новая версия
                  письма — её перезаписала генерация уже после отправки.
                </p>
              ) : null}
            </>
          ) : null}
        </>
      ) : (
        <p className="mt-4 text-small text-muted">Письма по этой строке нет.</p>
      )}

      {card.hh_negotiations_total !== null ? (
        <p className="mt-4 text-small text-muted">
          hh насчитал откликов на вакансии: {count(card.hh_negotiations_total)}
        </p>
      ) : null}

      {unconfirmed ? (
        <p className="mt-4">
          <Pill strong>hh не подтвердил</Pill>
          <span className="mt-2 block text-small text-muted">
            Агент отчитался об отправке {date(card.sent_at)}, но числа откликов от hh по этой
            вакансии в базе нет. Нажмите «Обновить исходы» — агент перечитает страницу вакансии. Не
            отправляйте повторно, пока hh не ответил.
          </span>
        </p>
      ) : null}

      {card.sent_at === null && card.agent_status !== 'skipped' && letter ? (
        <div className="mt-4">
          {card.confirmed_at ? (
            <p className="mb-3 text-small">
              <strong className="font-semibold">Подтверждено {date(card.confirmed_at)}</strong> —
              ждёт отправки агентом.
            </p>
          ) : null}
          <ApplyConfirm vacancyId={card.vacancy_id} />
        </div>
      ) : null}

      {claimsSend ? (
        <p className="mt-4">
          <Pill strong>отправку не подтвердил</Pill>
          <span className="mt-2 block text-small text-muted">
            Агент дошёл до статуса «sent», но даты отправки не записал. Считать такую строку
            отправленной нельзя: дату пишет только тот процесс, который печатал письмо.
          </span>
        </p>
      ) : null}
    </Card>
  )
}
