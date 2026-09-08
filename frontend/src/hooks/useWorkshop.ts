/**
 * Queries and mutations for the Мастерская screen.
 *
 * Every mutation invalidates the list it changed rather than patching the cache
 * by hand. The lists are short — a handful of rules, a handful of references —
 * so a refetch costs nothing, and an optimistic update that guessed wrong would
 * show a rule as saved that the server refused for naming a skill the profile
 * does not have. That refusal is the whole point of this screen; it must never
 * be the thing the cache papers over.
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import * as api from '@/api/workshop'
import type { ReferenceKind, RuleCreate, RuleUpdate } from '@/types/workshop'

const RULES = ['workshop', 'rules'] as const
const REFERENCES = ['workshop', 'references'] as const
const VACANCIES = ['workshop', 'vacancies'] as const

export function useRules() {
  return useQuery({ queryKey: RULES, queryFn: api.fetchRules })
}

export function useReferences() {
  return useQuery({ queryKey: REFERENCES, queryFn: api.fetchReferences })
}

export function useVacancyChoices(query: string) {
  return useQuery({
    queryKey: [...VACANCIES, query],
    queryFn: () => api.fetchVacancies(query),
  })
}

export function useCreateRule() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (payload: RuleCreate) => api.createRule(payload),
    onSuccess: () => client.invalidateQueries({ queryKey: RULES }),
  })
}

export function useUpdateRule() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({ id, changes }: { id: string; changes: RuleUpdate }) =>
      api.updateRule(id, changes),
    onSuccess: () => client.invalidateQueries({ queryKey: RULES }),
  })
}

export function useDeleteRule() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteRule(id),
    onSuccess: () => client.invalidateQueries({ queryKey: RULES }),
  })
}

export function useCreateReference() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (input: {
      kind: ReferenceKind
      title: string
      note: string
      file: File | null
      text: string
    }) => api.createReference(input),
    onSuccess: () => client.invalidateQueries({ queryKey: REFERENCES }),
  })
}

export function useUpdateReference() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: ({
      id,
      changes,
    }: {
      id: string
      changes: { title?: string; note?: string; is_active?: boolean }
    }) => api.updateReference(id, changes),
    onSuccess: () => client.invalidateQueries({ queryKey: REFERENCES }),
  })
}

export function useDeleteReference() {
  const client = useQueryClient()
  return useMutation({
    mutationFn: (id: string) => api.deleteReference(id),
    onSuccess: () => client.invalidateQueries({ queryKey: REFERENCES }),
  })
}

/**
 * The trial letter.
 *
 * Not a query: it costs a model call, and a query would run it on mount and
 * again on every refetch. It runs when somebody presses the button.
 */
export function usePreview() {
  return useMutation({ mutationFn: (vacancyId: string) => api.requestPreview(vacancyId) })
}
