import { useState } from 'react'

import { fileUrl } from '@/api/documents'
import { AtsReport } from '@/components/AtsReport'
import { EmployerSignals } from '@/components/EmployerSignals'
import { useGenerateDocument } from '@/hooks/useDocuments'
import type { DocumentCandidate, DocumentKind, GeneratedDocument } from '@/types/documents'

/**
 * One vacancy's card: the two buttons, what they produced, and the audit.
 *
 * This is the component `prompts/11-dashboard.md` expects to drop into each row
 * of its vacancy table, so it takes a candidate and owns nothing above itself.
 *
 * Three behaviours are load-bearing rather than cosmetic.
 *
 * **A withheld document is not rendered as a document.** The response carries
 * `delivered`, and everything below branches on it first: a refusal shows the
 * reason and the hard rules, and offers no download, because there is no file.
 *
 * **The report is always beside the text.** Never behind a tab, never on
 * another screen. A CV whose audit nobody read is a CV nobody checked.
 *
 * **There is no send button, here or anywhere in the dashboard.** An
 * application is sent from `agent/`, in a browser, under the owner's own
 * account, after a human confirms that particular one. A browser cannot give
 * the guarantee that confirmation exists for, so this screen generates
 * documents and stops.
 */
export function VacancyDocuments({ candidate }: { candidate: DocumentCandidate }) {
  const cv = useGenerateDocument('cv')
  const letter = useGenerateDocument('cover_letter')
  const [shown, setShown] = useState<DocumentKind | null>(null)

  const result =
    shown === 'cv' ? (cv.data ?? null) : shown === 'cover_letter' ? (letter.data ?? null) : null
  const failure = shown === 'cv' ? cv.error : shown === 'cover_letter' ? letter.error : null

  return (
    <article className="border-b border-obsidian p-card">
      <header>
        <h3 className="text-body text-obsidian">{candidate.title}</h3>
        <p className="text-body-sm text-felt-gray">
          {candidate.company ?? 'компания не указана'} · score {formatScore(candidate.score)}
        </p>
        <Route candidate={candidate} />
        <EmployerSignals signals={candidate.employer} />
      </header>

      <div className="mt-element flex flex-wrap gap-element">
        <GenerateButton
          label="CV под эту вакансию"
          versions={candidate.cv_versions}
          pending={cv.isPending}
          onClick={() => {
            setShown('cv')
            cv.mutate(candidate.vacancy_id)
          }}
        />
        <GenerateButton
          label="Сопроводительное"
          versions={candidate.letter_versions}
          pending={letter.isPending}
          onClick={() => {
            setShown('cover_letter')
            letter.mutate(candidate.vacancy_id)
          }}
        />
      </div>

      {failure && (
        <p className="mt-element text-body-sm text-obsidian">
          Не удалось: {failure.message}
        </p>
      )}
      {result && <Result document={result} />}
    </article>
  )
}

/**
 * How this vacancy is applied to, said before the buttons.
 *
 * A vacancy on the agent's source is sent by `wwao apply`, and its letter is what
 * lets it into that queue. Any other is applied to by the owner on the original
 * page, so the link is the point of the line: the documents below are what they
 * take with them.
 */
function Route({ candidate }: { candidate: DocumentCandidate }) {
  if (candidate.via_agent) {
    return (
      <p className="text-caption text-felt-gray">
        Автоотклик через агента · {candidate.source_slug}
      </p>
    )
  }
  return (
    <p className="text-caption text-felt-gray">
      Откликнуться самому · {candidate.source_slug || 'источник не указан'}
      {candidate.url && (
        <>
          {' · '}
          <a
            href={candidate.url}
            target="_blank"
            rel="noreferrer"
            className="text-obsidian underline underline-offset-2"
          >
            открыть оригинал
          </a>
        </>
      )}
    </p>
  )
}

/**
 * A button that says what it will do and what happened last time.
 *
 * The version count is on the button because it changes what pressing it means:
 * at zero it generates, and afterwards it generates *another version* and
 * replaces nothing. A person who has pressed it twice should not have to guess
 * whether the first document still exists.
 *
 * Pill radius and the single permitted fill, per the design system; disabled
 * while the request is in flight, which is the whole of the asynchronous state
 * a person needs — the work is a request this page is still holding.
 */
function GenerateButton({
  label,
  versions,
  pending,
  onClick,
}: {
  label: string
  versions: number
  pending: boolean
  onClick: () => void
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={pending}
      className="rounded-pill bg-slate-pill px-6 py-3 text-body-sm font-normal text-paper transition-colors duration-slow ease-monopo hover:bg-obsidian disabled:bg-ash-mist"
    >
      {pending ? 'Генерируем…' : label}
      {versions > 0 && !pending && (
        <span className="ml-2 text-caption text-paper/70">версий: {versions}</span>
      )}
    </button>
  )
}

function Result({ document }: { document: GeneratedDocument }) {
  if (!document.delivered) {
    return <Withheld document={document} />
  }

  return (
    <div className="mt-section">
      <div className="flex flex-wrap items-baseline gap-element">
        <span className="text-caption uppercase tracking-wide text-felt-gray">
          Версия {document.version} · {document.source === 'model' ? 'модель' : 'по правилам'}
        </span>
        {document.document_id && (
          <a
            href={fileUrl(document.document_id)}
            className="rounded-pill border border-obsidian px-5 py-2 text-body-sm text-obsidian transition-colors duration-slow ease-monopo hover:bg-obsidian hover:text-paper"
          >
            Скачать {document.file_format?.toUpperCase()}
          </a>
        )}
      </div>

      {document.text && (
        <pre className="mt-element max-h-96 overflow-auto whitespace-pre-wrap border border-ash-mist p-element text-body-sm font-normal text-inkstone">
          {document.text}
        </pre>
      )}

      {document.review && (
        <div className="mt-element">
          <AtsReport review={document.review} />
        </div>
      )}
    </div>
  )
}

/**
 * What is shown when the system refused to hand a document over.
 *
 * The reason first, in the owner's language, then the rules the document had to
 * satisfy — so the refusal is actionable rather than merely firm. The audit is
 * included when it was the audit that withheld the document, because the
 * findings are the specific answer to "what is wrong with it".
 */
function Withheld({ document }: { document: GeneratedDocument }) {
  return (
    <div className="mt-section border border-obsidian p-element">
      <p className="text-caption uppercase tracking-wide text-felt-gray">Документ не выдан</p>
      <p className="text-body-sm font-semibold text-obsidian">{document.reason_ru}</p>

      {document.problems.length > 0 && (
        <p className="mt-element text-caption text-felt-gray">
          Проверки: {document.problems.join(', ')}
        </p>
      )}

      {document.hard_rules.length > 0 && (
        <ul className="mt-element space-y-1">
          {document.hard_rules.map((rule) => (
            <li key={rule} className="text-caption text-inkstone">
              — {rule}
            </li>
          ))}
        </ul>
      )}

      {document.review && (
        <div className="mt-element">
          <AtsReport review={document.review} />
        </div>
      )}
    </div>
  )
}

/** The score as a whole number: the API sends a Decimal, so it arrives as text. */
function formatScore(score: string): string {
  const value = Number(score)
  return Number.isNaN(value) ? score : String(Math.round(value))
}
