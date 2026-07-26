"""Email provider adapters (PRODUCT.md §3). base.EmailProvider is the
interface; stub.StubEmailProvider is the credential-free test double.
Gmail/Microsoft adapters land in Sprint 5 with the same interface.
"""
from .base import EmailProvider, OutgoingEmail, SendResult
from .browser import BrowserWebmailProvider
from .gmail import GmailProvider
from .microsoft import MicrosoftProvider
from .smtp import SmtpProvider
from .stub import StubEmailProvider

# provider key -> adapter class. Single source of truth for dispatch.
EMAIL_PROVIDERS = {
    "google": GmailProvider,
    "microsoft": MicrosoftProvider,
    "smtp": SmtpProvider,
    "browser": BrowserWebmailProvider,
    "stub": StubEmailProvider,
}

__all__ = ["EmailProvider", "OutgoingEmail", "SendResult", "StubEmailProvider",
           "GmailProvider", "MicrosoftProvider", "SmtpProvider",
           "BrowserWebmailProvider", "EMAIL_PROVIDERS"]
