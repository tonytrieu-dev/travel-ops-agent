interface FilterSelectOption {
  value: string
  label: string
}

interface FilterSelectProps {
  value: string
  onChange: (value: string) => void
  options: FilterSelectOption[]
  ariaLabel: string
}

export function FilterSelect({ value, onChange, options, ariaLabel }: FilterSelectProps) {
  return (
    <select
      value={value}
      onChange={(event) => onChange(event.target.value)}
      aria-label={ariaLabel}
      className="rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm text-slate-600"
    >
      {options.map((option) => (
        <option key={option.value} value={option.value}>
          {option.label}
        </option>
      ))}
    </select>
  )
}
