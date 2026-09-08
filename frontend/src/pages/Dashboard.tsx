import { HealthBadge } from '@/components/HealthBadge'

export function Dashboard() {
  return (
    <div className="flex flex-col gap-8">
      <p className="max-w-2xl text-muted">
        Каркас готов. Дашборд с вакансиями появится в фазе 7.
      </p>
      <div className="rounded-field border border-line bg-card p-4">
        <HealthBadge />
      </div>
    </div>
  )
}
