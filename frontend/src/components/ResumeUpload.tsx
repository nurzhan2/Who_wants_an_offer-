import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { apiGet, apiUpload } from '@/api/client'
import { Button } from '@/components/ui'
import { explain } from '@/lib/errors'
import type { ParseStatus } from '@/types/api'

/**
 * Загрузить резюме: the upload that used to be a curl command.
 *
 * The upload itself answers at once; the parse runs on the server for about
 * three minutes (one model call and one embedding), so the component polls the
 * profile and says what is happening and for how long. When the parse ends the
 * new resume becomes the active one and every screen that depends on it is
 * refreshed.
 */

interface Accepted {
  profile_id: string
  parse_status: ParseStatus
}

interface ParsedProfile {
  id: string
  name: string | null
  headline: string | null
  parse_status: ParseStatus
  parse_error: string | null
}

const ACCEPT = '.pdf,.docx,.txt,.md'
const POLL_MS = 3_000

export function ResumeUpload() {
  const client = useQueryClient()
  const input = useRef<HTMLInputElement>(null)
  const [profileId, setProfileId] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const [now, setNow] = useState(() => Date.now())

  const upload = useMutation({
    mutationFn: (file: File) => {
      const form = new FormData()
      form.append('file', file)
      return apiUpload<Accepted>('/api/v1/resume/upload', form)
    },
    onSuccess: (accepted) => {
      setProfileId(accepted.profile_id)
      setStartedAt(Date.now())
    },
  })

  const parsed = useQuery({
    queryKey: ['profile', profileId],
    queryFn: () => apiGet<ParsedProfile>(`/api/v1/profile/${profileId ?? ''}`),
    enabled: profileId !== null,
    refetchInterval: (query) => (query.state.data?.parse_status === 'pending' || !query.state.data ? POLL_MS : false),
  })

  const status = parsed.data?.parse_status ?? (profileId ? 'pending' : null)

  useEffect(() => {
    if (status !== 'pending') return
    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 1_000)
    return () => {
      window.clearInterval(timer)
    }
  }, [status])

  useEffect(() => {
    if (status === 'ready') {
      for (const key of [['profile'], ['overview'], ['documents'], ['vacancies'], ['vacancy']]) {
        void client.invalidateQueries({ queryKey: key })
      }
    }
  }, [status, client])

  const elapsed = startedAt === null ? 0 : Math.max(0, Math.round((now - startedAt) / 1000))

  return (
    <div className="border border-hairline p-card">
      <p className="font-semibold">Резюме</p>
      <p className="mt-1 max-w-2xl text-small text-muted">
        PDF, DOCX, TXT или Markdown, до 10 МБ. После загрузки сервер разбирает его около трёх
        минут; новое резюме станет активным, и подбор нужно будет пересчитать.
      </p>
      <input
        ref={input}
        type="file"
        accept={ACCEPT}
        className="sr-only"
        aria-label="Файл резюме"
        onChange={(event) => {
          const file = event.target.files?.[0]
          if (file) upload.mutate(file)
          event.target.value = ''
        }}
      />
      <div className="mt-4 flex flex-wrap items-center gap-4">
        <Button
          disabled={upload.isPending || status === 'pending'}
          onClick={() => {
            input.current?.click()
          }}
        >
          {upload.isPending ? 'Загружаем…' : status === 'pending' ? 'Разбирается…' : 'Загрузить резюме'}
        </Button>
        {status === 'pending' ? (
          <span className="text-small" role="status" aria-live="polite">
            Разбор идёт {elapsed} с — обычно около трёх минут. Страницу можно не обновлять.
          </span>
        ) : null}
      </div>
      {upload.isError ? (
        <p className="mt-3 text-small font-semibold" role="alert">
          {explain(upload.error).reason}
        </p>
      ) : null}
      {status === 'ready' && parsed.data ? (
        <p className="mt-3 text-small" role="status">
          Готово: {parsed.data.name ?? 'резюме'}
          {parsed.data.headline ? ` — ${parsed.data.headline}` : ''}. Теперь на «Обзоре» нажмите
          «Пересчитать подбор».
        </p>
      ) : null}
      {status === 'failed' ? (
        <p className="break-anywhere mt-3 text-small font-semibold" role="alert">
          Разобрать не удалось{parsed.data?.parse_error ? `: ${parsed.data.parse_error}` : '.'}
        </p>
      ) : null}
    </div>
  )
}
