/**
 * The dashboard shell.
 *
 * Two screens, switched by state rather than by a router. Adding one for two
 * tabs would be a dependency and a URL scheme to maintain before there is
 * anything to link to; the vacancy list lands in phase 7 and that is the change
 * that will want real routes.
 */
import { useState } from 'react'

import { HealthBadge } from '@/components/HealthBadge'
import { Workshop } from '@/pages/Workshop'

type Screen = 'overview' | 'workshop'

const TABS: { id: Screen; label: string }[] = [
  { id: 'overview', label: 'Обзор' },
  { id: 'workshop', label: 'Мастерская' },
]

export function Dashboard() {
  const [screen, setScreen] = useState<Screen>('overview')

  return (
    <main className="mx-auto max-w-3xl px-6 py-16">
      <h1 className="text-3xl font-semibold tracking-tight text-slate-900">
        Who wants an offer?
      </h1>

      <nav className="mt-6 flex gap-2 border-b border-slate-200">
        {TABS.map((tab) => (
          <button
            key={tab.id}
            type="button"
            onClick={() => { setScreen(tab.id); }}
            className={`-mb-px border-b-2 px-3 py-2 text-sm ${
              screen === tab.id
                ? 'border-slate-900 font-medium text-slate-900'
                : 'border-transparent text-slate-500 hover:text-slate-700'
            }`}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      <div className="mt-8">
        {screen === 'overview' ? (
          <>
            <p className="text-slate-600">
              Каркас готов. Дашборд с вакансиями появится в фазе 7.
            </p>
            <div className="mt-8 rounded-lg border border-slate-200 bg-white p-4">
              <HealthBadge />
            </div>
          </>
        ) : (
          <Workshop />
        )}
      </div>
    </main>
  )
}
