/**
 * The form for one rule.
 *
 * The kind picker drives everything below it, because a rule is a discriminated
 * union and there is no set of fields that fits all eight kinds. Showing every
 * field at once and ignoring the irrelevant ones would let somebody fill in a
 * minimum for a date-format rule and believe it did something.
 *
 * The two built-in kinds are not offered: they exist already, cannot be
 * duplicated usefully, and cannot be removed.
 *
 * A refusal is rendered where it happened. When the API answers 422 because the
 * rule would require claiming experience the profile does not list, the names
 * that caused it are shown next to the field that named them — a refusal
 * without the names is a puzzle rather than a correction.
 */
import { useState } from 'react'

import { ApiError } from '@/api/client'
import { Button } from '@/components/ui'
import { DATE_FORMAT_LABELS, KIND_LABELS, SCOPE_LABELS, SEVERITY_HINTS, SEVERITY_LABELS } from '@/components/workshop/labels'
import type {
  DateFormatPattern,
  LengthUnit,
  Problem,
  RuleCreate,
  RuleKind,
  RuleParams,
  RuleScope,
  RuleSeverity,
} from '@/types/workshop'

/** The kinds a person may create. The two built-ins are not among them. */
const CREATABLE: RuleKind[] = [
  'section_item_count',
  'required_section',
  'required_keyword',
  'forbidden_phrase',
  'length',
  'date_format',
]

const FIELD =
  'w-full rounded-md border border-slate-300 px-3 py-2 text-sm text-ink focus:border-slate-500 focus:outline-none'
const LABEL = 'block text-xs font-medium uppercase tracking-wide text-muted'

interface Props {
  busy: boolean
  error: unknown
  onSubmit: (payload: RuleCreate) => void
}

/** The `claims` of a refusal, when the error is one. */
function claimsOf(error: unknown): string[] {
  if (error instanceof ApiError && error.problem && typeof error.problem === 'object') {
    const claims = (error.problem as Problem).claims
    if (Array.isArray(claims)) {
      return claims
    }
  }
  return []
}

export function RuleForm({ busy, error, onSubmit }: Props) {
  const [kind, setKind] = useState<RuleKind>('section_item_count')
  const [scope, setScope] = useState<RuleScope>('cover_letter')
  const [severity, setSeverity] = useState<RuleSeverity>('hard')
  const [message, setMessage] = useState('')
  const [section, setSection] = useState('навыки')
  const [keyword, setKeyword] = useState('')
  const [pattern, setPattern] = useState<DateFormatPattern>('mm.yyyy')
  const [unit, setUnit] = useState<LengthUnit>('characters')
  const [minimum, setMinimum] = useState('')
  const [maximum, setMaximum] = useState('')

  const claims = claimsOf(error)
  const number = (value: string): number | null => (value.trim() === '' ? null : Number(value))

  function params(): RuleParams {
    switch (kind) {
      case 'section_item_count':
        return { kind, section, minimum: number(minimum), maximum: number(maximum) }
      case 'required_section':
        return { kind, section }
      case 'required_keyword':
        return { kind, keyword }
      case 'forbidden_phrase':
        return { kind, phrase: keyword }
      case 'date_format':
        return { kind, pattern }
      default:
        return { kind: 'length', unit, minimum: number(minimum), maximum: number(maximum) }
    }
  }

  const usesSection = kind === 'section_item_count' || kind === 'required_section'
  const usesWord = kind === 'required_keyword' || kind === 'forbidden_phrase'
  const usesBounds = kind === 'section_item_count' || kind === 'length'

  return (
    <form
      className="space-y-4 rounded-lg border border-slate-200 bg-white p-4"
      onSubmit={(event) => {
        event.preventDefault()
        onSubmit({ scope, severity, params: params(), message })
      }}
    >
      <h3 className="text-sm font-semibold text-ink">Новое правило</h3>

      <div className="grid gap-3 sm:grid-cols-3">
        <label className="space-y-1">
          <span className={LABEL}>Что проверяем</span>
          <select
            className={FIELD}
            value={kind}
            onChange={(event) => { setKind(event.target.value as RuleKind); }}
          >
            {CREATABLE.map((option) => (
              <option key={option} value={option}>
                {KIND_LABELS[option]}
              </option>
            ))}
          </select>
        </label>

        <label className="space-y-1">
          <span className={LABEL}>Где</span>
          <select
            className={FIELD}
            value={scope}
            onChange={(event) => { setScope(event.target.value as RuleScope); }}
          >
            {(Object.keys(SCOPE_LABELS) as RuleScope[]).map((option) => (
              <option key={option} value={option}>
                {SCOPE_LABELS[option]}
              </option>
            ))}
          </select>
        </label>

        <label className="space-y-1">
          <span className={LABEL}>Строгость</span>
          <select
            className={FIELD}
            value={severity}
            onChange={(event) => { setSeverity(event.target.value as RuleSeverity); }}
          >
            {(Object.keys(SEVERITY_LABELS) as RuleSeverity[]).map((option) => (
              <option key={option} value={option}>
                {SEVERITY_LABELS[option]}
              </option>
            ))}
          </select>
        </label>
      </div>

      <p className="text-xs text-muted">{SEVERITY_HINTS[severity]}</p>

      <div className="grid gap-3 sm:grid-cols-2">
        {usesSection && (
          <label className="space-y-1">
            <span className={LABEL}>Раздел</span>
            <input
              className={FIELD}
              value={section}
              onChange={(event) => { setSection(event.target.value); }}
              placeholder="навыки"
              required
            />
          </label>
        )}

        {usesWord && (
          <label className="space-y-1">
            <span className={LABEL}>Слово или оборот</span>
            <input
              className={FIELD}
              value={keyword}
              onChange={(event) => { setKeyword(event.target.value); }}
              required
            />
          </label>
        )}

        {kind === 'date_format' && (
          <label className="space-y-1">
            <span className={LABEL}>Как пишем даты</span>
            <select
              className={FIELD}
              value={pattern}
              onChange={(event) => { setPattern(event.target.value as DateFormatPattern); }}
            >
              {(Object.keys(DATE_FORMAT_LABELS) as DateFormatPattern[]).map((option) => (
                <option key={option} value={option}>
                  {DATE_FORMAT_LABELS[option]}
                </option>
              ))}
            </select>
          </label>
        )}

        {kind === 'length' && (
          <label className="space-y-1">
            <span className={LABEL}>Считаем</span>
            <select
              className={FIELD}
              value={unit}
              onChange={(event) => { setUnit(event.target.value as LengthUnit); }}
            >
              <option value="characters">знаки</option>
              <option value="words">слова</option>
            </select>
          </label>
        )}

        {usesBounds && (
          <>
            <label className="space-y-1">
              <span className={LABEL}>Не меньше</span>
              <input
                className={FIELD}
                type="number"
                min={0}
                value={minimum}
                onChange={(event) => { setMinimum(event.target.value); }}
                placeholder="21"
              />
            </label>
            <label className="space-y-1">
              <span className={LABEL}>Не больше</span>
              <input
                className={FIELD}
                type="number"
                min={0}
                value={maximum}
                onChange={(event) => { setMaximum(event.target.value); }}
              />
            </label>
          </>
        )}
      </div>

      <label className="space-y-1">
        <span className={LABEL}>Что показать человеку, если правило нарушено</span>
        <input
          className={FIELD}
          value={message}
          onChange={(event) => { setMessage(event.target.value); }}
          placeholder="В навыках не меньше 21 пункта"
          required
        />
        <span className="block text-xs text-muted">
          Этот текст видите только вы. Модели его не показывают — она получает описание,
          собранное из полей выше.
        </span>
      </label>

      {error != null && (
        <div className="rounded-md border border-red-200 bg-red-50 p-3 text-sm text-ink">
          <p>{error instanceof Error ? error.message : 'Не удалось сохранить правило'}</p>
          {claims.length > 0 && (
            <p className="mt-2 text-xs">
              Правило описывает форму, а не факты о кандидате. В нём названы навыки, которых нет
              в профиле: <span className="font-medium">{claims.join(', ')}</span>.
            </p>
          )}
        </div>
      )}

      <div>
        <Button type="submit" busy={busy ? 'Сохраняем правило…' : false}>
          Добавить правило
        </Button>
      </div>
    </form>
  )
}
