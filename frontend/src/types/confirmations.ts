/** `app/schemas/confirmations.py` and the queue item it carries. */

import type { Bucket } from '@/types/api'

export interface QueueSkill {
  canonical_name: string
}

export interface QueueMatch {
  score: string
  bucket: Bucket
  verdict: string | null
  application_angle: string | null
  matched_skills: QueueSkill[]
  missing_required: QueueSkill[]
  missing_nice: QueueSkill[]
  red_flags: string[]
  experience_gap_years: string | null
}

export interface QueueATS {
  overall: 'ok' | 'degraded' | 'unreadable'
  score: number
  critical: string[]
  requirements_total: number
  requirements_present: number
  unstated: string[]
  absent: number
}

/** Exactly what the agent would be handed for this vacancy. */
export interface QueueItem {
  vacancy_id: string
  url: string
  title: string
  company: string | null
  letter: string | null
  archived: boolean
  closed_for_applicants: boolean
  score: string | null
  score_explanation: string | null
  source: string
  match: QueueMatch | null
  anonymous: boolean
  employer_on_additional_check: boolean
  ats: QueueATS | null
  hh_lines: string[]
}

export interface ConfirmationState {
  confirmed_at: string
  valid: boolean
  reason: string | null
}

export interface ConfirmationCard {
  vacancy_id: string
  title: string
  company: string | null
  source: string | null
  url: string | null
  external_id: string | null
  published_at: string | null
  last_seen_at: string | null
  is_active: boolean
  resume_notice: string | null
  blockers: string[]
  item: QueueItem | null
  card_digest: string | null
  state: ConfirmationState | null
  ttl_hours: number
}

export interface ConfirmedVacancy {
  external_id: string
  title: string
  company: string | null
  confirmed_at: string
}

export interface ConfirmedList {
  items: ConfirmedVacancy[]
  no_longer_valid: number
}
