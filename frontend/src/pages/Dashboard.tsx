import { Shell } from '@/components/Shell'
import { useOverview } from '@/hooks/queries'
import { useLocation } from '@/hooks/useLocation'
import { Applications } from '@/pages/Applications'
import { Documents } from '@/pages/Documents'
import { MyData } from '@/pages/MyData'
import { Overview } from '@/pages/Overview'
import { Vacancies } from '@/pages/Vacancies'
import { Workshop } from '@/pages/Workshop'

/**
 * Which screen is on, and the one thing every screen shares.
 *
 * The overview query lives here because the header needs the active profile and
 * so does the first screen; asking twice would let the header say one thing
 * while the page below it says another. Every other screen fetches its own
 * data — they are read separately and nothing compares them.
 */
export function Dashboard() {
  const { route, id } = useLocation()
  const overview = useOverview()

  return (
    // undefined until the overview has answered: a header must not say «резюме
    // нет» while the only thing known is that the server has not replied.
    <Shell route={route} profile={overview.isSuccess ? overview.data.profile : undefined}>
      {route === 'overview' ? <Overview query={overview} /> : null}
      {route === 'vacancies' ? <Vacancies selected={id} /> : null}
      {route === 'applications' ? <Applications /> : null}
      {route === 'documents' ? <Documents /> : null}
      {route === 'workshop' ? <Workshop /> : null}
      {route === 'profile' ? <MyData /> : null}
    </Shell>
  )
}
