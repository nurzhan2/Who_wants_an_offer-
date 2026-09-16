import { useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import { useSaveTargetTitles, useSearchPlan } from '@/hooks/useSearchPlan'
import type { SearchBasis, SearchUse, SourceSearchPreview } from '@/types/searchPlan'

/** The server's own limits, repeated so the form refuses before the request does. */
const MAX_TITLES = 12
const MAX_TITLE_CHARS = 100

const BASIS: Record<SearchBasis, string> = {
  titles: 'Ищем по вашим названиям должностей.',
  skills: 'Поле пустое — ищем по ключевым словам из навыков резюме, как раньше.',
  headline: 'Навыки не распознаны — ищем по заголовку резюме.',
  none: 'Искать не по чему: нет ни названий, ни распознанных навыков.',
}

const USE: Record<SearchUse, string> = {
  catalog: 'первыми откроются страницы каталога',
  query: 'уйдут поисковые запросы',
  filter: 'лента фильтруется по словам',
}

/** Case-insensitive equality of two title lists, in order. */
function same(one: string[], other: string[]): boolean {
  return (
    one.length === other.length &&
    one.every((title, index) => title.toLowerCase() === (other[index] ?? '').toLowerCase())
  )
}

/**
 * «Какие вакансии ищу»: the job titles the owner wants to be hired as.
 *
 * Not a filter and not a skill list — the answer to "who do I want to work as",
 * kept apart from "what can I do". The titles pick hh's catalogue pages and go
 * to jsearch verbatim as queries; an empty list leaves the search on skill
 * keywords, as before.
 *
 * Below the editor is what the next run will do with them, per source, because
 * a field whose effect cannot be seen is guesswork.
 */
export function TargetTitles({ profileId, stored }: { profileId: string; stored: string[] }) {
  const [titles, setTitles] = useState<string[]>(stored)
  const [draft, setDraft] = useState('')
  const save = useSaveTargetTitles(profileId)
  const plan = useSearchPlan()

  // Re-seeded when the server's copy changes, which after the first load means
  // "a save came back" — the same rule the contact form below follows.
  useEffect(() => {
    setTitles(stored)
  }, [stored])

  const dirty = !same(titles, stored)
  const typed = draft.trim().replace(/\s+/g, ' ')
  const duplicate = titles.some((title) => title.toLowerCase() === typed.toLowerCase())
  const canAdd =
    typed !== '' && !duplicate && typed.length <= MAX_TITLE_CHARS && titles.length < MAX_TITLES

  function add() {
    if (!canAdd) return
    setTitles((current) => [...current, typed])
    setDraft('')
  }

  return (
    <section className="flex flex-col gap-6">
      <header className="flex flex-col gap-2 border-b border-hairline pb-4">
        <h2 className="text-xl tracking-tight text-ink">Какие вакансии ищу</h2>
        <p className="max-w-2xl text-sm text-muted">
          Названия должностей — кем вы хотите работать: «Python Developer», «Backend разработчик».
          Это не фильтр и не навыки. По ним hh выбирает разделы каталога, а jsearch, arbeitnow и
          remotive ищут вакансии. Пустое поле — поиск по навыкам из резюме.
        </p>
      </header>

      <ul className="flex flex-wrap gap-3" aria-label="Названия должностей">
        {titles.map((title) => (
          <li
            key={title}
            className="flex items-center gap-2 rounded-pill border border-ink py-1 pl-4 pr-1 text-sm"
          >
            <span>{title}</span>
            <button
              type="button"
              aria-label={`Убрать «${title}»`}
              className="rounded-pill px-2 text-muted transition-colors hover:bg-ink hover:text-paper"
              onClick={() => {
                setTitles((current) => current.filter((item) => item !== title))
              }}
            >
              ×
            </button>
          </li>
        ))}
        {titles.length === 0 ? <li className="text-sm text-muted">Пока ничего не указано.</li> : null}
      </ul>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex min-w-64 flex-1 flex-col gap-2">
          <span className="text-label uppercase text-muted">Название должности</span>
          <input
            value={draft}
            maxLength={MAX_TITLE_CHARS}
            placeholder="Python Developer"
            className="w-full rounded-field border border-hairline bg-paper px-4 py-3 text-ink outline-none transition-colors placeholder:text-muted/60 focus:border-ink"
            onChange={(event) => {
              setDraft(event.target.value)
            }}
            onKeyDown={(event) => {
              // Enter adds a title. It must not reach the contact form this
              // section sits in, which would submit on it.
              if (event.key === 'Enter') {
                event.preventDefault()
                add()
              }
            }}
          />
        </label>
        <button
          type="button"
          disabled={!canAdd}
          className="rounded-pill border border-ink px-6 py-3 text-label uppercase transition-colors enabled:hover:bg-ink enabled:hover:text-paper disabled:cursor-not-allowed disabled:border-hairline disabled:text-muted"
          onClick={add}
        >
          Добавить
        </button>
        <button
          type="button"
          disabled={!dirty || save.isPending}
          className="rounded-pill bg-ink px-10 py-3 text-label uppercase text-paper transition-colors disabled:cursor-not-allowed disabled:bg-hairline disabled:text-muted"
          onClick={() => {
            save.mutate(titles)
          }}
        >
          {save.isPending ? 'Сохраняем…' : 'Сохранить названия'}
        </button>
      </div>
      {duplicate && typed !== '' ? (
        <p className="text-sm text-muted">Такое название уже есть.</p>
      ) : null}
      {titles.length >= MAX_TITLES ? (
        <p className="text-sm text-muted">Не больше {MAX_TITLES} названий.</p>
      ) : null}
      {save.isError ? (
        <p className="text-sm text-ink">
          {save.error instanceof ApiError
            ? (save.error.detail ?? `Не удалось сохранить (${String(save.error.status)}).`)
            : 'Не удалось сохранить.'}
        </p>
      ) : null}
      {dirty && !save.isPending ? (
        <p className="text-sm text-muted">
          Есть несохранённые правки — план ниже изменится после сохранения.
        </p>
      ) : null}

      <div className="flex flex-col gap-4 border-t border-hairline pt-6">
        <h3 className="text-label uppercase text-muted">Что пойдёт в поиск на следующем прогоне</h3>
        {plan.isPending ? <p className="text-sm text-muted">Считаем план…</p> : null}
        {plan.isError ? <p className="text-sm text-muted">План поиска недоступен.</p> : null}
        {plan.data ? (
          <>
            <p className="text-sm">
              {BASIS[plan.data.basis]} Запросов в плане: {plan.data.queries}
              {plan.data.dropped > 0
                ? `, ещё ${String(plan.data.dropped)} не поместились в лимит прогона`
                : ''}
              .
            </p>
            <div className="grid gap-px border border-hairline bg-hairline md:grid-cols-2">
              {plan.data.sources.map((source) => (
                <SourcePlan key={source.slug} source={source} />
              ))}
            </div>
          </>
        ) : null}
      </div>
    </section>
  )
}

function SourcePlan({ source }: { source: SourceSearchPreview }) {
  const { preview } = source
  return (
    <article className="flex flex-col gap-3 bg-paper p-5">
      <header className="flex flex-wrap items-baseline justify-between gap-2">
        <span className="font-semibold">{source.slug}</span>
        <span className="text-xs text-muted">{USE[preview.use]}</span>
      </header>
      {!source.enabled && source.inactive ? (
        <p className="text-xs text-muted">Сейчас не запустится: {source.inactive.detail}</p>
      ) : null}
      {preview.terms.length > 0 ? (
        <ol className="flex flex-col gap-1 text-sm">
          {preview.terms.map((term, index) => (
            <li key={term} className="flex gap-3">
              <span className="tnum w-5 shrink-0 text-right text-muted">{index + 1}</span>
              <span className="min-w-0 break-words">{term}</span>
            </li>
          ))}
        </ol>
      ) : (
        <p className="text-sm text-muted">Ничего.</p>
      )}
      {preview.more > 0 ? <p className="text-xs text-muted">и ещё {preview.more}</p> : null}
      {preview.note ? <p className="text-xs text-muted">{preview.note}</p> : null}
    </article>
  )
}
