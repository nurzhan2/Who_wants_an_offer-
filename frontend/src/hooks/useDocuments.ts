import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  generateCoverLetter,
  generateCv,
  listCandidates,
  listDocuments,
  listVersions,
} from '@/api/documents'
import type { DocumentKind, GeneratedDocument } from '@/types/documents'

/**
 * The vacancies the two buttons appear on, with what each already has.
 *
 * The counts come down with the list rather than per row, so a screen of twenty
 * vacancies is one request; the backend computes them in the same query.
 */
export function useDocumentCandidates() {
  return useQuery({
    queryKey: ['documents', 'candidates'],
    queryFn: listCandidates,
  })
}

export function useDocumentHistory(vacancyId: string, kind: DocumentKind, enabled: boolean) {
  return useQuery({
    queryKey: ['documents', 'versions', vacancyId, kind],
    queryFn: () => listVersions(vacancyId, kind),
    enabled,
  })
}

export function useGeneratedDocuments() {
  return useQuery({
    queryKey: ['documents', 'list'],
    queryFn: listDocuments,
  })
}

/**
 * Generating one document, with the state a person can see while it runs.
 *
 * This is where "генерация асинхронная, состояние видно" actually lives. The
 * request takes as long as the model does — tens of seconds — and the mutation's
 * own `isPending` is what the button renders while it waits. Nothing polls: the
 * work is not a background job with an id, it is a request the page made and is
 * still holding, and inventing a job row to poll would add a moving part
 * without adding an answer.
 *
 * On success the candidate list and the documents list are invalidated, because
 * both just became wrong: the version count went up by one, and there is a new
 * row to list. `retry: false` because a failed generation may have cost a model
 * call, and silently paying for a second one is not a default anybody chose.
 */
export function useGenerateDocument(kind: DocumentKind) {
  const queryClient = useQueryClient()
  const generate = kind === 'cv' ? generateCv : generateCoverLetter

  return useMutation<GeneratedDocument, Error, string>({
    mutationFn: (vacancyId: string) => generate(vacancyId),
    retry: false,
    onSuccess: (result) => {
      void queryClient.invalidateQueries({ queryKey: ['documents', 'candidates'] })
      void queryClient.invalidateQueries({ queryKey: ['documents', 'list'] })
      void queryClient.invalidateQueries({
        queryKey: ['documents', 'versions', result.vacancy_id, kind],
      })
    },
  })
}
