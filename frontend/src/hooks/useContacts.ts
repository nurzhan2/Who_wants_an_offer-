import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { apiGet, apiPatch } from '@/api/client'
import type { ActiveProfile, ProfileContact, ProfileContactUpdate } from '@/types/contact'

/**
 * The profile this browser is looking at.
 *
 * A freshly opened tab knows no id, and every contacts request needs one. v1 is
 * single-user, so the server answers with the live profile; a 404 here means no
 * resume has been parsed yet, which the screen renders rather than treats as an
 * error.
 */
export function useActiveProfile() {
  return useQuery({
    queryKey: ['profile', 'active'],
    queryFn: () => apiGet<ActiveProfile>('/api/v1/profile/active'),
    retry: false,
  })
}

export function contactsKey(profileId: string) {
  return ['profile', profileId, 'contacts'] as const
}

/**
 * The contact block of one profile.
 *
 * Disabled until the id is known, so the throw below is unreachable rather than
 * defensive: it exists because `enabled` is a runtime guarantee the type system
 * does not see, and a non-null assertion would hide the day that changes.
 */
export function useContacts(profileId: string | undefined) {
  return useQuery({
    queryKey: contactsKey(profileId ?? 'none'),
    queryFn: () => apiGet<ProfileContact>(`/api/v1/profile/${requireId(profileId)}/contacts`),
    enabled: profileId !== undefined,
  })
}

function requireId(profileId: string | undefined): string {
  if (profileId === undefined) {
    throw new Error('a contacts request was made before the profile id was known')
  }
  return profileId
}

/**
 * Save a correction.
 *
 * The response is the whole block as it now stands, so it is written straight
 * into the cache: refetching would show the form a value it already has, one
 * round trip later, and give the "saved" state a flicker it has not earned.
 */
export function useSaveContacts(profileId: string | undefined) {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: (changes: ProfileContactUpdate) =>
      apiPatch<ProfileContact>(`/api/v1/profile/${requireId(profileId)}/contacts`, changes),
    onSuccess: (saved) => {
      queryClient.setQueryData(contactsKey(saved.profile_id), saved)
    },
  })
}
