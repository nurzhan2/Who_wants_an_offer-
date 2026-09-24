/**
 * The operations panel's wire types: `app/schemas/operations.py`, one to one.
 *
 * `done` and `total` are `null` when an operation cannot count — never zero.
 */

export type OperationKind =
  | 'crawl'
  | 'embed'
  | 'match'
  | 'letters'
  | 'outcomes'
  | 'send'
  | 'chain'

/** `ChainStepStatus`. `skipped` is what a resumed chain does with finished work. */
export type ChainStepStatus = 'pending' | 'running' | 'done' | 'failed' | 'skipped'

/** One step of the chain, as the panel draws it. */
export interface ChainStep {
  key: string
  title: string
  status: ChainStepStatus
  note: string | null
  report: string[]
  started_at: string | null
  finished_at: string | null
}

export type OperationStatus =
  | 'queued'
  | 'waiting_agent'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'

export interface Operation {
  id: string
  kind: OperationKind
  status: OperationStatus
  message: string
  queued_at: string
  started_at: string | null
  finished_at: string | null
  duration_seconds: number | null
  done: number | null
  total: number | null
  report: string[]
  error: string | null
  /** The chain's steps, in order. Empty for every other kind. */
  steps: ChainStep[]
}

export interface OperationsState {
  operations: Operation[]
  busy: OperationKind[]
  /** When a local watcher last asked for work; null if none has this process. */
  agent_seen_at: string | null
}
