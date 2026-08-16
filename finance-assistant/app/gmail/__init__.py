"""Read-only Gmail source adapter for the local ingestion pipeline."""

from .auth import GmailAuthError, GmailAuthenticator, SCOPES
from .client import GmailApiError, GmailClient
from .models import GmailAttachment, GmailBankSummary, GmailMessage, GmailSyncSummary
from .source import GmailConfigError, GmailSource, build_gmail_query, validate_gmail_bank_config

__all__ = [
    "GmailApiError", "GmailAttachment", "GmailAuthError", "GmailAuthenticator",
    "GmailBankSummary", "GmailClient", "GmailConfigError", "GmailMessage",
    "GmailSource", "GmailSyncSummary", "SCOPES", "build_gmail_query",
    "validate_gmail_bank_config",
]
