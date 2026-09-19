/** `app/schemas/autopilot.py`: the batch screen and what it left out. */

import type { QueueItem } from '@/types/confirmations'

export type SetAsideKind =
  | 'no_letter'
  | 'archived'
  | 'closed'
  | 'experience_gap'
  | 'language'
  | 'stale_page'
  | 'letter_rules'
  | 'letter_audit'
  | 'unservable'

/** One vacancy the autopilot will not apply to, with the sentence saying why. */
export interface SetAsideItem {
  vacancy_id: string
  external_id: string
  title: string
  company: string | null
  url: string
  score: string | null
  kind: SetAsideKind
  reason: string
}

/** One application waiting to be confirmed, with the digest to confirm it by. */
export interface BatchItem {
  vacancy_id: string
  item: QueueItem
  card_digest: string
  confirmed: boolean
  confirmed_at: string | null
}

export interface BatchPlan {
  items: BatchItem[]
  set_aside: SetAsideItem[]
  /** How many rows one confirmation may cover right now. */
  limit: number
  limit_reason: string
  last_sent_at: string | null
  first_batch: boolean
}

export interface BatchConfirmOutcome {
  vacancy_id: string
  confirmed: boolean
  detail: string | null
}

export interface BatchConfirmResult {
  confirmed: number
  outcomes: BatchConfirmOutcome[]
  limit: number
  limit_reason: string
}
