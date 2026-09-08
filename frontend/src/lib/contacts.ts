import type { ContactLink, ProfileContact, ProfileContactUpdate } from '@/types/contact'

/** One link while it is being edited: no id yet, and every field a string. */
export interface LinkDraft {
  /** Stable across renders so React does not re-key a row the user is typing in. */
  key: string
  kind: string
  url: string
  label: string
}

/** The form's state. Inputs deal in strings; the absence of a value is `''`. */
export interface ContactDraft {
  full_name: string
  phone: string
  email: string
  city: string
  links: LinkDraft[]
}

let nextKey = 0

export function newLinkDraft(kind = 'website'): LinkDraft {
  nextKey += 1
  return { key: `new-${String(nextKey)}`, kind, url: '', label: '' }
}

function fromLink(link: ContactLink): LinkDraft {
  return { key: link.id, kind: link.kind, url: link.url, label: link.label ?? '' }
}

export function toDraft(contact: ProfileContact): ContactDraft {
  return {
    full_name: contact.full_name ?? '',
    phone: contact.phone ?? '',
    email: contact.email ?? '',
    city: contact.city ?? '',
    links: contact.links.map(fromLink),
  }
}

/** An input the user left blank and a value the server has never had are the same. */
function same(current: string, stored: string | null): boolean {
  return current.trim() === (stored ?? '').trim()
}

function linksChanged(draft: LinkDraft[], stored: ContactLink[]): boolean {
  const kept = draft.filter((link) => link.url.trim() !== '')
  if (kept.length !== stored.length) {
    return true
  }
  return kept.some((link, index) => {
    const before = stored[index]
    return (
      before === undefined ||
      link.url.trim() !== before.url ||
      link.kind.trim() !== before.kind ||
      link.label.trim() !== (before.label ?? '')
    )
  })
}

/**
 * What actually changed, and nothing else.
 *
 * This is the load-bearing part of the screen rather than an optimisation.
 * Every field named in a PATCH is recorded as settled by a human and is never
 * written by the resume parser again — so a form that submitted the whole block
 * would freeze all four fields the first time someone corrected one of them,
 * and the next CV they uploaded would silently stop updating any of them.
 *
 * A field the user emptied is sent as `null`: that is a decision ("there is no
 * phone number on my CV") and has to be remembered as one, or the next upload
 * would put the extracted number back.
 */
export function diffContacts(stored: ProfileContact, draft: ContactDraft): ProfileContactUpdate {
  const changes: ProfileContactUpdate = {}

  if (!same(draft.full_name, stored.full_name)) {
    changes.full_name = draft.full_name.trim() || null
  }
  if (!same(draft.phone, stored.phone)) {
    changes.phone = draft.phone.trim() || null
  }
  if (!same(draft.email, stored.email)) {
    changes.email = draft.email.trim() || null
  }
  if (!same(draft.city, stored.city)) {
    changes.city = draft.city.trim() || null
  }
  if (linksChanged(draft.links, stored.links)) {
    changes.links = draft.links
      .filter((link) => link.url.trim() !== '')
      .map((link) => ({
        kind: link.kind.trim() || 'website',
        url: link.url.trim(),
        label: link.label.trim() || null,
      }))
  }

  return changes
}

export function hasChanges(changes: ProfileContactUpdate): boolean {
  return Object.keys(changes).length > 0
}
