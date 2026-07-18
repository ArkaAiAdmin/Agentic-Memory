"""Multi-channel alerting (Slack, email, Pushover)."""

from __future__ import annotations

import json
import logging
import os
import smtplib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


@dataclass
class AlertConfig:
    slack_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    alert_from: str = ""
    alert_to: str = ""
    pushover_user_key: str = ""
    pushover_api_token: str = ""


_config: AlertConfig | None = None


def get_alert_config() -> AlertConfig:
    global _config
    if _config is None:
        _config = AlertConfig(
            slack_webhook_url=os.environ.get("SLACK_WEBHOOK_URL", ""),
            smtp_host=os.environ.get("SMTP_HOST", ""),
            smtp_port=int(os.environ.get("SMTP_PORT", "587")),
            smtp_user=os.environ.get("SMTP_USER", ""),
            smtp_pass=os.environ.get("SMTP_PASS", ""),
            alert_from=os.environ.get("ALERT_FROM", ""),
            alert_to=os.environ.get("ALERT_TO", ""),
            pushover_user_key=os.environ.get("PUSHOVER_USER_KEY", ""),
            pushover_api_token=os.environ.get("PUSHOVER_API_TOKEN", ""),
        )
    return _config


SEVERITY_COLORS: dict[str, str] = {
    "info": "good",
    "warning": "warning",
    "error": "danger",
    "critical": "danger",
}


def _resolve_channels(config: AlertConfig, channel: str) -> list[str]:
    configured: list[str] = []
    if config.slack_webhook_url:
        configured.append("slack")
    if config.smtp_host and config.alert_to:
        configured.append("email")
    if config.pushover_user_key and config.pushover_api_token:
        configured.append("pushover")
    if channel in ("auto", "all"):
        return configured
    if channel in configured:
        return [channel]
    return []


def _alert_slack(webhook_url: str, severity: str, title: str, message: str) -> None:
    color = SEVERITY_COLORS.get(severity, "good")
    payload = {
        "attachments": [
            {
                "color": color,
                "title": title,
                "text": message,
                "fields": [
                    {"title": "Severity", "value": severity, "short": True},
                ],
            }
        ]
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def _alert_email(config: AlertConfig, severity: str, title: str, message: str) -> None:
    html = f"""<html><body>
<h2>{title}</h2>
<p><strong>Severity:</strong> {severity}</p>
<pre>{message}</pre>
</body></html>"""
    msg = MIMEText(html, "html")
    msg["Subject"] = f"[{severity.upper()}] {title}"
    msg["From"] = config.alert_from
    msg["To"] = config.alert_to
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=15) as server:
        if config.smtp_user:
            server.starttls()
            server.login(config.smtp_user, config.smtp_pass)
        server.send_message(msg)


def _alert_pushover(user_key: str, api_token: str, severity: str, title: str, message: str) -> None:
    priority = 1 if severity in ("error", "critical") else 0
    payload = {
        "token": api_token,
        "user": user_key,
        "title": title,
        "message": message,
        "priority": priority,
    }
    data = urllib.parse.urlencode(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json",
        data=data,
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)


def alert(severity: str, title: str, message: str, channel: str = "auto") -> dict[str, list[str]]:
    sent: list[str] = []
    errors: list[str] = []
    config = get_alert_config()
    channels = _resolve_channels(config, channel)
    if not channels:
        logger.warning("No alert channels configured for severity=%s title=%s", severity, title)
        return {"sent": sent, "errors": errors}
    for ch in channels:
        try:
            if ch == "slack":
                _alert_slack(config.slack_webhook_url, severity, title, message)
            elif ch == "email":
                _alert_email(config, severity, title, message)
            elif ch == "pushover":
                _alert_pushover(config.pushover_user_key, config.pushover_api_token, severity, title, message)
            sent.append(ch)
        except Exception as e:
            logger.warning("alert channel %s failed: %s", ch, e)
            errors.append(str(e))
    return {"sent": sent, "errors": errors}
