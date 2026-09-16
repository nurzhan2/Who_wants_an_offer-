/** What `GET /api/v1/sources/plan` answers: the next run's search, per source. */

/** How a source turns the search into requests. */
export type SearchUse = 'query' | 'filter' | 'catalog'

/** Where the plan's words came from. */
export type SearchBasis = 'titles' | 'skills' | 'headline' | 'none'

export interface SearchPreview {
  use: SearchUse
  terms: string[]
  more: number
  note: string | null
}

export interface SourceSearchPreview {
  slug: string
  name: string
  enabled: boolean
  inactive: { code: string; detail: string } | null
  preview: SearchPreview
}

export interface SearchPlan {
  target_titles: string[]
  basis: SearchBasis
  intent: string | null
  queries: number
  dropped: number
  sources: SourceSearchPreview[]
}
