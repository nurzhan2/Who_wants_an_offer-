import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { apiGet, apiSend } from '@/api/client'
import type { ConfirmationCard, ConfirmedList } from '@/types/confirmations'

/**
 * The confirmation card and the list of confirmed applications.
 *
 * The card is fetched fresh every time the modal opens (`staleTime: 0`): its
 * digest is what the confirmation is bound to, and a digest from a card read
 * an hour ago is exactly what the server is there to refuse.
 */

export function useConfirmationCard(vacancyId: string, open: boolean): UseQueryResult<ConfirmationCard> {
  return useQuery({
    queryKey: ['confirmations', vacancyId],
    queryFn: () => apiGet<ConfirmationCard>(`/api/v1/tracker/confirmations/${vacancyId}`),
    enabled: open,
    staleTime: 0,
    refetchOnMount: 'always',
  })
}

export function useConfirmedList(): UseQueryResult<ConfirmedList> {
  return useQuery({
    queryKey: ['confirmations', 'list'],
    queryFn: () => apiGet<ConfirmedList>('/api/v1/tracker/confirmations'),
    refetchInterval: 30_000,
  })
}

function useRefresh(vacancyId: string) {
  const client = useQueryClient()
  return (card: ConfirmationCard) => {
    client.setQueryData(['confirmations', vacancyId], card)
    void client.invalidateQueries({ queryKey: ['confirmations', 'list'] })
    void client.invalidateQueries({ queryKey: ['board'] })
  }
}

export function useConfirm(vacancyId: string) {
  const refresh = useRefresh(vacancyId)
  return useMutation({
    mutationFn: (cardDigest: string) =>
      apiSend<ConfirmationCard>('POST', `/api/v1/tracker/confirmations/${vacancyId}`, {
        card_digest: cardDigest,
      }),
    onSuccess: refresh,
  })
}

export function useWithdraw(vacancyId: string) {
  const refresh = useRefresh(vacancyId)
  return useMutation({
    mutationFn: () =>
      apiSend<ConfirmationCard>('DELETE', `/api/v1/tracker/confirmations/${vacancyId}`),
    onSuccess: refresh,
  })
}
