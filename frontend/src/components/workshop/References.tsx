/**
 * Reference documents: the list, and the form that adds one.
 *
 * The form takes a file or pasted text, never both, because the endpoint does —
 * two ways of giving one document, and only one of them can be the stored one.
 *
 * The upload's warnings are rendered where the upload happened and not swallowed
 * into a toast. "This PDF is laid out in two columns and came out interleaved"
 * has to reach the person while they still have the file open and can paste the
 * text instead; a warning shown after they have navigated away is a warning
 * nobody acts on.
 */
import { useState } from 'react'

import { REFERENCE_KIND_LABELS } from '@/components/workshop/labels'
import type { Reference, ReferenceKind } from '@/types/workshop'

const FIELD =
  'w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-slate-900 focus:border-slate-500 focus:outline-none'
const LABEL = 'block text-xs font-medium uppercase tracking-wide text-slate-500'

interface FormProps {
  busy: boolean
  error: unknown
  warnings: string[]
  onSubmit: (input: {
    kind: ReferenceKind
    title: string
    note: string
    file: File | null
    text: string
  }) => void
}

export function ReferenceForm({ busy, error, warnings, onSubmit }: FormProps) {
  const [kind, setKind] = useState<ReferenceKind>('cover_letter')
  const [title, setTitle] = useState('')
  const [note, setNote] = useState('')
  const [text, setText] = useState('')
  const [file, setFile] = useState<File | null>(null)

  return (
    <form
      className="space-y-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit({ kind, title, note, file, text })
      }}
    >
      <h3 className="text-sm font-semibold text-slate-900">Новый эталон</h3>

      <div className="grid gap-3 sm:grid-cols-2">
        <label className="space-y-1">
          <span className={LABEL}>Вид</span>
          <select
            className={FIELD}
            value={kind}
            onChange={(event) => { setKind(event.target.value as ReferenceKind); }}
          >
            {(Object.keys(REFERENCE_KIND_LABELS) as ReferenceKind[]).map((option) => (
              <option key={option} value={option}>
                {REFERENCE_KIND_LABELS[option]}
              </option>
            ))}
          </select>
        </label>

        <label className="space-y-1">
          <span className={LABEL}>Название</span>
          <input
            className={FIELD}
            value={title}
            onChange={(event) => { setTitle(event.target.value); }}
            placeholder="Письмо, на которое ответили"
            required
          />
        </label>
      </div>

      <label className="space-y-1">
        <span className={LABEL}>Чем именно хорош</span>
        <input
          className={FIELD}
          value={note}
          onChange={(event) => { setNote(event.target.value); }}
          placeholder="Короткое, без канцелярита, сразу по требованиям"
        />
      </label>

      <label className="space-y-1">
        <span className={LABEL}>Файл (PDF, DOCX, TXT, MD)</span>
        <input
          className={FIELD}
          type="file"
          accept=".pdf,.docx,.txt,.md,.markdown"
          onChange={(event) => { setFile(event.target.files?.[0] ?? null); }}
        />
      </label>

      {file === null && (
        <label className="space-y-1">
          <span className={LABEL}>…или вставьте текст</span>
          <textarea
            className={`${FIELD} h-40 font-mono`}
            value={text}
            onChange={(event) => { setText(event.target.value); }}
            placeholder="Текст эталонного документа"
          />
        </label>
      )}

      <p className="text-xs text-slate-500">
        Из эталона берут форму: структуру, длину, порядок и тон. Содержание письма и резюме
        берут только из вашего профиля — факты из чужого документа не переносятся никогда.
      </p>

      {warnings.length > 0 && (
        <ul className="space-y-1 rounded-md border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          {warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}

      {error != null && (
        <p className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-800">
          {error instanceof Error ? error.message : 'Не удалось сохранить эталон'}
        </p>
      )}

      <button
        type="submit"
        disabled={busy}
        className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50"
      >
        {busy ? 'Загружаем…' : 'Добавить эталон'}
      </button>
    </form>
  )
}

interface ListProps {
  references: Reference[]
  busyId: string | null
  onToggle: (reference: Reference) => void
  onDelete: (reference: Reference) => void
}

export function ReferenceList({ references, busyId, onToggle, onDelete }: ListProps) {
  if (references.length === 0) {
    return <p className="text-sm text-slate-500">Эталонов пока нет.</p>
  }

  return (
    <ul className="space-y-2">
      {references.map((reference) => (
        <li
          key={reference.id}
          className={`rounded-lg border p-3 ${
            reference.is_active ? 'border-slate-200 bg-white' : 'border-slate-200 bg-slate-50'
          }`}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 space-y-1">
              <p className="text-sm font-medium text-slate-900">
                {reference.title}{' '}
                <span className="text-xs font-normal text-slate-500">
                  · {REFERENCE_KIND_LABELS[reference.kind]} · {reference.characters} знаков
                  {reference.source_filename ? ` · ${reference.source_filename}` : ' · вставлен текстом'}
                </span>
              </p>
              {reference.note && <p className="text-sm text-slate-600">{reference.note}</p>}
              <p className="truncate text-xs text-slate-400">{reference.preview}</p>
              {!reference.is_active && (
                <span className="rounded bg-slate-100 px-1.5 py-0.5 text-xs text-slate-500">
                  выключен
                </span>
              )}
            </div>

            <div className="flex shrink-0 gap-2">
              <button
                type="button"
                disabled={busyId === reference.id}
                onClick={() => { onToggle(reference); }}
                className="rounded-md border border-slate-300 px-2.5 py-1 text-xs text-slate-700 disabled:opacity-50"
              >
                {reference.is_active ? 'Выключить' : 'Включить'}
              </button>
              <button
                type="button"
                disabled={busyId === reference.id}
                onClick={() => { onDelete(reference); }}
                className="rounded-md border border-red-200 px-2.5 py-1 text-xs text-red-700 disabled:opacity-50"
              >
                Удалить
              </button>
            </div>
          </div>
        </li>
      ))}
    </ul>
  )
}
