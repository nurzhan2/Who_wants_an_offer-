/** Every call the Мастерская screen makes, in one place. */
import { apiGet, apiSend, apiUpload } from '@/api/client'
import type {
  Preview,
  Reference,
  ReferenceCreated,
  ReferenceKind,
  Rule,
  RuleCreate,
  RuleUpdate,
  VacancyChoice,
} from '@/types/workshop'

const BASE = '/api/v1/workshop'

export function fetchRules(): Promise<Rule[]> {
  return apiGet<Rule[]>(`${BASE}/rules`)
}

export function createRule(payload: RuleCreate): Promise<Rule> {
  return apiSend<Rule>('POST', `${BASE}/rules`, payload)
}

export function updateRule(id: string, changes: RuleUpdate): Promise<Rule> {
  return apiSend<Rule>('PATCH', `${BASE}/rules/${id}`, changes)
}

export function deleteRule(id: string): Promise<null> {
  return apiSend<null>('DELETE', `${BASE}/rules/${id}`)
}

export function fetchReferences(): Promise<Reference[]> {
  return apiGet<Reference[]>(`${BASE}/references`)
}

/**
 * Store a reference, from a file or from pasted text.
 *
 * The endpoint takes exactly one of the two and refuses both, so this builds
 * the form that way rather than sending an empty `text` alongside a file.
 */
export function createReference(input: {
  kind: ReferenceKind
  title: string
  note: string
  file: File | null
  text: string
}): Promise<ReferenceCreated> {
  const form = new FormData()
  form.set('kind', input.kind)
  form.set('title', input.title)
  if (input.note.trim()) {
    form.set('note', input.note.trim())
  }
  if (input.file) {
    form.set('file', input.file)
  } else {
    form.set('text', input.text)
  }
  return apiUpload<ReferenceCreated>(`${BASE}/references`, form)
}

export function updateReference(
  id: string,
  changes: { title?: string; note?: string; is_active?: boolean },
): Promise<Reference> {
  return apiSend<Reference>('PATCH', `${BASE}/references/${id}`, changes)
}

export function deleteReference(id: string): Promise<null> {
  return apiSend<null>('DELETE', `${BASE}/references/${id}`)
}

export function fetchVacancies(query: string): Promise<VacancyChoice[]> {
  const search = query.trim() ? `?q=${encodeURIComponent(query.trim())}` : ''
  return apiGet<VacancyChoice[]>(`${BASE}/vacancies${search}`)
}

export function requestPreview(vacancyId: string): Promise<Preview> {
  return apiSend<Preview>('POST', `${BASE}/preview`, { vacancy_id: vacancyId })
}
