import type { Config } from 'tailwindcss'

/**
 * The design system, as tokens rather than as a document.
 *
 * Every rule the brief states is here, and the ones that are absent are absent
 * on purpose. There is no colour scale: the palette is two values and their
 * inversion, so a component that wanted to encode a status in colour has
 * nothing to reach for and has to encode it in position, label and weight
 * instead — which is what the brief asks for and what a screen full of amber
 * badges quietly refuses to do.
 *
 * Likewise the radii. Two values, 0 and 75px, and nothing between them: a
 * `rounded-lg` written out of habit does not compile to anything.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    // Replaced, not extended: an extended palette would keep Tailwind's
    // hundred-odd colours one autocomplete away, and the first `text-red-600`
    // typed in a hurry is the end of a monochrome interface.
    colors: {
      transparent: 'transparent',
      current: 'currentColor',
      ink: 'var(--ink)',
      paper: 'var(--paper)',
      // The only greys, and both are the ink at reduced opacity rather than a
      // third colour: they stay correct when a surface inverts.
      muted: 'var(--muted)',
      hairline: 'var(--hairline)',
      // The design system's own names for the same values, because phase 10
      // wrote its components against them (2026-09-13) and none of them
      // existed here: Tailwind generates nothing for an unknown class, so the
      // «Сгенерировать» button rendered paper-coloured text on no background.
      // Aliases, not new colours — the palette is still two values and their
      // inversion, and a grey here is still the ink at reduced opacity.
      obsidian: 'var(--ink)',
      inkstone: 'var(--ink)',
      'slate-pill': 'var(--ink)',
      'felt-gray': 'var(--muted)',
      pewter: 'var(--muted)',
      'ash-mist': 'var(--hairline)',
    },
    borderRadius: {
      none: '0px',
      // The same 0px under the name the "Мои данные" form was written against.
      // An alias rather than a rename: the system has one square corner and two
      // phases spelled it differently, and renaming across screens buys nothing.
      field: '0px',
      pill: '75px',
    },
    boxShadow: {
      // Nothing casts one. Declared as `none` rather than omitted so that
      // `shadow` remains a valid class that does nothing, instead of silently
      // falling through to Tailwind's default.
      DEFAULT: 'none',
      none: 'none',
    },
    extend: {
      fontFamily: {
        // Roobert first for the machines that have it; Inter is the
        // substitution the brief names, and the rest is the usual ladder down
        // to whatever the system has.
        sans: ['Roobert', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      fontWeight: {
        light: '300',
        normal: '400',
        semibold: '600',
      },
      fontSize: {
        // The display sizes, capped: the brief allows this system's big
        // headings but not above 78px, so 78 is the largest step that exists.
        display: ['78px', { lineHeight: '0.94', letterSpacing: '-0.03em' }],
        title: ['46px', { lineHeight: '1.02', letterSpacing: '-0.02em' }],
        heading: ['26px', { lineHeight: '1.15', letterSpacing: '-0.01em' }],
        body: ['15px', { lineHeight: '1.55' }],
        small: ['13px', { lineHeight: '1.5' }],
        micro: ['11px', { lineHeight: '1.4', letterSpacing: '0.08em' }],
        // `micro` under the name the contacts form uses for the same step.
        label: ['11px', { lineHeight: '1.4', letterSpacing: '0.08em' }],
        // The design system's names, which phase 10 used and nothing defined.
        // Mapped onto the steps above rather than onto the system's own pixel
        // values, so the documents panel reads at the same density as the rest.
        caption: ['12px', { lineHeight: '1.4' }],
        'body-sm': ['13px', { lineHeight: '1.5' }],
        subheading: ['26px', { lineHeight: '1.15', letterSpacing: '-0.01em' }],
      },
      maxWidth: {
        shell: '1078px',
      },
      spacing: {
        section: '46px',
        card: '34px',
        // The system's element gap, used by phase 10 and not defined until now.
        element: '14px',
      },
      transitionTimingFunction: {
        // One easing curve for everything that moves.
        slow: 'cubic-bezier(0.19, 1, 0.22, 1)',
        // The same curve under the name phase 10 used.
        monopo: 'cubic-bezier(0.19, 1, 0.22, 1)',
      },
      transitionDuration: {
        800: '800ms',
        1250: '1250ms',
        slow: '800ms',
      },
    },
  },
  plugins: [],
} satisfies Config
