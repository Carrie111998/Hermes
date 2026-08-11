import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Tip } from '@/components/ui/tooltip'

interface NewSessionHeaderButtonProps {
  className?: string
  label: string
  onClick: () => void
}

/** The "+" affordance for starting a new session from a sidebar section header.
 *  Shared by the flat Sessions header and the Home project header (#83479 —
 *  Home used to render only its "back to projects" button, with no way to
 *  start a new session from there). */
export function NewSessionHeaderButton({ className, label, onClick }: NewSessionHeaderButtonProps) {
  return (
    <Tip label={label}>
      <Button
        aria-label={label}
        className={className}
        onClick={event => {
          event.stopPropagation()
          onClick()
        }}
        size="icon-xs"
        variant="ghost"
      >
        <Codicon name="add" size="0.75rem" />
      </Button>
    </Tip>
  )
}
