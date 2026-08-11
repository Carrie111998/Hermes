import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useTheme } from '@/themes/context'

import { usageDarkStyle } from './dark-style'

export type UsageSelectOption = {
  label: string
  value: string
}

type UsageSelectProps = {
  className?: string
  label: string
  onChange: (value: string) => void
  options: UsageSelectOption[]
  value: string
}

export function UsageSelect({ className, label, onChange, options, value }: UsageSelectProps) {
  const { themeName } = useTheme()

  return (
    <Select onValueChange={onChange} value={value}>
      <SelectTrigger aria-label={label} className={className} size="sm">
        <SelectValue />
      </SelectTrigger>
      <SelectContent className="dark" style={usageDarkStyle(themeName)}>
        {options.map(option => (
          <SelectItem key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  )
}
