/**
 * The workshop's contracts, transcribed from `app/schemas/workshop.py`.
 *
 * `RuleParams` is a discriminated union on `kind`, exactly as the backend's is.
 * That is what lets the form switch on one field and lets TypeScript refuse a
 * payload with the wrong fields for its kind — the same guarantee the Pydantic
 * union gives on the other side, rather than a `Record<string, unknown>` that
 * would let a typo reach the API and come back as a 422 nobody can read.
 */

export type ReferenceKind = 'cv' | 'cover_letter'
export type RuleScope = 'cv' | 'cover_letter' | 'both'
export type RuleSeverity = 'hard' | 'soft'
export type LengthUnit = 'characters' | 'words'
export type DateFormatPattern = 'mm.yyyy' | 'mm/yyyy' | 'yyyy-mm' | 'yyyy'

export type RuleKind =
  | 'section_item_count'
  | 'required_section'
  | 'required_keyword'
  | 'date_format'
  | 'forbidden_phrase'
  | 'length'
  | 'no_links'
  | 'no_contact_handles'

export type RuleParams =
  | { kind: 'section_item_count'; section: string; minimum?: number | null; maximum?: number | null }
  | { kind: 'required_section'; section: string }
  | { kind: 'required_keyword'; keyword: string; case_sensitive?: boolean }
  | { kind: 'date_format'; pattern: DateFormatPattern }
  | { kind: 'forbidden_phrase'; phrase: string; case_sensitive?: boolean }
  | { kind: 'length'; unit: LengthUnit; minimum?: number | null; maximum?: number | null }
  | { kind: 'no_links' }
  | { kind: 'no_contact_handles' }

export interface Rule {
  /** A UUID, or `builtin:<kind>` for a rule that cannot be removed. */
  id: string
  kind: RuleKind
  scope: RuleScope
  severity: RuleSeverity
  params: RuleParams
  /** The owner's own sentence. Never sent to the model. */
  message: string
  is_active: boolean
  is_builtin: boolean
  /** The same rule as the model is asked for it, in English. */
  asked_as: string
}

export interface RuleCreate {
  scope: RuleScope
  severity: RuleSeverity
  params: RuleParams
  message: string
  is_active?: boolean
}

export interface RuleUpdate {
  scope?: RuleScope
  severity?: RuleSeverity
  params?: RuleParams
  message?: string
  is_active?: boolean
}

export interface Reference {
  id: string
  kind: ReferenceKind
  title: string
  note: string | null
  is_active: boolean
  characters: number
  preview: string
  source_filename: string | null
  source_format: string | null
  size_bytes: number | null
  created_at: string
  updated_at: string
}

export interface ReferenceCreated {
  reference: Reference & { text: string }
  warnings: string[]
}

export interface Violation {
  rule_id: string
  kind: RuleKind
  severity: RuleSeverity
  message: string
  detail: string
}

export interface VacancyChoice {
  id: string
  title: string
  company: string | null
  city: string | null
}

export interface Preview {
  written: boolean
  text: string | null
  source: string | null
  language: string | null
  characters: number
  attempts: number
  warnings: Violation[]
  broken_rules: Violation[]
  detail: string | null
  rules_applied: number
  references_used: number
  examples_used: number
}

/**
 * An RFC 7807 problem document, as `app/core/exceptions.py` renders one.
 *
 * `claims` is the field that matters here: when a rule is refused for naming a
 * skill the profile does not have, it carries the names. A refusal with no
 * names is unactionable, so the form shows them.
 */
export interface Problem {
  title: string
  detail: string
  status: number
  claims?: string[]
}
