"""Send Email Tool — send emails via AgentMail or SMTP.

Uses the AgentMail Python SDK when AGENTMAIL_API_KEY is configured.
Falls back to SMTP (EMAIL_* env vars) if AgentMail is not available.
Secrets are resolved in order: env vars → Hermes secret scope → macOS Keychain.
"""

import json
import logging
import os
import subprocess
import sys

from tools.registry import registry

logger = logging.getLogger(__name__)


def _read_macos_keychain(service: str, account: str = None) -> str:
    """Read a password from the macOS Keychain via the security command.

    Returns an empty string if the entry is not found or the security command
    is unavailable (non-macOS). This is a best-effort fallback layer — it
    never raises.
    """
    if not sys.platform == "darwin":
        return ""
    try:
        account = account or os.environ.get("USER", os.environ.get("LOGNAME", ""))
        cmd = [
            "security", "find-generic-password",
            "-a", account,
            "-s", service,
            "-w",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.debug("macOS keychain lookup for %s failed: %s", service, e)
    return ""


def _get_secret(name: str, default: str = "") -> str:
    """Resolve a secret from multiple sources.

    Resolution order:
    1. Environment variables (loaded from .env at startup or injected by the platform)
    2. Hermes secret scope (for multiplexed gateway profile isolation)
    3. macOS Keychain (fallback — look up by the env-var name as the service name)

    macOS Keychain integration is automatic on macOS. To store a key:
        security add-generic-password -a "$USER" -s "YOUR_KEY_NAME" -w "your_secret_value"

    To retrieve:
        security find-generic-password -a "$USER" -s "YOUR_KEY_NAME" -w
    """
    # 1. Environment variable
    val = os.getenv(name, "").strip()
    if val:
        return val

    # 2. Hermes secret scope (profile-scoped in multiplex mode)
    try:
        from agent.secret_scope import get_secret
        val = get_secret(name, "")
        if val:
            return val.strip()
    except Exception:
        pass

    # 3. macOS Keychain fallback (macOS only)
    if sys.platform == "darwin":
        val = _read_macos_keychain(name, os.environ.get("USER", ""))
        if val:
            return val.strip()
        # Also try common alternative names
        alt_names = []
        if name == "AGENTMAIL_API_KEY":
            alt_names = ["AgentMail API Key"]
        for alt in alt_names:
            val = _read_macos_keychain(alt, os.environ.get("USER", ""))
            if val:
                return val.strip()

    return default


def check_email_requirements() -> bool:
    """Check if email sending capabilities are available.

    Returns True if either AgentMail API key or SMTP credentials are configured.
    Checks environment variables, Hermes secret scope, and macOS Keychain.
    """
    # Check for AgentMail
    agentmail_key = _get_secret("AGENTMAIL_API_KEY", "")
    if agentmail_key:
        return True
    # Check for SMTP credentials
    smtp_host = _get_secret("EMAIL_SMTP_HOST", "")
    smtp_user = _get_secret("EMAIL_ADDRESS", "")
    smtp_pass = _get_secret("EMAIL_PASSWORD", "")
    return bool(smtp_host and smtp_user and smtp_pass)


def _send_via_agentmail(to: str, subject: str, text: str, inbox_id: str = None, cc: str = "", bcc: str = "", html: str = "") -> dict:
    """Send an email using the AgentMail SDK."""
    try:
        from agentmail import AgentMail
    except ImportError:
        # Install on demand
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "agentmail", "-q"],
                capture_output=True
            )
        except Exception:
            pass
        try:
            from agentmail import AgentMail
        except ImportError:
            # Try uv as fallback
            venv_python = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "venv", "bin", "python"
            )
            if os.path.exists(venv_python):
                subprocess.run(
                    ["uv", "pip", "install", "agentmail", "-q"],
                    capture_output=True
                )
            from agentmail import AgentMail

    api_key = _get_secret("AGENTMAIL_API_KEY", "")
    if not api_key:
        return {"error": "AGENTMAIL_API_KEY not configured"}

    client = AgentMail(api_key=api_key)

    # Use provided inbox_id, configured inbox, or default from config
    if not inbox_id:
        inbox_id = _get_secret("AGENTMAIL_INBOX_ID", "")

    if not inbox_id:
        # Try to get from config
        inbox_id = os.getenv("AGENTMAIL_INBOX_ID", "")

    if not inbox_id:
        # Fall back to the email.inbox config value
        try:
            from hermes_cli.config import load_config
            config = load_config()
            inbox_id = (config.get("email", {}) or {}).get("inbox", "")
        except Exception:
            pass

    if not inbox_id:
        # Last resort: list inboxes and use the first one
        try:
            inboxes = client.inboxes.list()
            if inboxes and hasattr(inboxes, 'data') and inboxes.data:
                inbox_id = inboxes.data[0].inbox_id
            elif inboxes and hasattr(inboxes, 'inboxes') and len(inboxes.inboxes) > 0:
                inbox_id = inboxes.inboxes[0].inbox_id
        except Exception as e:
            return {"error": f"Could not determine inbox_id: {e}"}

    if not inbox_id:
        return {"error": "No AGENTMAIL_INBOX_ID configured and no inboxes found"}

    result = client.inboxes.messages.send(
        inbox_id,
        to=to,
        subject=subject,
        text=text,
        cc=cc if cc else None,
        bcc=bcc if bcc else None,
        html=html if html else None,
    )

    return {
        "success": True,
        "message_id": result.message_id,
        "thread_id": result.thread_id,
        "provider": "agentmail",
        "from": inbox_id,
    }


def _send_via_smtp(to: str, subject: str, text: str, cc: str = "", bcc: str = "", html: str = "") -> dict:
    """Send an email using SMTP (fallback method)."""
    import smtplib
    import ssl
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart

    smtp_host = _get_secret("EMAIL_SMTP_HOST", "")
    smtp_port = int(_get_secret("EMAIL_SMTP_PORT", "587"))
    smtp_user = _get_secret("EMAIL_ADDRESS", "")
    smtp_pass = _get_secret("EMAIL_PASSWORD", "")

    if not all([smtp_host, smtp_user, smtp_pass]):
        return {"error": "SMTP credentials not fully configured"}

    msg = MIMEMultipart()
    msg["From"] = smtp_user
    msg["To"] = to
    msg["Subject"] = subject
    if cc:
        msg["Cc"] = cc
    if bcc:
        msg["Bcc"] = bcc
    msg.attach(MIMEText(text, "plain"))
    if html:
        from email.mime.text import MIMEText as MIMETextHTML
        msg.attach(MIMETextHTML(html, "html"))

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls(context=context)
            server.login(smtp_user, smtp_pass)
            server.send_message(msg)
        return {
            "success": True,
            "from": smtp_user,
            "provider": "smtp",
        }
    except Exception as e:
        return {"error": str(e)}


def email_send_tool(args, **kwargs):
    """Send an email to a recipient."""
    to = args.get("to", "")
    subject = args.get("subject", "No Subject")
    text = args.get("text", "")
    inbox_id = args.get("inbox_id", "")
    cc = args.get("cc", "")
    bcc = args.get("bcc", "")
    html = args.get("html", "")

    if not to:
        return json.dumps({"error": "'to' is required"})

    if not text:
        return json.dumps({"error": "'text' (email body) is required"})

    # Try AgentMail first, fall back to SMTP
    agentmail_key = _get_secret("AGENTMAIL_API_KEY", "")

    if agentmail_key:
        result = _send_via_agentmail(to, subject, text, inbox_id, cc=cc, bcc=bcc, html=html)
    else:
        result = _send_via_smtp(to, subject, text, cc=cc, bcc=bcc, html=html)

    return json.dumps(result)


EMAIL_SEND_SCHEMA = {
    "name": "email_send",
    "description": (
        "Send an email to a recipient. Uses AgentMail when AGENTMAIL_API_KEY is "
        "configured (agent-owned inbox like christopher-1550@agentmail.to), or "
        "falls back to SMTP using EMAIL_SMTP_HOST/EMAIL_ADDRESS/EMAIL_PASSWORD. "
        "Secrets are resolved from environment variables, Hermes secret scope, "
        "or macOS Keychain. When using AgentMail, the inbox_id parameter selects "
        "which agent inbox to send from (defaults to your configured inbox)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address (e.g. 'user@example.com')"
            },
            "subject": {
                "type": "string",
                "description": "Email subject line"
            },
            "text": {
                "type": "string",
                "description": "Plain text email body content"
            },
            "inbox_id": {
                "type": "string",
                "description": "AgentMail inbox identifier to send from (AgentMail only). Defaults to your configured inbox."
            },
            "cc": {
                "type": "string",
                "description": "Carbon copy recipient email address"
            },
            "bcc": {
                "type": "string",
                "description": "Blind carbon copy recipient email address"
            },
            "html": {
                "type": "string",
                "description": "HTML email body content (optional). If omitted, the plain text version is sent."
            }
        },
        "required": ["to", "text"]
    }
}

registry.register(
    name="email_send",
    toolset="email",
    schema=EMAIL_SEND_SCHEMA,
    handler=email_send_tool,
    check_fn=check_email_requirements,
    requires_env=[],
    emoji="📧",
)
