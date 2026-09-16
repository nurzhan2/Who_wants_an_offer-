export type ServiceState = 'ok' | 'degraded'
export type ComponentState = 'ok' | 'error'

export interface ComponentHealth {
  status: ComponentState
  detail: string | null
  latency_ms: number | null
}

export interface HealthResponse {
  status: ServiceState
  version: string
  code_fingerprint: string
  environment: string
  components: Record<string, ComponentHealth>
}
