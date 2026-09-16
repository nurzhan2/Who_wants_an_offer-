/** `app/schemas/notices.py`: what the owner must see on every screen. */

export type NoticeKind = 'resume_visibility'

export interface Notice {
  kind: NoticeKind
  title: string
  /** hh's own sentence, verbatim. */
  quote: string | null
  body: string
  /** Newer evidence suggests the cause has gone. */
  resolved: boolean
  applications: number
  first_seen_at: string | null
  last_seen_at: string | null
}

export interface Notices {
  items: Notice[]
}
