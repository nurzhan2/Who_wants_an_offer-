import { HealthBadge } from '@/components/HealthBadge'

export function Dashboard() {
  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
        Who wants an offer?
      </h1>
      <p className="mt-3 text-slate-600">
        Каркас готов. Дашборд с вакансиями появится в фазе 7.
      </p>
      <div className="mt-8 rounded-lg border border-slate-200 bg-white p-4">
        <HealthBadge />
      </div>
    </main>
  )
}
