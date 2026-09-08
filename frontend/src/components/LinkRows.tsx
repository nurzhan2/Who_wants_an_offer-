import type { LinkDraft } from '@/lib/contacts'
import { newLinkDraft } from '@/lib/contacts'

/**
 * The kinds the UI offers. Not a closed list: the input is a free slug with
 * these as suggestions, because the next place someone keeps a profile should
 * not need a release to be storable.
 */
const KNOWN_KINDS = ['github', 'gitlab', 'telegram', 'linkedin', 'website', 'portfolio']

const KIND_LIST_ID = 'contact-link-kinds'

interface LinkRowsProps {
  links: LinkDraft[]
  onChange: (links: LinkDraft[]) => void
}

export function LinkRows({ links, onChange }: LinkRowsProps) {
  function update(key: string, patch: Partial<LinkDraft>) {
    onChange(links.map((link) => (link.key === key ? { ...link, ...patch } : link)))
  }

  function remove(key: string) {
    onChange(links.filter((link) => link.key !== key))
  }

  return (
    <div className="flex flex-col gap-4">
      <datalist id={KIND_LIST_ID}>
        {KNOWN_KINDS.map((kind) => (
          <option key={kind} value={kind} />
        ))}
      </datalist>

      {links.length === 0 && (
        <p className="text-sm text-muted">
          Ссылок пока нет. Мы добавим их сами, когда найдём в резюме.
        </p>
      )}

      {links.map((link) => (
        <div
          key={link.key}
          className="grid grid-cols-1 gap-3 border-b border-hairline pb-4 sm:grid-cols-[9rem_1fr_9rem_auto]"
        >
          <input
            aria-label="Тип ссылки"
            list={KIND_LIST_ID}
            value={link.kind}
            onChange={(event) => {
              update(link.key, { kind: event.target.value })
            }}
            className="rounded-field border border-hairline bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-ink"
          />
          <input
            aria-label="Адрес"
            type="url"
            inputMode="url"
            placeholder="https://"
            value={link.url}
            onChange={(event) => {
              update(link.key, { url: event.target.value })
            }}
            className="rounded-field border border-hairline bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-ink"
          />
          <input
            aria-label="Подпись"
            placeholder="Подпись"
            value={link.label}
            onChange={(event) => {
              update(link.key, { label: event.target.value })
            }}
            className="rounded-field border border-hairline bg-paper px-3 py-2 text-sm text-ink outline-none focus:border-ink"
          />
          <button
            type="button"
            onClick={() => {
              remove(link.key)
            }}
            className="rounded-pill border border-hairline px-4 py-2 text-label uppercase text-muted transition-colors hover:border-ink hover:text-ink"
          >
            Убрать
          </button>
        </div>
      ))}

      <div>
        <button
          type="button"
          onClick={() => {
            onChange([...links, newLinkDraft()])
          }}
          className="rounded-pill border border-ink px-6 py-2 text-label uppercase text-ink transition-colors hover:bg-ink hover:text-paper"
        >
          Добавить ссылку
        </button>
      </div>
    </div>
  )
}
