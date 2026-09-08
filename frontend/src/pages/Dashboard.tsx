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
    <div className="flex flex-col gap-8">
      <p className="max-w-2xl text-muted">
        Каркас готов. Дашборд с вакансиями появится в фазе 7.
      </p>
      <div className="rounded-field border border-line bg-card p-4">
        <HealthBadge />
      </div>

      <section className="flex flex-col gap-4">
        <h2 className="text-lg">Документы под вакансию</h2>
        <p className="max-w-2xl text-muted">
          Резюме и сопроводительное собираются под конкретную вакансию из твоего профиля.
          Ничего не придумывается: каждый факт в документе есть в профиле. Отправляет отклик
          человек, из CLI — здесь только генерация.
        </p>
        <DocumentsSection />
      </section>
    </div>
  )
}
