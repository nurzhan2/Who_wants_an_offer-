import type { Config } from 'tailwindcss'

/**
 * The monopo saigon design system, as tokens.
 *
 * Chosen by the owner and described in `prompts/hh/hh-final.md`; this file is
 * that description turned into the only vocabulary the components may use.
 * Three of its rules are the ones a dashboard breaks first, so they are worth
 * restating where they are enforced:
 *
 * - **there is no chromatic colour in the interface.** Not for statuses, not for
 *   scores, not for "good" and "bad". Difference is carried by position, by
 *   label and by font weight. The one permitted gradient is a background behind
 *   the overview heading and never a control's fill, so it is not a token here.
 * - **radii are 0px or 75px and nothing between.** Cards, inputs and tables are
 *   square; buttons and tags are full pills.
 * - **there are no shadows anywhere.** Separation is a 1px hairline and an
 *   inverted surface.
 */
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        obsidian: '#000000',
        paper: '#ffffff',
        inkstone: '#181818',
        'felt-gray': '#6d6d6d',
        'slate-pill': '#636363',
        'ash-mist': '#9a9a9a',
        pewter: '#808080',
      },
      fontFamily: {
        // Roobert is the system's face; Inter is the substitution it names, and
        // the stack ends in the platform sans so a machine with neither still
        // renders the intended proportions rather than a serif.
        sans: ['Roobert', 'Inter', 'system-ui', '-apple-system', 'Segoe UI', 'sans-serif'],
      },
      fontSize: {
        caption: ['12px', { lineHeight: '1.19' }],
        'body-sm': ['16px', { lineHeight: '1.15' }],
        body: ['18px', { lineHeight: '1.21' }],
        subheading: ['39px', { lineHeight: '1.19' }],
        'heading-sm': ['54px', { lineHeight: '1.39' }],
        // 78px is the ceiling this dashboard uses: the system goes higher, and
        // the conflict resolution in the brief caps it here because this is a
        // dense data screen rather than an agency showcase.
        heading: ['78px', { lineHeight: '1.10' }],
      },
      borderRadius: {
        // The system has no value between these two.
        none: '0px',
        pill: '75px',
      },
      spacing: {
        // Base unit 4px; these three are the layout's named gaps.
        element: '14px',
        card: '34px',
        section: '46px',
      },
      maxWidth: {
        canvas: '1078px',
      },
      transitionTimingFunction: {
        monopo: 'cubic-bezier(0.19, 1, 0.22, 1)',
      },
      transitionDuration: {
        slow: '800ms',
        slower: '1250ms',
      },
    },
  },
  plugins: [],
} satisfies Config
