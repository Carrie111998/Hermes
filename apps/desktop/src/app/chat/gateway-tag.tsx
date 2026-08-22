import { useStore } from '@nanostores/react'

import { Tip } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { $connectionLabels, $multiGateway, $primaryConnectionId } from '@/store/gateway-separation'

/** Owning-gateway chip.
 *
 *  Two machines can each serve a profile called `default`, and upstream draws
 *  both as bare rows, so a Dell session is indistinguishable from an HP one.
 *  This chip names the machine a row came from. It renders only when more than
 *  one connection is registered, so single-gateway sidebars are untouched. */
export function GatewayTag({
  className,
  connectionId
}: {
  className?: string
  connectionId: null | string | undefined
}) {
  const multi = useStore($multiGateway)
  const labels = useStore($connectionLabels)
  const primary = useStore($primaryConnectionId)
  const id = (connectionId || '').trim() || primary
  const label = labels[id]

  if (!multi || !label) {
    return null
  }

  // Long device names would eat the row, so the chip shows a short form and the
  // tooltip carries the full name. Prefer a trailing parenthetical, because
  // that is usually where the DISCRIMINATOR lives: "Mecha Hermes (HP)" and
  // "MechaHome Hermes (Dell)" are near-identical until you reach the bracket.
  const bracket = /\(([^)]{1,12})\)\s*$/.exec(label)
  const short = bracket ? bracket[1].trim() : label

  return (
    <Tip label={label}>
      <span
        aria-label={label}
        className={cn(
          'shrink-0 rounded px-1 py-px text-[10px] leading-none font-medium',
          'bg-muted text-muted-foreground/80 max-w-[7rem] truncate',
          className
        )}
        role="img"
      >
        {short}
      </span>
    </Tip>
  )
}
