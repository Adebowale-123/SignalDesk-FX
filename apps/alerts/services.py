"""Telegram and email alerts."""

import html
import logging

import requests
from django.conf import settings
from django.core.mail import send_mail

from apps.core.models import SiteSettings

from .models import AlertLog

log = logging.getLogger("apps.alerts")


def _send_telegram(site, text):
    response = requests.post(f"https://api.telegram.org/bot{site.telegram_bot_token}/sendMessage",
                             json={"chat_id": site.telegram_chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": True}, timeout=15)
    data = response.json()
    if not data.get("ok"):
        raise RuntimeError(data.get("description", "Telegram error"))


def _deliver(subject, text_html, text_plain, signal=None, kind="new"):
    site = SiteSettings.load()
    sent = []
    if site.telegram_bot_token and site.telegram_chat_id:
        try:
            _send_telegram(site, text_html)
            AlertLog.objects.create(signal=signal, channel="telegram", kind=kind, ok=True)
            sent.append("telegram")
        except Exception as exc:  # alerts must never break the analysis
            AlertLog.objects.create(signal=signal, channel="telegram", kind=kind, ok=False, error=str(exc)[:255])
            log.warning("Telegram alert failed: %s", exc)
    if site.email_list:
        try:
            send_mail(subject, text_plain, settings.DEFAULT_FROM_EMAIL, site.email_list)
            AlertLog.objects.create(signal=signal, channel="email", kind=kind, ok=True)
            sent.append("email")
        except Exception as exc:
            AlertLog.objects.create(signal=signal, channel="email", kind=kind, ok=False, error=str(exc)[:255])
            log.warning("Email alert failed: %s", exc)
    return sent


def _levels(signal):
    i = signal.instrument
    conf = f"{signal.confidence}%" if signal.confidence is not None else "not calibrated yet"
    return i, conf


def alert_new_signal(signal):
    i, conf = _levels(signal)
    emoji = "🟢" if signal.direction == "buy" else "🔴"
    why = "\n".join(f"• {r['label']}: {r['value']}" for r in signal.reasons if r.get("tone") != "neutral")
    plain = (f"{i} — {signal.direction.upper()} ({signal.profile})\nConfidence: {conf}\n"
             f"Entry: {i.fmt(signal.entry)}\nStop loss: {i.fmt(signal.stop_loss)}\n"
             f"Take profit: {i.fmt(signal.take_profit)}\nRisk/Reward: 1:{signal.risk_reward:g}\n\nWhy?\n{why}")
    text = (f"{emoji} <b>{html.escape(str(i))} — {signal.direction.upper()}</b>\n"
            f"<i>{html.escape(str(signal.profile))}</i>\n\n"
            f"Confidence: <b>{conf}</b>\nEntry: <b>{i.fmt(signal.entry)}</b>\n"
            f"Stop loss: {i.fmt(signal.stop_loss)}\nTake profit: {i.fmt(signal.take_profit)}\n"
            f"Risk/Reward: 1:{signal.risk_reward:g}\n\n<b>Why?</b>\n{html.escape(why)}")
    return _deliver(f"SignalDesk: {i} {signal.direction.upper()}", text, plain, signal, "new")


def alert_closed(signal):
    i = signal.instrument
    word = {"tp": "✅ Take profit hit", "sl": "❌ Stop loss hit", "expired": "⏱ Expired"}[signal.status]
    detail = f"{signal.pips:+.1f} pips ({signal.result_r:+.2f}R)" if signal.pips is not None else ""
    plain = f"{i} {signal.direction.upper()}: {word}. {detail}"
    text = f"{word}\n<b>{html.escape(str(i))} {signal.direction.upper()}</b> — {detail}"
    return _deliver(f"SignalDesk: {i} {word}", text, plain, signal, "closed")


def send_test():
    return _deliver("SignalDesk test alert", "✅ <b>SignalDesk FX</b> alerts are working.",
                    "SignalDesk FX alerts are working.", None, "test")
