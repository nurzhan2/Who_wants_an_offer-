import type { ChangeEvent } from 'react'

interface FieldProps {
  id: string
  label: string
  value: string
  onChange: (value: string) => void
  /** Shown under the input: what the field is for, or where its value came from. */
  hint?: string | undefined
  placeholder?: string | undefined
  type?: 'text' | 'email' | 'tel' | 'url' | undefined
  autoComplete?: string | undefined
}

/**
 * One labelled input.
 *
 * Square corners and a hairline border, per the design system: an input is a
 * slot on the page. The focus state darkens the rule rather than adding a ring,
 * because nothing in this product casts a shadow or glows.
 */
export function Field({
  id,
  label,
  value,
  onChange,
  hint,
  placeholder,
  type = 'text',
  autoComplete,
}: FieldProps) {
  return (
    <div className="flex flex-col gap-2">
      <label htmlFor={id} className="text-label uppercase text-muted">
        {label}
      </label>
      <input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        autoComplete={autoComplete}
        onChange={(event: ChangeEvent<HTMLInputElement>) => {
          onChange(event.target.value)
        }}
        className="w-full rounded-field border border-line bg-card px-4 py-3 text-ink outline-none transition-colors placeholder:text-muted/60 focus:border-ink"
      />
      {hint !== undefined && <p className="text-xs text-muted">{hint}</p>}
    </div>
  )
}
