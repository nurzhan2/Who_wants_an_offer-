import { useSyncExternalStore } from 'react'

import { parseHash, type Location } from '@/app/routes'

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => {
    window.removeEventListener('hashchange', onChange)
  }
}

function snapshot(): string {
  return window.location.hash
}

/**
 * The current screen, from the URL fragment.
 *
 * `useSyncExternalStore` rather than `useState` + an effect: the hash is state
 * that lives outside React and can change before the first paint — somebody
 * opening a copied link — and the effect version renders the default screen
 * first and corrects itself, which shows as a flash of the wrong page on every
 * deep link.
 *
 * The parse happens on the value, not in the snapshot: the store must return a
 * stable reference for an unchanged hash, and a fresh object every call is an
 * infinite render loop.
 */
export function useLocation(): Location {
  const hash = useSyncExternalStore(subscribe, snapshot, () => '')
  return parseHash(hash)
}
