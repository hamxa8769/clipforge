"""ClipForge auth — OAuth channels, encrypted token storage, app settings."""

from __future__ import annotations

from clipforge.auth.channels import all_channels, channel_status
from clipforge.auth.connect_guide import connect_guide
from clipforge.auth.oauth import OAuthError, StateStore
from clipforge.auth.providers import PLATFORMS, PROVIDERS, get_provider, redirect_uri
from clipforge.auth.secrets import SecretStore, mask_secret
from clipforge.auth.settings_store import AppSettings, SettingsStore
from clipforge.auth.token_store import TokenRecord, TokenStore

__all__ = [
    "AppSettings",
    "OAuthError",
    "PLATFORMS",
    "PROVIDERS",
    "SecretStore",
    "SettingsStore",
    "StateStore",
    "TokenRecord",
    "TokenStore",
    "all_channels",
    "channel_status",
    "connect_guide",
    "get_provider",
    "mask_secret",
    "redirect_uri",
]
