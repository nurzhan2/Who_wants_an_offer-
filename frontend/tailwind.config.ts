import type { Config } from 'tailwindcss'

/**
 * The monopo saigon design system, as far as this app uses it.
 *
 * Four rules, and they are all subtractive: the palette is monochrome, input
 * fields have square corners, buttons are full pills, and nothing casts a
 * shadow. Depth comes from a hairline rule and from space, never from a blur.
 *
 * They live here as tokens rather than as literals in components so that the
 * one place to argue about a value is this file. `1078px` is the content
 * measure the whole product is laid out on.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        // Not pure #000/#fff: paper is warmed slightly and ink stopped short of
        // black, which is what keeps a monochrome page from looking like a
        // rendering error.
        paper: '#f4f4f2',
        card: '#ffffff',
        ink: '#111111',
        muted: '#6b6b6b',
        line: '#d8d8d4',
        // The one non-grey, and it is only ever a failure state.
        alarm: '#8a1f11',
      },
      maxWidth: {
        shell: '1078px',
      },
      borderRadius: {
        // Square. An input is a slot on a page, not a lozenge.
        field: '0px',
        // A pill, at any height this app uses.
        pill: '75px',
      },
      fontSize: {
        label: ['0.6875rem', { lineHeight: '1rem', letterSpacing: '0.12em' }],
      },
    },
  },
  plugins: [],
} satisfies Config
