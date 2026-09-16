import type { ReactNode } from 'react'

import { href, ROUTES, type RouteName } from '@/app/routes'
import { NoticeBanner } from '@/components/NoticeBanner'
import { useHealth } from '@/hooks/useHealth'
import { PARSE_STATUS } from '@/lib/labels'
import type { ProfileBrief } from '@/types/api'

/**
 * The frame: one column, 1078px, centred, and a row of screens across the top.
 *
 * The header carries the two facts that decide whether anything below it can be
 * trusted — whose resume this is drawn for, and whether the backend is
 * answering — because every number on every screen is a claim about one
 * database read through one API, and a page that renders stale figures with no
 * sign of the connection being gone is the failure mode a dashboard has.
 */
export function Shell({
  route,
  profile,
  children,
}: {
  route: RouteName
  profile: ProfileBrief | null
  children: ReactNode
}) {
  return (
    <div className="min-h-screen bg-paper text-ink">
      <header className="border-b border-hairline">
        <div className="mx-auto flex max-w-shell flex-wrap items-baseline justify-between gap-4 px-6 py-6">
          <a href={href('overview')} className="text-small font-semibold uppercase tracking-widest">
            Who wants an offer?
          </a>
          <Connection />
        </div>
        <nav className="mx-auto max-w-shell px-6" aria-label="Разделы">
          <ul className="-mx-3 flex flex-wrap">
            {(Object.keys(ROUTES) as RouteName[]).map((name) => (
              <li key={name}>
                <a
                  href={href(name)}
                  aria-current={name === route ? 'page' : undefined}
                  className={`inline-block px-3 py-4 text-small transition-colors duration-800 ease-slow ${
                    name === route ? 'font-semibold' : 'text-muted hover:text-ink'
                  }`}
                >
                  {ROUTES[name]}
                </a>
              </li>
            ))}
          </ul>
        </nav>
      </header>
      <NoticeBanner />

      <main key={route} className="rise mx-auto max-w-shell px-6 py-section">
        <Owner profile={profile} />
        {children}
      </main>

      <footer className="mx-auto max-w-shell px-6 pb-section text-small text-muted">
        <p className="border-t border-hairline pt-6">
          Отклик уходит только после вашего подтверждения в карточке вакансии: письмо, ссылка и
          оценка показываются целиком, и отправляет их агент на вашем компьютере — ровно то, что вы
          подтвердили.
        </p>
      </footer>
    </div>
  )
}

/**
 * Whose resume everything below is scored against.
 *
 * Name and headline and nothing else. The resume holds contact details; they
 * belong on the screen that is about the resume, not in a header that every
 * other screen inherits — and nothing here is logged or measured anywhere.
 */
function Owner({ profile }: { profile: ProfileBrief | null }) {
  if (profile === null) {
    return (
      <p className="mb-section text-small text-muted">
        Активного резюме нет. Загрузите его, чтобы вакансии получили оценку.
      </p>
    )
  }
  return (
    <div className="mb-section flex flex-wrap items-baseline gap-x-4 gap-y-1 text-small">
      <span className="font-semibold">{profile.name ?? 'Резюме без имени'}</span>
      {profile.headline ? <span className="text-muted">{profile.headline}</span> : null}
      <span className="text-muted">
        {profile.skills} навыков · {PARSE_STATUS[profile.parse_status]}
      </span>
    </div>
  )
}

/**
 * Whether the backend is answering.
 *
 * Encoded in the word and in the weight, not in a dot's colour: this system has
 * no chromatic values at all, and a grey dot beside "деградация" would carry
 * exactly as much information as the word on its own.
 */
function Connection() {
  const { data, isPending, isError } = useHealth()

  if (isPending) return <span className="text-small text-muted">проверяем бэкенд…</span>
  if (isError) return <span className="text-small font-semibold">бэкенд недоступен</span>

  return (
    <span className="text-small text-muted">
      {data.status === 'ok' ? 'бэкенд отвечает' : <strong className="font-semibold">деградация</strong>}
      {' · '}
      {data.version} · {data.environment}
    </span>
  )
}
