/**
 * The contact block, mirroring `app.schemas.contact` on the backend.
 *
 * `edited` is the half of this contract that is easy to mistake for noise. It
 * says which fields a person has settled by hand: those are never rewritten by
 * the parser, and the rest are still filled in from each new resume. The screen
 * shows that difference, because "we read this off your CV" and "you typed
 * this" are not the same claim to make about someone's phone number.
 */
export interface ContactLink {
  id: string
  kind: string
  url: string
  label: string | null
  is_manual: boolean
}

export interface ContactEdits {
  full_name: boolean
  phone: boolean
  email: boolean
  city: boolean
}

export interface ProfileContact {
  profile_id: string
  full_name: string | null
  phone: string | null
  email: string | null
  city: string | null
  edited: ContactEdits
  links: ContactLink[]
  updated_at: string | null
}

/** One link on its way back to the server; the id and the flag are the server's. */
export interface ContactLinkWrite {
  kind: string
  url: string
  label?: string | null
}

/**
 * A correction.
 *
 * Every field is optional and an absent one means "unchanged" — which is why
 * the form sends only what the person actually touched. Sending the whole block
 * would flag every field as hand-edited and stop the next resume from ever
 * updating any of them.
 */
export interface ProfileContactUpdate {
  full_name?: string | null
  phone?: string | null
  email?: string | null
  city?: string | null
  links?: ContactLinkWrite[]
}

/** The subset of the profile this screen needs to find its own id. */
export interface ActiveProfile {
  id: string
  name: string | null
  headline: string | null
  /** Job titles the owner is looking for; empty means "search by skills". */
  target_titles: string[]
}
