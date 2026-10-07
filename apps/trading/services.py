"""
The auto-trader: after each analysis cycle it

  1. syncs trades it opened earlier (take profit / stop loss filled at the broker, or closed by hand),
  2. closes trades whose signal expired (like the backtest), and
  3. opens a trade for each fresh TRADE NOW signal that passes every safety check.

Stop loss and take profit are attached to the order at the broker, so they work even if this server is down.
Every signal it does not trade gets a "skipped" record with the reason, so nothing happens silently.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from django.db import IntegrityError, transaction
from django.utils import timezone as dj_tz

from apps.core.models import SiteSettings
from apps.market.models import TIMEFRAMES

from .models import AutoTrader, BrokerTrade
from .oanda import BrokerError, OandaBroker, client_id

log = logging.getLogger("apps.trading")

FRESH_GRACE = timedelta(minutes=10)  # how late after its candle a signal may still be traded


def get_broker(trader=None, site=None):
    site = site or SiteSettings.load()
    trader = trader or AutoTrader.load()
    return OandaBroker(site.oanda_api_token, trader.account_id, site.oanda_environment)


def oanda_name(instrument):
    return instrument.oanda_symbol or instrument.symbol.replace("/", "_")


def _notify(subject, lines):
    from apps.alerts.services import notify

    try:
        notify(subject, lines)
    except Exception:  # alerts must never break trading
        log.exception("Trade alert failed")


# ---------------------------------------------------------------------------
# Account state and loss limits
# ---------------------------------------------------------------------------


def refresh_account(trader, broker, now):
    """Store balance/NAV; reset day and week start values; halt when a loss limit is hit."""
    s = broker.summary()
    trader.account_currency, trader.balance, trader.nav = s["currency"], s["balance"], s["nav"]
    today = now.date()
    monday = today - timedelta(days=today.weekday())
    if trader.day_start != today or trader.day_start_nav is None:
        trader.day_start, trader.day_start_nav = today, s["nav"]
    if trader.week_start != monday or trader.week_start_nav is None:
        trader.week_start, trader.week_start_nav = monday, s["nav"]
    if trader.halted_until and now >= trader.halted_until:
        trader.halted_until, trader.halt_reason = None, ""
    day_loss = 100 * (trader.day_start_nav - s["nav"]) / trader.day_start_nav if trader.day_start_nav else 0
    week_loss = 100 * (trader.week_start_nav - s["nav"]) / trader.week_start_nav if trader.week_start_nav else 0
    if week_loss >= float(trader.weekly_loss_limit) and not trader.halted_until:
        trader.halted_until = datetime.combine(monday + timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)
        trader.halt_reason = f"Weekly loss limit reached ({week_loss:.1f}% this week). Trading resumes next Monday."
        _notify("Auto-trading paused", [trader.halt_reason])
    elif day_loss >= float(trader.daily_loss_limit) and not trader.halted_until:
        trader.halted_until = datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        trader.halt_reason = f"Daily loss limit reached ({day_loss:.1f}% today). Trading resumes tomorrow (UTC)."
        _notify("Auto-trading paused", [trader.halt_reason])
    return s


# ---------------------------------------------------------------------------
# Syncing open trades
# ---------------------------------------------------------------------------


def recover_pending(broker, now):
    """An order that was sent but never recorded (e.g. the server stopped mid-way): ask OANDA by its client ID."""
    stale = BrokerTrade.objects.filter(status=BrokerTrade.Status.PENDING, created_at__lt=now - timedelta(minutes=2))
    for bt in stale.select_related("signal"):
        try:
            info = broker.trade(f"@{client_id(bt.signal)}")
        except BrokerError:
            bt.status, bt.message = BrokerTrade.Status.ERROR, "Order was interrupted and not found at OANDA."
            bt.save(update_fields=["status", "message"])
            continue
        bt.status = BrokerTrade.Status.OPEN if info["state"] == "OPEN" else BrokerTrade.Status.CLOSED
        bt.broker_trade_id = info["id"]
        bt.fill_price, bt.units, bt.opened_at = info["price"], int(abs(info["units"])), bt.opened_at or now
        bt.message = "Recovered after an interruption."
        bt.save()


def sync_trades(broker, now, trader):
    recover_pending(broker, now)
    closed = []
    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.OPEN).select_related("signal", "instrument"):
        try:
            info = broker.trade(bt.broker_trade_id)
            if info["state"] == "OPEN" and trader.close_on_expiry and bt.signal.status == "expired":
                result = broker.close_trade(bt.broker_trade_id)
                info = {"state": "CLOSED", "realized_pl": result["realized_pl"], "close_price": result["price"],
                        "reason": "expired"}
        except BrokerError as exc:
            bt.message = f"Could not check: {exc}"[:255]
            bt.save(update_fields=["message"])
            continue
        if info["state"] != "CLOSED":
            continue
        bt.status = BrokerTrade.Status.CLOSED
        bt.closed_at = now
        bt.close_price = info["close_price"]
        bt.realized_pl = info["realized_pl"]
        bt.close_reason = info["reason"]
        bt.save()
        closed.append(bt)
        label = {"tp": "take profit", "sl": "stop loss", "expired": "signal expired"}.get(bt.close_reason, "closed")
        _notify(f"Closed {bt.instrument} ({label})",
                [f"{bt.instrument} {bt.direction.upper()} closed: {label}",
                 f"Result: {bt.realized_pl:+.2f} {trader.account_currency}"])
    return closed


# ---------------------------------------------------------------------------
# Opening trades
# ---------------------------------------------------------------------------


def position_units(risk_amount, entry, stop, loss_factor, units_precision, min_units):
    """Units so that hitting the stop loses about `risk_amount` in the account currency (rounded down)."""
    distance = abs(entry - stop)
    if distance <= 0 or loss_factor <= 0:
        return 0
    raw = risk_amount / (distance * loss_factor)
    step = 10 ** -units_precision
    units = math.floor(raw / step + 1e-6) * step  # round down (never risk more), ignoring float noise
    units = round(units, units_precision)
    return units if units >= min_units else 0


def fresh_signals(trader, now):
    from apps.signals.models import Signal

    profiles = list(trader.traded_profiles())
    signals = (Signal.objects.filter(status=Signal.Status.OPEN, profile__in=profiles, broker_trade__isnull=True)
               .select_related("instrument", "profile").order_by("candle_time"))
    out = []
    for s in signals:
        length = timedelta(minutes=TIMEFRAMES[s.profile.entry_timeframe])
        if now - s.candle_time <= min(length, timedelta(minutes=60)) + FRESH_GRACE:
            out.append(s)
    return out


def _record(signal, status, message, **fields):
    return BrokerTrade.objects.create(signal=signal, instrument=signal.instrument, direction=signal.direction,
                                      status=status, message=message[:255], **fields)


def _blocking_news(signal, now):
    from apps.news.services import NewsCalendar, currencies_for

    p = signal.profile
    if not p.news_filter:
        return None
    return NewsCalendar.load().blocking(now, currencies_for(signal.instrument), p.news_minutes_before,
                                        p.news_minutes_after, p.news_include_medium)


def open_trade(signal, trader, broker, account, open_instruments, now):
    """Run every check, then place the order. Always leaves a BrokerTrade record."""
    i = signal.instrument
    name = oanda_name(i)
    env = broker.environment
    if trader.min_confidence and (signal.confidence is None or signal.confidence < trader.min_confidence):
        return _record(signal, BrokerTrade.Status.SKIPPED,
                       f"Confidence {signal.confidence if signal.confidence is not None else 'not calibrated'} "
                       f"is below your minimum of {trader.min_confidence}%.", environment=env)
    if name in open_instruments:
        return _record(signal, BrokerTrade.Status.SKIPPED, f"A trade on {i} is already open.", environment=env)
    if account["open_trades"] >= trader.max_open_trades:
        return _record(signal, BrokerTrade.Status.SKIPPED,
                       f"Already {account['open_trades']} open trades (your maximum is {trader.max_open_trades}).",
                       environment=env)
    event = _blocking_news(signal, now)
    if event:
        return _record(signal, BrokerTrade.Status.SKIPPED,
                       f"News: {event['currency']} {event['title']} at {event['time']:%H:%M} UTC.", environment=env)

    # Reserve the signal first, so a second process can never place it twice.
    try:
        with transaction.atomic():
            bt = _record(signal, BrokerTrade.Status.PENDING, "Placing order…", environment=env,
                         stop_loss=signal.stop_loss, take_profit=signal.take_profit)
    except IntegrityError:
        return None
    try:
        info = broker.instrument_info(name)
        quote = broker.price(name)
        if not quote["tradeable"]:
            raise _Skip(f"{i} is not tradable right now (market closed or halted).")
        buy = signal.direction == "buy"
        price = quote["ask"] if buy else quote["bid"]
        risk_dist = abs(signal.entry - signal.stop_loss)
        if (buy and (price <= signal.stop_loss or price >= signal.take_profit)) or \
                (not buy and (price >= signal.stop_loss or price <= signal.take_profit)):
            raise _Skip(f"Price {price} has already reached the stop loss or take profit level.")
        moved = abs(price - signal.entry)
        if moved > float(trader.max_slippage_r) * risk_dist:
            raise _Skip(f"Price is {moved / float(i.pip_size):.1f} pips away from the signal's entry "
                        f"(limit {float(trader.max_slippage_r):g} of the stop distance): too late to enter.")
        risk_amount = account["nav"] * float(trader.risk_percent) / 100
        units = position_units(risk_amount, price, signal.stop_loss, quote["loss_factor"], info["units_precision"],
                               info["min_units"])
        if units <= 0:
            raise _Skip("Position would be smaller than the broker minimum for this risk amount.")
        signed = units if buy else -units
        if info["units_precision"] == 0:
            signed = int(signed)
        fill = broker.market_order(name, signed, signal.stop_loss, signal.take_profit, info["precision"],
                                   client_id(signal), comment=f"{signal.profile.name}"[:120])
    except _Skip as skip:
        bt.status, bt.message = BrokerTrade.Status.SKIPPED, str(skip)[:255]
        bt.save()
        return bt
    except BrokerError as exc:
        text = str(exc)
        bt.status = BrokerTrade.Status.REJECTED if "Order cancelled" in text or "OANDA 4" in text else \
            BrokerTrade.Status.ERROR
        bt.message = text[:255]
        bt.save()
        _notify(f"Order for {i} not placed", [f"{i} {signal.direction.upper()}: {text}"])
        return bt
    bt.status = BrokerTrade.Status.OPEN
    bt.units = int(abs(fill["units"]))
    bt.requested_price, bt.fill_price = price, fill["price"]
    bt.risk_amount = round(risk_amount, 2)
    bt.broker_trade_id = fill["trade_id"]
    bt.opened_at = now
    bt.message = f"Opened {abs(fill['units']):g} units, risking about {risk_amount:.2f} {account['currency']}."
    bt.save()
    account["open_trades"] += 1
    open_instruments.add(name)
    _notify(f"Opened {signal.direction.upper()} {i}", [
        f"{'🟢' if buy else '🔴'} {signal.direction.upper()} {i} — {abs(fill['units']):g} units at {i.fmt(fill['price'])}",
        f"Stop loss {i.fmt(signal.stop_loss)} · Take profit {i.fmt(signal.take_profit)}",
        f"Risk about {risk_amount:.2f} {account['currency']} ({float(trader.risk_percent):g}%) · "
        f"{'PRACTICE' if env == 'practice' else 'LIVE'} account · {signal.profile}"])
    return bt


class _Skip(Exception):
    pass


# ---------------------------------------------------------------------------
# Cycle
# ---------------------------------------------------------------------------


def trade_cycle(now=None):
    """Called after every analysis cycle. Never raises."""
    trader = AutoTrader.load()
    site = SiteSettings.load()
    now = now or dj_tz.now()
    if not trader.account_id or not site.oanda_api_token:
        return {"status": "not set up"}
    if site.data_provider != "oanda":
        trader.last_error = ("Auto-trading needs OANDA as the price source (Settings → Data source), so signal "
                             "levels match the prices it trades at.")
        trader.save(update_fields=["last_error"])
        return {"status": "needs oanda data"}
    if site.oanda_environment == "live" and not trader.allow_live:
        trader.last_error = "Live account selected but live trading is not allowed in Auto-trading settings."
        trader.save(update_fields=["last_error"])
        return {"status": "live not allowed"}
    summary = {"opened": 0, "closed": 0, "skipped": 0}
    try:
        broker = get_broker(trader, site)
        summary["closed"] = len(sync_trades(broker, now, trader))
        account = refresh_account(trader, broker, now)
        trader.last_error = ""
        if trader.enabled and not trader.halted_until:
            signals = fresh_signals(trader, now)
            if signals:
                open_instruments = {t["instrument"] for t in broker.open_trades()}
                for signal in signals:
                    bt = open_trade(signal, trader, broker, account, open_instruments, now)
                    if bt and bt.status == BrokerTrade.Status.OPEN:
                        summary["opened"] += 1
                    elif bt:
                        summary["skipped"] += 1
    except BrokerError as exc:
        trader.last_error = str(exc)[:255]
        log.warning("Auto-trading: %s", exc)
    trader.last_sync_at = now
    trader.save()
    return summary


def close_all(reason="Closed from the dashboard"):
    """Kill switch: close every open trade the auto-trader opened."""
    trader = AutoTrader.load()
    broker = get_broker(trader)
    closed, errors = 0, []
    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.OPEN):
        try:
            result = broker.close_trade(bt.broker_trade_id)
        except BrokerError as exc:
            errors.append(f"{bt.instrument}: {exc}")
            continue
        bt.status, bt.close_reason = BrokerTrade.Status.CLOSED, "closed"
        bt.close_price, bt.realized_pl, bt.closed_at = result["price"], result["realized_pl"], dj_tz.now()
        bt.message = reason[:255]
        bt.save()
        closed += 1
    return closed, errors
