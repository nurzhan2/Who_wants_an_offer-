/**
 * Мастерская: эталоны, правила, предпросмотр.
 *
 * The screen is arranged in the order the two levers are used. References come
 * first because they are the softer one — form by example, nothing enforced.
 * Rules come second because a hard one changes what may be saved. The preview
 * comes last because it is how a person finds out what the two of them did.
 *
 * The page holds no state the server owns: everything is a query, and every
 * mutation invalidates the list it changed. A rule that was refused must never
 * appear in the list because an optimistic update put it there.
 */
import { useState } from 'react'

import { PreviewPanel } from '@/components/workshop/PreviewPanel'
import { ReferenceForm, ReferenceList } from '@/components/workshop/References'
import { RuleForm } from '@/components/workshop/RuleForm'
import { RuleList } from '@/components/workshop/RuleList'
import {
  useCreateReference,
  useCreateRule,
  useDeleteReference,
  useDeleteRule,
  usePreview,
  useReferences,
  useRules,
  useUpdateReference,
  useUpdateRule,
  useVacancyChoices,
} from '@/hooks/useWorkshop'

function Section({ title, hint, children }: { title: string; hint: string; children: React.ReactNode }) {
  return (
    <section className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold text-ink">{title}</h2>
        <p className="mt-1 text-sm text-muted">{hint}</p>
      </div>
      {children}
    </section>
  )
}

export function Workshop() {
  const [query, setQuery] = useState('')

  const rules = useRules()
  const references = useReferences()
  const vacancies = useVacancyChoices(query)

  const createRule = useCreateRule()
  const updateRule = useUpdateRule()
  const deleteRule = useDeleteRule()
  const createReference = useCreateReference()
  const updateReference = useUpdateReference()
  const deleteReference = useDeleteReference()
  const preview = usePreview()

  const ruleBusyId = updateRule.isPending
    ? updateRule.variables.id
    : deleteRule.isPending
      ? deleteRule.variables
      : null
  const referenceBusyId = updateReference.isPending
    ? updateReference.variables.id
    : deleteReference.isPending
      ? deleteReference.variables
      : null

  return (
    <div className="space-y-10">
      <Section
        title="Эталоны"
        hint="Документы, на которые ориентироваться. Из них берут форму — структуру, длину, порядок и тон. Содержание всегда берут только из вашего профиля."
      >
        <ReferenceForm
          busy={createReference.isPending}
          error={createReference.error}
          warnings={createReference.data?.warnings ?? []}
          onSubmit={(input) => { createReference.mutate(input); }}
        />
        {references.isPending && <p className="text-sm text-muted">Загружаем эталоны…</p>}
        {references.isError && (
          <p className="text-sm text-ink">Не удалось загрузить эталоны.</p>
        )}
        {references.data && (
          <ReferenceList
            references={references.data}
            busyId={referenceBusyId}
            onToggle={(reference) => {
              updateReference.mutate({
                id: reference.id,
                changes: { is_active: !reference.is_active },
              })
            }}
            onDelete={(reference) => { deleteReference.mutate(reference.id); }}
          />
        )}
      </Section>

      <Section
        title="Правила"
        hint="Проверяемые требования. Жёсткие проверяются кодом после генерации: пока правило не выполнено, текст не отдаётся. Правило описывает форму — потребовать написать неправду им нельзя."
      >
        <RuleForm
          busy={createRule.isPending}
          error={createRule.error}
          onSubmit={(payload) => { createRule.mutate(payload); }}
        />
        {rules.isPending && <p className="text-sm text-muted">Загружаем правила…</p>}
        {rules.isError && <p className="text-sm text-ink">Не удалось загрузить правила.</p>}
        {rules.data && (
          <RuleList
            rules={rules.data}
            busyId={ruleBusyId}
            onToggle={(rule) => {
              updateRule.mutate({ id: rule.id, changes: { is_active: !rule.is_active } })
            }}
            onDelete={(rule) => { deleteRule.mutate(rule.id); }}
          />
        )}
      </Section>

      <PreviewPanel
        vacancies={vacancies.data ?? []}
        query={query}
        onQuery={setQuery}
        busy={preview.isPending}
        error={preview.error}
        preview={preview.data}
        onRun={(vacancyId) => { preview.mutate(vacancyId); }}
      />
    </div>
  )
}
