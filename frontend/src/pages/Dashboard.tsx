import { DocumentsSection } from '@/components/DocumentsSection'
import { HealthBadge } from '@/components/HealthBadge'

/**
 * The dashboard as it stands: the health badge, and the documents section.
 *
 * The full set of screens — обзор, вакансии, отклики, документы, мои данные,
 * мастерская — is `prompts/11-dashboard.md`'s, along with the routing that
 * connects them. This page holds the one section that exists so far, laid out
 * to the monopo saigon grid the rest will use: 1078px centred, 46px between
 * sections, monochrome, no shadows.
 */
export function Dashboard() {
  return (
    <main className="mx-auto max-w-canvas px-6 py-section font-sans text-obsidian">
      <header className="flex flex-wrap items-baseline justify-between gap-element">
        <h1 className="text-subheading font-light tracking-tight">Who wants an offer?</h1>
        <HealthBadge />
      </header>

      <section className="mt-section">
        <h2 className="text-body font-semibold">Документы под вакансию</h2>
        <p className="mt-2 text-body-sm text-felt-gray">
          Резюме и сопроводительное собираются под конкретную вакансию из твоего профиля.
          Ничего не придумывается: каждый факт в документе есть в профиле. Отправляет отклик
          человек, из CLI — здесь только генерация.
        </p>
        <div className="mt-element">
          <DocumentsSection />
        </div>
      </section>
    </main>
  )
}
