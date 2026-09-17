import { useMutation, useQuery, useQueryClient, type UseQueryResult } from '@tanstack/react-query'

import { apiGet, apiSend } from '@/api/client'
import type { Notices } from '@/types/notices'
import type { Operation, OperationKind, OperationsState } from '@/types/operations'

/**
 * The operations panel and the notices banner.
 *
 * **Polling follows the work.** While anything runs the panel asks every two
 * seconds, so a count moves as it moves; when nothing does, every fifteen, which
 * is still quick enough to notice a watcher starting up. A finished operation
 * invalidates the screens it changed, so the result is on the page without a
 * reload.
 */

const BUSY_MS = 2_000
const IDLE_MS = 15_000

/** What each operation changes, so its end refreshes exactly those screens. */
const TOUCHES: Record<OperationKind, string[][]> = {
  crawl: [['overview'], ['vacancies']],
  embed: [['overview']],
  match: [['overview'], ['vacancies'], ['vacancy']],
  letters: [['overview'], ['letter-queue'], ['documents'], ['board'], ['vacancy']],
  outcomes: [['overview'], ['board'], ['notices'], ['documents']],
  send: [['overview'], ['board'], ['notices'], ['vacancy'], ['confirmations']],
}

export function useOperations(): UseQueryResult<OperationsState> {
  const client = useQueryClient()
  return useQuery({
    queryKey: ['operations'],
    queryFn: async () => {
      const previous = client.getQueryData<OperationsState>(['operations'])
      const next = await apiGet<OperationsState>('/api/v1/operations')
      refreshWhatFinished(client, previous, next)
      return next
    },
    refetchInterval: (query) => ((query.state.data?.busy.length ?? 0) > 0 ? BUSY_MS : IDLE_MS),
    // A crawl runs for twenty minutes in a tab nobody is looking at; its end
    // still has to refresh the screens it changed.
    refetchIntervalInBackground: true,
  })
}

function refreshWhatFinished(
  client: ReturnType<typeof useQueryClient>,
  previous: OperationsState | undefined,
  next: OperationsState,
): void {
  if (!previous) return
  for (const operation of next.operations) {
    const before = previous.operations.find((item) => item.id === operation.id)
    const wasLive = before !== undefined && !isFinished(before)
    if (wasLive && isFinished(operation)) {
      for (const key of TOUCHES[operation.kind]) {
        void client.invalidateQueries({ queryKey: key })
      }
    }
  }
}

export function isFinished(operation: Operation): boolean {
  return (
    operation.status === 'success' ||
    operation.status === 'failed' ||
    operation.status === 'cancelled'
  )
}

export function useStartOperation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (kind: OperationKind) =>
      apiSend<Operation>('POST', '/api/v1/operations', { kind }),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['operations'] })
    },
  })
}

export function useCancelOperation() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => apiSend<Operation>('DELETE', `/api/v1/operations/${id}`),
    onSettled: () => {
      void client.invalidateQueries({ queryKey: ['operations'] })
    },
  })
}

export function useNotices(): UseQueryResult<Notices> {
  return useQuery({
    queryKey: ['notices'],
    queryFn: () => apiGet<Notices>('/api/v1/notices'),
    refetchInterval: 60_000,
  })
}
