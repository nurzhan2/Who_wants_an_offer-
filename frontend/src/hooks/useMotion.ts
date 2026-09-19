import { useEffect, useLayoutEffect, useRef, useState, type RefObject } from 'react'

import { EASE, nearView, stillness, TRAVEL_MS } from '@/lib/motion'

/**
 * A list that re-sorts instead of being replaced.
 *
 * FLIP, by hand: the rows' offsets are remembered after every commit, and when
 * the order changes each row that survived is drawn back at its old place and
 * slides to the new one. Only `transform` moves, through the Web Animations API,
 * so nothing is laid out twice. A row new to the list is not moved here — it
 * arrives through its own entrance — and a row far off screen is not animated at
 * all, which is what keeps a list of two thousand rows scrolling while it sorts.
 *
 * Children opt in with `data-flip` holding their key; the container must be the
 * offset parent (`relative`), so the offsets are the rows' own and not the
 * page's, and scrolling between two renders does not read as movement.
 */
export function useReorder(container: RefObject<HTMLElement | null>, order: string): void {
  const offsets = useRef(new Map<string, number>())

  useLayoutEffect(() => {
    const element = container.current
    if (!element) return
    const rows = Array.from(element.children).filter(
      (child): child is HTMLElement => child instanceof HTMLElement && child.dataset.flip !== undefined,
    )
    const before = offsets.current
    const after = new Map<string, number>()
    for (const row of rows) after.set(row.dataset.flip ?? '', row.offsetTop)
    offsets.current = after
    if (before.size === 0 || stillness()) return

    for (const row of rows) {
      const was = before.get(row.dataset.flip ?? '')
      const now = after.get(row.dataset.flip ?? '')
      if (was === undefined || now === undefined || was === now) continue
      if (!nearView(row)) continue
      row.animate([{ transform: `translateY(${String(was - now)}px)` }, { transform: 'none' }], {
        duration: TRAVEL_MS,
        easing: EASE,
      })
    }
  }, [container, order])
}

/**
 * True for a moment after `stamp` changes to a new non-zero value.
 *
 * A button's «готово»: the mutation's `submittedAt` once it has succeeded, so a
 * second success flashes again rather than being swallowed by the first.
 */
export function useFlash(stamp: number, ms = 2400): boolean {
  const [shown, setShown] = useState(0)
  useEffect(() => {
    if (stamp === 0) return
    setShown(stamp)
    const timer = window.setTimeout(() => {
      setShown(0)
    }, ms)
    return () => {
      window.clearTimeout(timer)
    }
  }, [stamp, ms])
  return stamp !== 0 && shown === stamp
}

/**
 * True for a moment after `busy` falls back to false with `ok` set: something
 * this screen watched running has just finished well. A step that was already
 * finished when the page opened does not flash.
 */
export function useCompletion(busy: boolean, ok: boolean): boolean {
  const was = useRef(busy)
  const [stamp, setStamp] = useState(0)
  useEffect(() => {
    if (was.current && !busy && ok) setStamp(Date.now())
    was.current = busy
  }, [busy, ok])
  return useFlash(stamp)
}

/** The clock, ticking once a second while `active` — for a live elapsed time. */
export function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    if (!active) return
    const timer = window.setInterval(() => {
      setNow(Date.now())
    }, 1000)
    return () => {
      window.clearInterval(timer)
    }
  }, [active])
  return now
}
