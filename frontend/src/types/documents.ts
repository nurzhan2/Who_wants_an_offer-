/**
 * The document endpoints' contracts, mirroring `app/schemas/document.py`.
 *
 * Two shapes here are deliberately awkward to use carelessly, because both
 * carry a distinction the backend refuses to collapse and a screen must not
 * collapse either.
 *
 * `GeneratedDocument.delivered` is the flag everything branches on. A withheld
 * document has no `document_id`, no `text` and no file; it has a `reason_ru`
 * and, when the audit is what withheld it, the report that did.
 *
 * `RequirementCoverage` keeps three lists apart. `held_but_unnamed` is fixable
 * by regenerating — the candidate has the skill and this version did not name
 * it — and `not_held` is not fixable at all. Rendering them the same way would
 * be inviting the owner to write down a skill they do not have.
 */

export type DocumentKind = 'cv' | 'cover_letter'
export type DocumentSource = 'model' | 'fallback'
export type AtsOverall = 'ok' | 'degraded' | 'unreadable'
export type AtsSeverity = 'critical' | 'warning' | 'info'

export interface AtsFinding {
  code: string
  severity: AtsSeverity
  title: string
  explanation: string
  example_fragment: string | null
  fix: string
  penalty: number
}

export interface AtsReport {
  score: number
  overall: AtsOverall
  findings: AtsFinding[]
  checks_run: string[]
  sections_detected: string[]
  source_format: string
  page_count: number
  word_count: number
}

export interface RequirementCoverage {
  /** Required, held, and named in this document the vacancy's own way. */
  named: string[]
  /** Required and held, but not named in this version. Fixable. */
  held_but_unnamed: string[]
  /** Required and not held. Not fixable, and nothing is suggested. */
  not_held: string[]
  /**
   * Requirements the employer never stated: read out of the description text.
   * Cuts across the three lists above rather than being a fourth one.
   */
  inferred: string[]
  literal_coverage: number
}

export interface DocumentReview {
  ats: AtsReport
  coverage: RequirementCoverage
}

export interface GeneratedDocument {
  vacancy_id: string
  kind: DocumentKind
  delivered: boolean
  document_id: string | null
  version: number | null
  source: DocumentSource | null
  filename: string | null
  file_format: string | null
  text: string | null
  review: DocumentReview | null
  reason: string | null
  reason_ru: string | null
  problems: string[]
  hard_rules: string[]
}

export interface DocumentVersion {
  id: string
  vacancy_id: string
  kind: DocumentKind
  version: number
  rules_version: string
  source: DocumentSource
  ats_score: number
  ats_overall: AtsOverall
  created_at: string
}

export interface DocumentSummary extends DocumentVersion {
  vacancy_title: string
  company: string | null
}

/**
 * What the employer published about their own posting.
 *
 * Nothing here is looked up: no employee of a company is searched for, on this
 * screen or anywhere else in this project. `responses_count` is `null` when the
 * page did not state one, which is not zero, and the component that renders it
 * has to keep those apart.
 */
export interface EmployerSignals {
  last_activity: string | null
  accredited_it_employer: boolean
  on_additional_check: boolean
  responses_count: number | null
}

export interface DocumentCandidate {
  vacancy_id: string
  title: string
  company: string | null
  score: string
  cv_versions: number
  letter_versions: number
  employer: EmployerSignals
  /** True when the agent applies to it; false means «откликнуться самому» on `url`. */
  via_agent: boolean
  source_slug: string
  url: string
}
