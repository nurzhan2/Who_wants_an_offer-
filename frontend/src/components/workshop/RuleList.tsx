/**
 * The rules, built-in first.
 *
 * A built-in is shown in the same list as the owner's own, marked, with its
 * controls absent rather than disabled — there is no address to delete it at,
 * so offering a greyed-out button would be promising something the API cannot
 * do. A person is entitled to know which constraints they cannot lift and why,
 * which is what the built-in's own message says.
 *
 * Each row carries two sentences: the owner's, and the one the model is asked
 * for. They are side by side because that comparison is where a rule that does
 * not measure what its author meant becomes visible.
 */
import { Button } from '@/components/ui'
import { describeRule, SCOPE_LABELS, SEVERITY_LABELS } from '@/components/workshop/labels'
import type { Rule } from '@/types/workshop'

interface Props {
  rules: Rule[]
  busyId: string | null
  /** What is being done to the row `busyId` names. */
  busyAction: 'toggle' | 'delete' | null
  onToggle: (rule: Rule) => void
  onDelete: (rule: Rule) => void
}

function Badge({ children, tone }: { children: string; tone: 'hard' | 'soft' | 'muted' }) {
  const tones = {
    hard: 'bg-amber-100 text-amber-900',
    soft: 'bg-slate-100 text-muted',
    muted: 'bg-slate-100 text-muted',
  }
  return <span className={`rounded px-1.5 py-0.5 text-xs ${tones[tone]}`}>{children}</span>
}

export function RuleList({ rules, busyId, busyAction, onToggle, onDelete }: Props) {
  if (rules.length === 0) {
    return <p className="text-sm text-muted">Правил пока нет.</p>
  }

  return (
    <ul className="space-y-2">
      {rules.map((rule) => (
        <li
          key={rule.id}
          className={`rounded-lg border p-3 ${
            rule.is_active ? 'border-slate-200 bg-white' : 'border-slate-200 bg-slate-50'
          }`}
        >
          <div className="flex flex-wrap items-start justify-between gap-2">
            <div className="min-w-0 space-y-1">
              <p className="text-sm font-medium text-ink">{rule.message}</p>
              <p className="text-sm text-muted">{describeRule(rule.params)}</p>
              <p className="text-xs text-muted">
                Модель просят так: <span className="font-mono">{rule.asked_as}</span>
              </p>
              <div className="flex flex-wrap items-center gap-1.5 pt-1">
                <Badge tone={rule.severity === 'hard' ? 'hard' : 'soft'}>
                  {SEVERITY_LABELS[rule.severity]}
                </Badge>
                <Badge tone="muted">{SCOPE_LABELS[rule.scope]}</Badge>
                {rule.is_builtin && <Badge tone="muted">встроенное, не отключается</Badge>}
                {!rule.is_active && <Badge tone="muted">выключено</Badge>}
              </div>
            </div>

            {!rule.is_builtin && (
              <div className="flex shrink-0 gap-2">
                <Button
                  outline
                  disabled={busyId === rule.id}
                  busy={
                    busyId === rule.id && busyAction === 'toggle'
                      ? rule.is_active
                        ? 'Выключаем…'
                        : 'Включаем…'
                      : false
                  }
                  onClick={() => { onToggle(rule); }}
                >
                  {rule.is_active ? 'Выключить' : 'Включить'}
                </Button>
                <Button
                  outline
                  disabled={busyId === rule.id}
                  busy={busyId === rule.id && busyAction === 'delete' ? 'Удаляем…' : false}
                  onClick={() => { onDelete(rule); }}
                >
                  Удалить
                </Button>
              </div>
            )}
          </div>
        </li>
      ))}
    </ul>
  )
}
