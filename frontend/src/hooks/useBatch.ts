import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { apiGet, apiSend } from '@/api/client'
import type { BatchConfirmResult, BatchPlan } from '@/types/autopilot'

/**
 * The batch: what is ready to send, what was set aside, and one confirmation.
 *
 * Fetched fresh whenever the screen opens (`staleTime: 0`), for the same reason
 * the single card is: every row carries the digest its confirmation binds, and
 * a digest read ten minutes ago is exactly what the server is there to refuse.
 */

export function useBatchPlan(open: boolean): UseQueryResult<BatchPlan> {
  return useQuery({
    queryKey: ['batch'],
    queryFn: () => apiGet<BatchPlan>('/api/v1/tracker/batch'),
    enabled: open,
    staleTime: 0,
    refetchOnMount: 'always',
  })
}

/** The set-aside list on its own, for the panel that shows it beside the queue. */
export function useSetAside(): UseQueryResult<BatchPlan> {
  return useQuery({
    queryKey: ['batch'],
    queryFn: () => apiGet<BatchPlan>('/api/v1/tracker/batch'),
    refetchInterval: 60_000,
  })
}

export function useConfirmBatch() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (rows: { vacancy_id: string; card_digest: string }[]) =>
      apiSend<BatchConfirmResult>('POST', '/api/v1/tracker/batch', { items: rows }),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['batch'] })
      void client.invalidateQueries({ queryKey: ['confirmations'] })
      void client.invalidateQueries({ queryKey: ['board'] })
    },
  })
}
