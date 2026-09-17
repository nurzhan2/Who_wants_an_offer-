import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { apiGet, apiSend } from '@/api/client'
import type {
  Board,
  CursorPage,
  Documents,
  MatchMode,
  Overview,
  Profile,
  QueuedLetter,
  VacancyCard,
  VacancyListItem,
  WorkshopResult,
} from '@/types/api'

/**
 * Every read the dashboard makes, in one place.
 *
 * Two decisions worth stating.
 *
 * **The overview refetches; the rest does not.** A crawl runs for about twenty
 * minutes and writes as it goes, so the numbers on the first screen are stale
 * the moment they are drawn — that screen is the one somebody leaves open while
 * a run is going. The vacancy list is a thing being read, and rows shifting
 * under a reader's cursor because a poll landed is worse than a number being a
 * minute old.
 *
 * **Filters are part of the key.** The list is keyset-paginated: a cursor is
 * only meaningful for the filter it was produced under, so the two travel
 * together into the cache key and changing a filter cannot reuse a page from
 * another one.
 */

const OVERVIEW_REFETCH_MS = 30_000

export function useOverview(): UseQueryResult<Overview> {
  return useQuery({
    queryKey: ['overview'],
    queryFn: () => apiGet<Overview>('/api/v1/overview'),
    refetchInterval: OVERVIEW_REFETCH_MS,
  })
}

export interface VacancyQuery {
  city?: string | undefined
  bucket?: string[] | undefined
  source?: string[] | undefined
  remote?: string[] | undefined
  score_min?: number | undefined
  salary_min?: number | undefined
  include_unpriced?: boolean | undefined
  mode?: MatchMode | undefined
  q?: string | undefined
  cursor?: string | undefined
  limit?: number | undefined
}

export function useVacancies(query: VacancyQuery): UseQueryResult<CursorPage<VacancyListItem>> {
  return useQuery({
    queryKey: ['vacancies', query],
    queryFn: () =>
      apiGet<CursorPage<VacancyListItem>>('/api/v1/vacancies', {
        ...query,
        with_total: true,
        with_facets: true,
      }),
    // Keeps the previous page on screen while the next one loads, so changing a
    // filter does not blank the table and jump the scroll position.
    placeholderData: (previous) => previous,
  })
}

export function useVacancy(id: string | null): UseQueryResult<VacancyCard> {
  return useQuery({
    queryKey: ['vacancy', id],
    queryFn: () => apiGet<VacancyCard>(`/api/v1/vacancies/${id ?? ''}`),
    enabled: id !== null,
  })
}

export function useBoard(): UseQueryResult<Board> {
  return useQuery({ queryKey: ['board'], queryFn: () => apiGet<Board>('/api/v1/tracker/board') })
}

export function useDocuments(): UseQueryResult<Documents> {
  return useQuery({
    queryKey: ['documents'],
    queryFn: () => apiGet<Documents>('/api/v1/documents/overview'),
  })
}

export function useLetterQueue(): UseQueryResult<QueuedLetter[]> {
  return useQuery({
    queryKey: ['letter-queue'],
    queryFn: () => apiGet<QueuedLetter[]>('/api/v1/documents/queue'),
  })
}

export function useActiveProfile(): UseQueryResult<Profile> {
  return useQuery({
    queryKey: ['profile', 'active'],
    queryFn: () => apiGet<Profile>('/api/v1/profile/active'),
    // A 404 here is the answer — no resume has been uploaded — not a transport
    // failure, so there is nothing for a retry to fix.
    retry: false,
  })
}

/**
 * Writing one letter: the only mutation this interface has.
 *
 * There is no send. An application goes out through `wwao apply --send`, where
 * the letter is printed on a confirmation card and a person at the keyboard
 * says yes to that letter for that vacancy; a browser cannot make that promise,
 * so the button does not exist here.
 */
export function useWriteLetter() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ vacancyId, force }: { vacancyId: string; force: boolean }) =>
      apiSend<WorkshopResult>('POST', '/api/v1/documents/letters', {
        vacancy_id: vacancyId,
        force,
      }),
    onSuccess: (result) => {
      // The queue's "has_letter" flag, the documents list and the vacancy card
      // all just changed. Invalidating rather than patching: the server decides
      // what a saved letter looks like, including the rules version it recorded.
      if (!result.saved) return
      void client.invalidateQueries({ queryKey: ['letter-queue'] })
      void client.invalidateQueries({ queryKey: ['documents'] })
      void client.invalidateQueries({ queryKey: ['vacancy', result.vacancy_id] })
      void client.invalidateQueries({ queryKey: ['board'] })
    },
  })
}
