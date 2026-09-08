import { ApiError } from '@/api/client'
import { Card, Empty, Failure, Field, Loading, Pill, Section } from '@/components/ui'
import { useActiveProfile } from '@/hooks/queries'
import { count, date } from '@/lib/format'
import { PARSE_STATUS, REMOTE, SKILL_LEVEL } from '@/lib/labels'
import type { ProfileSkill } from '@/types/api'

/**
 * Мои данные: the resume as the matcher reads it.
 *
 * Everything a score is computed from, in the form it is stored in — because
 * every extraction error becomes a wrong score, and the only way to notice one
 * is to see what was extracted. A skill listed at the wrong level, a city that
 * is not where the person lives, a year of experience the parser invented: they
 * are all invisible until this page shows them.
 *
 * Contact details are shown here and nowhere else, and nothing on this page is
 * sent anywhere: there is no analytics in this frontend and no logging of what
 * it renders.
 */
export function MyData() {
  const { data, isPending, isError, error } = useActiveProfile()

  if (isPending) return <Loading what="резюме" />
  if (isError) {
    const missing = error instanceof ApiError && error.status === 404
    return missing ? (
      <Section title="Мои данные">
        <Empty>
          Активного резюме нет. Загрузите файл через <code>POST /api/v1/resume/upload</code> —
          дашборд читает то, что разобрал бэкенд, и сам ничего не загружает.
        </Empty>
      </Section>
    ) : (
      <Failure error={error} what="резюме" />
    )
  }

  const corroborated = data.skills.filter((skill) => skill.evidence === 'corroborated').length

  return (
    <div className="rise">
      <Section title="Резюме" note="То, из чего считается каждая оценка на других экранах.">
        <div className="grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          <Field label="имя">{data.name ?? '—'}</Field>
          <Field label="должность">{data.headline ?? '—'}</Field>
          <Field label="грейд">{data.seniority ?? '—'}</Field>
          <Field label="опыт, лет">
            <span className="tnum">{data.total_years ?? '—'}</span>
          </Field>
          <Field label="города">{data.locations.join(', ') || '—'}</Field>
          <Field label="релокация">{data.relocation ? 'готов' : 'нет'}</Field>
          <Field label="формат">{data.remote_pref ? REMOTE[data.remote_pref] : '—'}</Field>
          <Field label="зарплатные ожидания">
            {data.salary_min ? `от ${data.salary_min} ${data.salary_currency ?? ''}` : '—'}
          </Field>
          <Field label="языки">
            {data.languages
              .map((item) => `${item.code ?? ''} ${item.level ?? ''}`.trim())
              .filter(Boolean)
              .join(', ') || '—'}
          </Field>
          <Field label="файл">{data.resume_filename ?? '—'}</Field>
          <Field label="разбор">{PARSE_STATUS[data.parse_status]}</Field>
          <Field label="обновлено">{date(data.updated_at)}</Field>
        </div>
        {data.summary ? <p className="mt-8 max-w-3xl">{data.summary}</p> : null}
        {data.parse_error ? (
          <p className="mt-4 max-w-3xl text-small">Ошибка разбора: {data.parse_error}</p>
        ) : null}
      </Section>

      <Section
        title="Навыки"
        note={`${count(data.skills.length)} штук, из них ${count(corroborated)} подтверждены датированной работой. «Названо» — тоже навык, просто ничто его не датирует.`}
      >
        {data.skills.length === 0 ? (
          <Empty>Навыков не извлечено — оценка вакансий будет держаться на одной семантике.</Empty>
        ) : (
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {data.skills.map((skill) => (
              <Skill key={skill.id} skill={skill} />
            ))}
          </div>
        )}
      </Section>
    </div>
  )
}

function Skill({ skill }: { skill: ProfileSkill }) {
  const spelling = skill.raw_names.find((name) => name.trim()) ?? skill.canonical_name
  return (
    <Card className="p-6">
      <div className="flex items-baseline justify-between gap-3">
        <span className="font-semibold">{spelling}</span>
        {skill.evidence === 'corroborated' ? <Pill strong>подтверждён</Pill> : <Pill>названо</Pill>}
      </div>
      <div className="mt-3 text-small text-muted">
        {SKILL_LEVEL[skill.level] ?? skill.level}
        {skill.years ? ` · ${skill.years} лет` : ''}
        {skill.last_used_year ? ` · последний раз в ${String(skill.last_used_year)}` : ''}
      </div>
      {spelling.toLowerCase() !== skill.canonical_name ? (
        <div className="mt-1 text-small text-muted">ключ: {skill.canonical_name}</div>
      ) : null}
    </Card>
  )
}
