"""Email provider adapters (PRODUCT.md §3). base.EmailProvider is the
interface; stub.StubEmailProvider is the credential-free test double.
Gmail/Microsoft adapters land in Sprint 5 with the same interface.
"""
from .base import EmailProvider, OutgoingEmail, SendResult
from .gmail import GmailProvider
from .microsoft import MicrosoftProvider
from .stub import StubEmailProvider

__all__ = ["EmailProvider", "OutgoingEmail", "SendResult", "StubEmailProvider",
           "GmailProvider", "MicrosoftProvider"]
