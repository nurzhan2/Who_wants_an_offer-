import { useSyncExternalStore } from 'react'
import { flushSync } from 'react-dom'

import { parseHash, type Location } from '@/app/routes'
import { stillness } from '@/lib/motion'

/**
 * A change of screen, handed to the browser as a view transition: it keeps a
 * picture of the page it had, renders the new one inside `flushSync`, and
 * cross-fades the two (the timing is in `index.css`). Where the browser has no
 * view transitions, or the person asked for no motion, the screen changes
 * between two frames as it always did.
 */
function subscribe(onChange: () => void): () => void {
  const change = (): void => {
    if (typeof document.startViewTransition !== 'function' || stillness()) {
      onChange()
      return
    }
    document.startViewTransition(() => {
      flushSync(onChange)
    })
  }
  window.addEventListener('hashchange', change)
  return () => {
    window.removeEventListener('hashchange', change)
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
