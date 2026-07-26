"""Owner-approval channel adapters."""

from hermes_cli.lanes.channels.dashboard import DashboardApprovalChannel
from hermes_cli.lanes.channels.telegram import TelegramApprovalChannel

__all__ = ["DashboardApprovalChannel", "TelegramApprovalChannel"]
