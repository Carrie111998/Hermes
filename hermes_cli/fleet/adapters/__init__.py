"""Fleet adapter implementations."""

from .external_cli import ExternalCliAdapter
from .native_provider import NativeProviderAdapter

__all__ = ["ExternalCliAdapter", "NativeProviderAdapter"]
