import { useState } from 'react'

import { Dashboard } from '@/pages/Dashboard'
import { MyData } from '@/pages/MyData'
import { Workshop } from '@/pages/Workshop'

/**
 * The application shell.
 *
 * A `useState` rather than a router: there are three screens, and adding a
 * routing dependency to switch between them would be a dependency the product
 * does not need yet. When the dashboard grows deep links — a vacancy, a match —
 * that is the moment to bring one in, and this is a component to replace rather
 * than a pattern to spread.
 *
 * `max-w-shell` is the 1078px measure the design system lays everything out on.
 */
const SCREENS = [
  { id: 'overview', title: 'Обзор' },
  { id: 'my-data', title: 'Мои данные' },
  { id: 'workshop', title: 'Мастерская' },
] as const

type ScreenId = (typeof SCREENS)[number]['id']

export function App() {
  const [screen, setScreen] = useState<ScreenId>('overview')

  return (
    <div className="min-h-screen bg-paper text-ink">
      <div className="mx-auto flex max-w-shell flex-col gap-12 px-6 py-16">
        <header className="flex flex-col gap-8">
          <h1 className="text-3xl tracking-tight">Who wants an offer?</h1>
          <nav className="flex gap-3">
            {SCREENS.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-current={screen === item.id ? 'page' : undefined}
                onClick={() => {
                  setScreen(item.id)
                }}
                className={
                  screen === item.id
                    ? 'rounded-pill bg-ink px-6 py-2 text-label uppercase text-paper'
                    : 'rounded-pill border border-line px-6 py-2 text-label uppercase text-muted transition-colors hover:border-ink hover:text-ink'
                }
              >
                {item.title}
              </button>
            ))}
          </nav>
        </header>

        <main>
          {screen === 'overview' && <Dashboard />}
          {screen === 'my-data' && <MyData />}
          {screen === 'workshop' && <Workshop />}
        </main>
      </div>
    </div>
  )
}
