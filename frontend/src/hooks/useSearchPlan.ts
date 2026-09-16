import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiGet, apiSend } from '@/api/client'
import type { ActiveProfile } from '@/types/contact'
import type { SearchPlan } from '@/types/searchPlan'

const PLAN_KEY = ['sources', 'plan'] as const

/**
 * What the next run will ask each source for.
 *
 * Built by the server from the active profile without contacting any source,
 * so it is cheap to refetch after every save — which is the point: the owner
 * changes their job titles and sees which hh pages and which search queries
 * that changed.
 */
export function useSearchPlan() {
  return useQuery({
    queryKey: PLAN_KEY,
    queryFn: () => apiGet<SearchPlan>('/api/v1/sources/plan'),
    retry: false,
  })
}

/**
 * Replace the job titles the owner is looking for.
 *
 * The whole list goes every time: the server treats it as one value, cleans it
 * (spaces, repeats) and answers with what it stored. The plan is refetched
 * rather than guessed at, since the server is the one that builds it.
 */
export function useSaveTargetTitles(profileId: string) {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (titles: string[]) =>
      apiSend<ActiveProfile>('PATCH', `/api/v1/profile/${profileId}`, {
        target_titles: titles,
      }),
    onSuccess: (profile) => {
      client.setQueryData(['profile', 'active'], profile)
      void client.invalidateQueries({ queryKey: PLAN_KEY })
    },
  })
}
