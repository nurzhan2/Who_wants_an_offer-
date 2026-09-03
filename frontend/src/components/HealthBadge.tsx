import { useHealth } from '@/hooks/useHealth'

const LABELS: Record<string, string> = {
  ok: 'Работает',
  degraded: 'Деградация',
}

export function HealthBadge() {
  const { data, isPending, isError } = useHealth()

  if (isPending) {
    return <span className="text-sm text-slate-500">Проверяем бэкенд…</span>
  }

  if (isError) {
    return <span className="text-sm text-red-600">Бэкенд недоступен</span>
  }

  const healthy = data.status === 'ok'

  return (
    <span className="inline-flex items-center gap-2 text-sm">
      <span
        className={`h-2 w-2 rounded-full ${healthy ? 'bg-emerald-500' : 'bg-amber-500'}`}
        aria-hidden
      />
      <span className={healthy ? 'text-emerald-700' : 'text-amber-700'}>
        {LABELS[data.status] ?? data.status}
      </span>
      <span className="text-slate-400">
        v{data.version} · {data.environment}
      </span>
    </span>
  )
}
