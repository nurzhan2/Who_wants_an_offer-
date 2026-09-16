/**
 * The shapes the API answers with.
 *
 * Hand-written rather than generated, and narrow on purpose: every field here
 * is one a screen actually renders. A generated mirror of the OpenAPI document
 * would carry the whole vacancy record into a file nobody reads, and the first
 * time the backend added a column the diff would be noise.
 *
 * `null` means *not measured* everywhere it appears, exactly as it does in
 * `app/schemas/dashboard.py`: no run has counted that file, nothing has scored
 * that vacancy, hh has not answered that application. Rendering one as `0` is
 * the single easiest way for this interface to lie, so the types keep the
 * absence and the components have to decide what to say about it.
 */

export type ParseStatus = 'pending' | 'ready' | 'failed'
export type RunStatus = 'running' | 'success' | 'partial' | 'failed'
export type Bucket = 'apply_now' | 'strong' | 'stretch' | 'skip' | 'filtered'
export type Remote = 'no' | 'hybrid' | 'full'
export type Seniority = 'junior' | 'middle' | 'senior' | 'lead'
export type ApplicationStatus =
  | 'saved'
  | 'applied'
  | 'screening'
  | 'interview'
  | 'offer'
  | 'rejected'

// ── overview ─────────────────────────────────────────────────────────

export interface VacancyCounts {
  total: number
  active: number
  embedded: number
  needs_embedding: number
  scored: number
  skill_rows: number
}

export interface CrawlPosition {
  scope: string
  label: string
  title: string | null
  total: number | null
  outstanding: number | null
  stretches: number
  newest: string | null
  oldest: string | null
  updated_at: string | null
}

export interface SourceCrawl {
  slug: string
  positions: CrawlPosition[]
}

export interface RunError {
  stage: string | null
  error: string | null
  detail: string | null
}

export interface SourceRunState {
  slug: string
  run_id: string
  status: RunStatus
  started_at: string
  finished_at: string | null
  found: number
  new: number
  updated: number
  errors: RunError[]
  stopped_by_robot_check: boolean
}

export interface HarvestedVacancy {
  id: string
  title: string
  company: string | null
  url: string | null
  source_slug: string | null
  first_seen_at: string
  score: string | null
  bucket: Bucket | null
}

export interface Harvest {
  since: string | null
  total: number
  items: HarvestedVacancy[]
}

export interface ApplicationCounts {
  sent: number
  /** Of `sent`, the ones hh itself confirmed. */
  sent_confirmed: number
  queued: number
  needs_manual: number
  with_letter: number
  answered: number
}

export interface ProfileBrief {
  id: string
  name: string | null
  headline: string | null
  parse_status: ParseStatus
  skills: number
  updated_at: string
}

export interface Overview {
  generated_at: string
  profile: ProfileBrief | null
  vacancies: VacancyCounts
  crawl: SourceCrawl[]
  runs: SourceRunState[]
  harvest: Harvest
  applications: ApplicationCounts
}

// ── the list and the card ────────────────────────────────────────────

export interface VacancyListItem {
  id: string
  title: string
  company: string | null
  source_slugs: string[]
  city: string | null
  country: string | null
  remote: Remote
  salary_min: string | null
  salary_max: string | null
  currency: string | null
  salary_min_normalized: string | null
  score: string | null
  bucket: Bucket | null
  missing_required_count: number
  published_at: string | null
  is_applied: boolean
}

export interface Facets {
  sources: Record<string, number>
  buckets: Record<string, number>
  cities: Record<string, number>
}

export interface CursorPage<T> {
  items: T[]
  next_cursor: string | null
  total: number | null
  facets: Facets | null
}

export type RequirementSource = 'employer_field' | 'description_text'

export interface RequirementStanding {
  canonical_name: string
  is_required: boolean
  coverage: string | null
  spelling: string | null
  weight: string | null
  /** `other_profile` or `resume_text`; null means no evidence anywhere. */
  evidence: string | null
  evidence_detail: string | null
  /**
   * `employer_field` when the employer named the requirement in hh's own
   * field, `description_text` when it was read out of their description.
   */
  source: RequirementSource
}

export interface RequirementBreakdown {
  covered: RequirementStanding[]
  not_in_this_cv: RequirementStanding[]
  absent: RequirementStanding[]
}

export interface MatchComponentScores {
  skill_coverage_required: string
  skill_coverage_nice: string
  semantic_similarity: string
  experience_fit: string
  domain_fit: string
  logistics_fit: string
}

export interface MatchSummary {
  score: string
  rule_score: string
  semantic_score: string | null
  llm_score: string | null
  bucket: Bucket
  components: MatchComponentScores
  red_flags: string[]
  experience_gap_years: string | null
  verdict: string | null
  application_angle: string | null
  scored_at: string
}

export interface LetterBrief {
  application_id: string
  characters: number
  sent_at: string | null
  agent_status: string | null
  outcome: string | null
}

export interface VacancyRead {
  id: string
  title: string
  company: string | null
  company_url: string | null
  description_md: string | null
  description_raw: string | null
  seniority: Seniority | null
  min_years: string | null
  city: string | null
  country: string | null
  remote: Remote
  employment_type: string | null
  language: string | null
  published_at: string | null
  first_seen_at: string
  last_seen_at: string
  is_active: boolean
  salary_min: string | null
  salary_max: string | null
  currency: string | null
  period: string | null
  skills: { canonical_name: string; is_required: boolean; weight: string }[]
  /** Every posting this vacancy was deduplicated from, with the URL crawled. */
  sources: { id: string; source_slug: string; external_id: string; url: string }[]
}

export interface VacancyCard {
  vacancy: VacancyRead
  match: MatchSummary | null
  requirements: RequirementBreakdown
  letter: LetterBrief | null
}

// ── the board ────────────────────────────────────────────────────────

export interface BoardCard {
  id: string
  vacancy_id: string
  title: string
  company: string | null
  url: string | null
  status: ApplicationStatus
  agent_status: string | null
  agent_reason: string | null
  applied_at: string | null
  sent_at: string | null
  sent_letter: string | null
  cover_letter: string | null
  match_score: string | null
  match_bucket: Bucket | null
  vacancy_key_skills: string[] | null
  hh_warning: string | null
  hh_blocking_warning: string | null
  hh_negotiations_total: number | null
  hh_last_state: string | null
  hh_last_state_at: string | null
  /** hh itself confirmed this send: a count of at least one, or a state. */
  send_confirmed: boolean
  vacancy_published_at: string | null
  vacancy_last_seen_at: string | null
  vacancy_active: boolean | null
}

export interface BoardColumn {
  key: string
  cards: BoardCard[]
}

export interface Board {
  stages: BoardColumn[]
  outcomes: BoardColumn[]
  other: BoardColumn
}

// ── documents ────────────────────────────────────────────────────────

export interface ATSFinding {
  code: string
  severity: 'critical' | 'warning' | 'info'
  title: string
  explanation: string
  example_fragment: string | null
  fix: string
  penalty: number
}

export interface Recoverable {
  total: number
  recovered: number
  lost: string[]
}

export interface ATSReport {
  score: number
  overall: 'ok' | 'degraded' | 'unreadable'
  findings: ATSFinding[]
  sections_detected: string[]
  coverage: {
    work_periods: Recoverable
    dates: Recoverable
    skills: Recoverable
  } | null
  source_format: string
  page_count: number
  word_count: number
}

export interface ResumeDocument {
  profile_id: string
  filename: string | null
  source_format: string | null
  size_bytes: number | null
  is_active: boolean
  parse_status: ParseStatus
  parse_error: string | null
  uploaded_at: string
  ats: ATSReport | null
}

export interface LetterProblem {
  code: string
  message: string
}

export interface LetterDocument {
  application_id: string
  vacancy_id: string
  title: string
  company: string | null
  url: string | null
  text: string
  characters: number
  rules_version: number | null
  problems: LetterProblem[]
  written_at: string
  sent_at: string | null
  sent_letter: string | null
  outcome: string | null
  outcome_at: string | null
  match_score: string | null
}

export interface Documents {
  resumes: ResumeDocument[]
  letters: LetterDocument[]
  current_rules_version: number
}

// ── the workshop ─────────────────────────────────────────────────────

export interface QueuedLetter {
  vacancy_id: string
  title: string
  company: string | null
  score: string
  has_letter: boolean
  /** True when the agent applies to it; false means «откликнуться самому» on `url`. */
  via_agent: boolean
  source_slug: string
  url: string
}

export interface OutcomeEvidence {
  sent: number
  answered: number
  positive: number
  text_unknown: number
  similar: number
  blocked: number
  used: number
}

export interface WorkshopResult {
  vacancy_id: string
  title: string
  company: string | null
  saved: boolean
  skipped: string | null
  text: string | null
  characters: number
  from_model: boolean
  matched_skills: number
  missing_skills: number
  problems: LetterProblem[]
  evidence: OutcomeEvidence
  evidence_is_enough: boolean
}

// ── the profile ──────────────────────────────────────────────────────

export interface ProfileSkill {
  id: string
  canonical_name: string
  raw_names: string[]
  years: string | null
  level: 'basic' | 'working' | 'strong' | 'expert'
  evidence: 'corroborated' | 'stated'
  last_used_year: number | null
}

export interface Profile {
  id: string
  name: string | null
  headline: string | null
  seniority: Seniority | null
  total_years: string | null
  summary: string | null
  locations: string[]
  relocation: boolean
  remote_pref: Remote | null
  salary_min: string | null
  salary_currency: string | null
  languages: { code?: string; level?: string }[]
  is_active: boolean
  parse_status: ParseStatus
  parse_error: string | null
  resume_filename: string | null
  resume_format: string | null
  resume_size_bytes: number | null
  skills: ProfileSkill[]
  created_at: string
  updated_at: string
}
