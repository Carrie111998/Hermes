from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

SCOPES = ("https://www.googleapis.com/auth/gmail.readonly",)


class GmailAuthError(RuntimeError):
    pass


class GmailAuthenticator:
    def __init__(self, credentials_path: str | Path, token_path: str | Path) -> None:
        self.credentials_path = Path(credentials_path).expanduser()
        self.token_path = Path(token_path).expanduser()

    def authorize(self) -> Any:
        if not self.credentials_path.is_file():
            raise GmailAuthError(f"Gmail OAuth credentials missing: {self.credentials_path}")
        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise GmailAuthError("Gmail OAuth dependencies are not installed") from exc

        credentials = None
        if self.token_path.is_file():
            try:
                credentials = Credentials.from_authorized_user_file(str(self.token_path), SCOPES)
            except (ValueError, json.JSONDecodeError) as exc:
                raise GmailAuthError("Gmail OAuth token is invalid") from exc
        if credentials and credentials.expired and credentials.refresh_token:
            try:
                credentials.refresh(Request())
            except Exception as exc:
                raise GmailAuthError("Gmail OAuth token refresh failed") from exc
        if not credentials or not credentials.valid:
            try:
                flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
                # Keep the loopback listener alive indefinitely.  A fixed port
                # also allows a browser on another computer to use a local SSH
                # tunnel (the installed-app OAuth client accepts localhost
                # redirects), while the listener remains bound only to this
                # Ubuntu host's loopback interface.
                credentials = flow.run_local_server(
                    host="localhost",
                    bind_addr="127.0.0.1",
                    port=8765,
                    timeout_seconds=None,
                    open_browser=False,
                )
            except Exception as exc:
                raise GmailAuthError("Gmail OAuth authorization failed") from exc
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(credentials.to_json(), encoding="utf-8")
        os.chmod(self.token_path, 0o600)
        return credentials
