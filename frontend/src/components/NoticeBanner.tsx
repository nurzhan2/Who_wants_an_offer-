import { useNotices } from '@/hooks/useOperations'
import { date } from '@/lib/format'
import type { Notice } from '@/types/notices'

/**
 * What the owner must know on every screen: today, hh's resume-visibility line.
 *
 * It is about the resume, not about a vacancy, so it hangs above every page
 * rather than surfacing at the moment of sending. The active notice is the one
 * inverted surface in the header — the system's only way to be loud. Once the
 * sends stop carrying hh's sentence the banner says so in plain weight instead
 * of vanishing, because silence would read the same as "never mind".
 */
export function NoticeBanner() {
  const { data } = useNotices()
  if (!data || data.items.length === 0) return null
  return (
    <div className="rise">
      {data.items.map((notice) => (
        <Banner key={notice.kind} notice={notice} />
      ))}
    </div>
  )
}

function Banner({ notice }: { notice: Notice }) {
  return (
    <div
      role={notice.resolved ? 'status' : 'alert'}
      className={notice.resolved ? 'border-b border-hairline' : 'invert-surface'}
    >
      <div className="mx-auto max-w-shell px-6 py-5">
        <p className="font-semibold">{notice.title}</p>
        {notice.quote ? (
          <blockquote className="break-anywhere mt-2 border-l border-current pl-4 text-small">
            hh: «{notice.quote}»
          </blockquote>
        ) : null}
        <p className="mt-2 max-w-3xl text-small opacity-80">{notice.body}</p>
        <p className="mt-2 text-small opacity-60">
          Встречалось в {notice.applications} откликах, с {date(notice.first_seen_at)} по{' '}
          {date(notice.last_seen_at)}.
        </p>
      </div>
    </div>
  )
}
