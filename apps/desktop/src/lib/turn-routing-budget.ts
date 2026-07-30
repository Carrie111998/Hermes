import type { RoutingBudgetStatus } from '@/store/turn-routing'

export function formatRoutingBudget(status: RoutingBudgetStatus): string {
  if (status.cooldownReasonCode) {
    return `Grok cooldown · ${status.cooldownReasonCode}`
  }
  if (status.weeklyLimit === 0) {
    return 'Grok automation disabled (0/week)'
  }
  return `Grok budget ${status.availableSlots}/${status.weeklyLimit} available · ${status.committedSlots} used · ${status.reservedSlots} reserved`
}
