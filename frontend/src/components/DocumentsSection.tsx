import { useDocumentCandidates } from '@/hooks/useDocuments'
import { VacancyDocuments } from '@/components/VacancyDocuments'

/**
 * The vacancies the two buttons live on, best-scoring first.
 *
 * A holding place, deliberately. `prompts/11-dashboard.md` builds the Vacancies
 * screen — filters, the score breakdown, the table — and puts these same two
 * buttons on each of its rows; what this section does is give them somewhere to
 * work today, against live data, without pre-empting that screen's layout. The
 * component it renders per vacancy takes a candidate and owns nothing above
 * itself, so re-homing it is an import and not a rewrite.
 */
export function DocumentsSection() {
  const { data, isPending, isError, error } = useDocumentCandidates()

  if (isPending) {
    return <p className="text-body-sm text-felt-gray">Загружаем вакансии…</p>
  }

  if (isError) {
    return (
      <p className="text-body-sm text-obsidian">
        Не удалось загрузить список: {error.message}
      </p>
    )
  }

  if (data.length === 0) {
    return (
      <p className="text-body-sm text-felt-gray">
        Пока нечего показать: загрузи резюме и запусти скоринг, и вакансии появятся здесь.
      </p>
    )
  }

  return (
    <div className="border-t border-obsidian">
      {data.map((candidate) => (
        <VacancyDocuments key={candidate.vacancy_id} candidate={candidate} />
      ))}
    </div>
  )
}
