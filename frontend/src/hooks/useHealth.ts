import { useQuery } from '@tanstack/react-query'

import { apiGet } from '@/api/client'
import type { HealthResponse } from '@/types/health'

export function useHealth() {
  return useQuery({
    queryKey: ['health'],
    queryFn: () => apiGet<HealthResponse>('/health'),
    refetchInterval: 30_000,
  })
}
