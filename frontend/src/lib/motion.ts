/**
 * The motion this interface has, as numbers and as one clock.
 *
 * The system allows one curve and slow durations; everything that moves from
 * script reads them from here so that a JavaScript animation and a CSS one are
 * the same motion. Nothing in this file decides *whether* to move — callers ask
 * `stillness()` first, and a person who asked their machine to stop animating
 * gets the final state at once, not a faster version of the animation.
 */

import type { CSSProperties } from 'react'

/** The system's only curve: cubic-bezier(0.19, 1, 0.22, 1). */
export const EASE = 'cubic-bezier(0.19, 1, 0.22, 1)'

/** The system's floor for anything that travels. */
export const TRAVEL_MS = 800

/** A number counting up to its value: short, the brief's half-second ceiling. */
export const COUNT_MS = 480

/** Whether the person asked for no motion. Read on every call: it can change. */
export function stillness(): boolean {
  return typeof window.matchMedia === 'function'
    ? window.matchMedia('(prefers-reduced-motion: reduce)').matches
    : false
}

/**
 * The same curve for a script: exponential ease-out, which is what
 * cubic-bezier(0.19, 1, 0.22, 1) draws to within a pixel.
 */
export function easeOut(t: number): number {
  return t >= 1 ? 1 : 1 - Math.pow(2, -10 * t)
}

interface Tween {
  start: number
  duration: number
  step: (progress: number) => void
}

/*
 * One requestAnimationFrame loop for every counter on the page. A list of fifty
 * scores counting at once is fifty closures called from one frame, not fifty
 * frame loops competing for it.
 */
const live = new Set<Tween>()
let running = false

function frame(now: number): void {
  for (const tween of live) {
    const progress = Math.min(1, (now - tween.start) / tween.duration)
    tween.step(easeOut(progress))
    if (progress >= 1) live.delete(tween)
  }
  if (live.size > 0) {
    window.requestAnimationFrame(frame)
  } else {
    running = false
  }
}

/**
 * Call `step` with an eased 0→1 progress every frame for `duration` ms.
 * Returns the cancel; a cancelled tween leaves whatever it last drew.
 */
export function tween(duration: number, step: (progress: number) => void): () => void {
  const item: Tween = { start: performance.now(), duration, step }
  live.add(item)
  if (!running) {
    running = true
    window.requestAnimationFrame(frame)
  }
  return () => {
    live.delete(item)
  }
}

/**
 * Whether an element is on screen or within one screen of it. Motion further
 * away than that would finish before anybody scrolled to it, so it is skipped
 * rather than paid for — which is what keeps a long list scrolling.
 */
export function nearView(element: Element): boolean {
  const box = element.getBoundingClientRect()
  const height = window.innerHeight
  return box.bottom > -height && box.top < height * 2
}

/** The `style` that tells an entering row its place in the cascade (`.enter`). */
export function cascade(index: number): CSSProperties {
  return { '--i': index } as CSSProperties
}
