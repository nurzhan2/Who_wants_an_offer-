/**
 * The document endpoints.
 *
 * Kept apart from the components so the two buttons are one function call each,
 * and so the one URL that is not fetched — the download — is written down next
 * to the ones that are.
 */
import { apiGet, apiPost } from '@/api/client'
import type {
  DocumentCandidate,
  DocumentKind,
  DocumentSummary,
  DocumentVersion,
  GeneratedDocument,
} from '@/types/documents'

const BASE = '/api/v1/documents'

export function generateCv(vacancyId: string): Promise<GeneratedDocument> {
  return apiPost<GeneratedDocument>(`${BASE}/cv/${vacancyId}`)
}

export function generateCoverLetter(vacancyId: string): Promise<GeneratedDocument> {
  return apiPost<GeneratedDocument>(`${BASE}/cover-letter/${vacancyId}`)
}

export function listCandidates(): Promise<DocumentCandidate[]> {
  return apiGet<DocumentCandidate[]>(`${BASE}/candidates`)
}

export function listDocuments(): Promise<DocumentSummary[]> {
  return apiGet<DocumentSummary[]>(BASE)
}

export function listVersions(vacancyId: string, kind: DocumentKind): Promise<DocumentVersion[]> {
  return apiGet<DocumentVersion[]>(`${BASE}/versions/${vacancyId}/${kind}`)
}

/**
 * Where a stored version's file lives.
 *
 * A URL rather than a fetch: the browser downloads it by following a link,
 * which keeps the Content-Disposition filename the server chose — including
 * the Cyrillic one, which is the ordinary case here. Fetching the bytes into
 * JavaScript and re-attaching a name would throw that header away and rebuild
 * it worse.
 */
export function fileUrl(documentId: string): string {
  return `${BASE}/${documentId}/file`
}
