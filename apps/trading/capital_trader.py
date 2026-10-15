"""
Auto-trading through Capital.com, run on the server after each analysis cycle (no extra computer needed).

Each cycle: sync positions (closed at take profit / stop loss, or by hand), close positions whose signal expired,
recover an order interrupted mid-way, then open a position for every fresh signal that passes the safety checks.
Stop loss and take profit are placed at the signal's distances from Capital.com's own live price, so a small
difference between this portal's price feed and the broker's doesn't change the trade's risk or reward.
"""

import logging
import math
from datetime import datetime, timedelta, timezone

from django.db import IntegrityError, transaction

from apps.market.data import latest_price

from .capital import BrokerError, CapitalBroker, epic_for
from .models import BrokerTrade
from .services import _blocking_news, _notify, apply_account, fresh_signals

log = logging.getLogger("apps.trading")


def get_capital(trader):
    return CapitalBroker(trader.capital_api_key, trader.capital_identifier, trader.capital_password,
                         trader.capital_environment)


def _env(broker):
    return f"capital-{broker.environment}"


def size_for(risk_amount, distance, loss_factor, lot_size, min_size, step):
    """Deal size so that hitting the stop loses about risk_amount (rounded down to the broker's step)."""
    if distance <= 0 or loss_factor <= 0 or step <= 0:
        return 0.0
    raw = risk_amount / (distance * loss_factor * (lot_size or 1))
    size = math.floor(raw / step + 1e-9) * step
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    size = round(size, decimals)
    return size if size >= min_size else 0.0


def loss_factor(broker, instrument, price, account_currency):
    """How much one unit of the quote currency is worth in the account currency."""
    base, quote = [c.upper() for c in instrument.currencies]
    if quote == "XAU":
        quote = "USD"
    acc = (account_currency or "USD").upper()
    if quote == acc:
        return 1.0
    if base == acc:
        return 1.0 / price
    for epic, invert in ((f"{quote}{acc}", False), (f"{acc}{quote}", True)):
        try:
            m = broker.market(epic)
        except BrokerError:
            continue
        mid = (m["bid"] + m["offer"]) / 2
        return 1.0 / mid if invert else mid
    raise BrokerError(f"Cannot convert {quote} to the account currency {acc}.")


def _parse(stamp):
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "")).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(broker, trader, positions, now):
    open_ids = {p["deal_id"] for p in positions}

    # An order interrupted between "sent" and "saved": adopt the matching position, if any.
    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.PENDING, environment__startswith="capital",
                                         issued_at__lt=now - timedelta(minutes=2)).select_related("instrument"):
        epic = epic_for(bt.instrument)
        taken = set(BrokerTrade.objects.exclude(pk=bt.pk).values_list("broker_trade_id", flat=True))
        match = next((p for p in positions if p["epic"] == epic and p["deal_id"] not in taken
                      and p["direction"] == bt.direction.upper()
                      and (_parse(p["created"]) or now) >= bt.issued_at - timedelta(minutes=1)), None)
        if match:
            bt.status, bt.broker_trade_id = BrokerTrade.Status.OPEN, match["deal_id"]
            bt.fill_price, bt.lots, bt.opened_at = match["level"], match["size"], now
            bt.message = "Recovered after an interruption."
        else:
            bt.status, bt.message = BrokerTrade.Status.ERROR, "Order was interrupted and not found at Capital.com."
        bt.save()

    closed = []
    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.OPEN, environment__startswith="capital") \
            .select_related("signal", "instrument"):
        reason = level = None
        if bt.broker_trade_id in open_ids:
            if not (trader.close_on_expiry and bt.signal.status == "expired"):
                continue
            try:
                level = broker.close_position(bt.broker_trade_id)["level"]
            except BrokerError as exc:
                bt.message = f"Could not close at expiry: {exc}"[:255]
                bt.save(update_fields=["message"])
                continue
            reason = "expired"
        else:
            try:
                reason, level = broker.close_reason(bt.broker_trade_id, (bt.opened_at or now) - timedelta(minutes=1))
            except BrokerError:
                reason, level = "closed", None
            if reason == "closed" and bt.signal.status == "expired":
                reason = "expired"
        bt.status, bt.close_reason, bt.closed_at = BrokerTrade.Status.CLOSED, reason, now
        bt.close_price = level
        if level and bt.fill_price and bt.lots:
            sign = 1 if bt.direction == "buy" else -1
            try:
                factor = loss_factor(broker, bt.instrument, level, trader.account_currency)
            except BrokerError:
                factor = None
            if factor:
                bt.realized_pl = round((level - bt.fill_price) * sign * bt.lots * factor, 2)
        bt.save()
        closed.append(bt)
        label = {"tp": "take profit", "sl": "stop loss", "expired": "signal expired"}.get(reason, "closed")
        result = f"{bt.realized_pl:+.2f} {trader.account_currency} (approx.)" if bt.realized_pl is not None else \
            "see Capital.com"
        _notify(f"Closed {bt.instrument} ({label})", [f"{bt.instrument} {bt.direction.upper()} closed: {label}",
                                                      f"Result: {result}"])
    return closed


# ---------------------------------------------------------------------------
# Opening
# ---------------------------------------------------------------------------


class _Skip(Exception):
    pass


def _skip(signal, message, env):
    return BrokerTrade.objects.create(signal=signal, instrument=signal.instrument, direction=signal.direction,
                                      environment=env, status=BrokerTrade.Status.SKIPPED, message=message[:255])


def open_position(signal, trader, broker, account, busy, now):
    i, env = signal.instrument, _env(broker)
    epic = epic_for(i)
    if trader.min_confidence and (signal.confidence is None or signal.confidence < trader.min_confidence):
        return _skip(signal, f"Confidence {signal.confidence if signal.confidence is not None else 'not calibrated'} "
                             f"is below your minimum of {trader.min_confidence}%.", env)
    if epic in busy:
        return _skip(signal, f"A trade on {i} is already open.", env)
    if account["open"] >= trader.max_open_trades:
        return _skip(signal, f"Already {account['open']} open trades (your maximum is {trader.max_open_trades}).",
                     env)
    event = _blocking_news(signal, now)
    if event:
        return _skip(signal, f"News: {event['currency']} {event['title']} at {event['time']:%H:%M} UTC.", env)
    risk_dist = abs(signal.entry - signal.stop_loss)
    source_price = latest_price(i)
    if source_price is not None and abs(source_price - signal.entry) > float(trader.max_slippage_r) * risk_dist:
        return _skip(signal, f"Price is {abs(source_price - signal.entry) / float(i.pip_size):.1f} pips away from "
                             f"the signal's entry: too late to enter.", env)
    try:
        with transaction.atomic():
            bt = BrokerTrade.objects.create(signal=signal, instrument=i, direction=signal.direction, environment=env,
                                            status=BrokerTrade.Status.PENDING, issued_at=now,
                                            message="Placing order…")
    except IntegrityError:
        return None
    try:
        m = broker.market(epic)
        if m["status"] != "TRADEABLE":
            raise _Skip(f"{i} is not tradable right now ({m['status'].lower()}).")
        buy = signal.direction == "buy"
        price = m["offer"] if buy else m["bid"]
        tp_dist = abs(signal.take_profit - signal.entry)
        if min(risk_dist, tp_dist) < m["min_stop"]:
            raise _Skip("Stop or target is closer than Capital.com allows for this market.")
        stop = round(price - risk_dist if buy else price + risk_dist, m["decimals"])
        target = round(price + tp_dist if buy else price - tp_dist, m["decimals"])
        risk_amount = account["nav"] * float(trader.risk_percent) / 100
        factor = loss_factor(broker, i, price, account["currency"])
        size = size_for(risk_amount, risk_dist, factor, m["lot_size"], m["min_size"], m["size_step"])
        if size <= 0:
            raise _Skip(f"Trade would be below Capital.com's minimum size ({m['min_size']:g}) at "
                        f"{float(trader.risk_percent):g}% risk.")
        exposure = size * (m["lot_size"] or 1) * price * factor  # position value in the account currency
        cap = (20 if i.kind == "metal" else 30) * account["nav"]
        if exposure > cap:  # never more than retail maximum leverage, whatever the size units turn out to be
            raise _Skip(f"Position would be worth {exposure:,.0f} {account['currency']} "
                        f"({exposure / account['nav']:.0f}× the account): not placed as a safety measure.")
        fill = broker.open_position(epic, signal.direction, size, stop, target)
    except _Skip as skip:
        bt.status, bt.message = BrokerTrade.Status.SKIPPED, str(skip)[:255]
        bt.save()
        return bt
    except BrokerError as exc:
        text = str(exc)
        bt.status = BrokerTrade.Status.REJECTED if "rejected" in text.lower() or "Capital.com 4" in text else \
            BrokerTrade.Status.ERROR
        bt.message = text[:255]
        bt.save()
        _notify(f"Order for {i} not placed", [f"{i} {signal.direction.upper()}: {text}"])
        return bt
    bt.status, bt.broker_trade_id = BrokerTrade.Status.OPEN, fill["deal_id"] or ""
    bt.requested_price, bt.fill_price = price, fill["level"] or price
    bt.lots, bt.stop_loss, bt.take_profit = fill["size"] or size, stop, target
    bt.risk_amount, bt.opened_at = round(risk_amount, 2), now
    bt.message = f"Opened {bt.lots:g} units, risking about {risk_amount:.2f} {account['currency']}."
    bt.save()
    busy.add(epic)
    account["open"] += 1
    _notify(f"Opened {signal.direction.upper()} {i}", [
        f"{'🟢' if buy else '🔴'} {signal.direction.upper()} {i} — {bt.lots:g} units at {i.fmt(bt.fill_price)}",
        f"Stop loss {i.fmt(stop)} · Take profit {i.fmt(target)}",
        f"Risk about {risk_amount:.2f} {account['currency']} ({float(trader.risk_percent):g}%) · "
        f"Capital.com {broker.environment.upper()} · {signal.profile}"])
    return bt


def capital_cycle(trader, now):
    if not (trader.capital_api_key and trader.capital_identifier and trader.capital_password):
        return {"status": "not set up"}
    if trader.capital_environment == "live" and not trader.allow_live:
        trader.last_error = "Capital.com LIVE is selected but live trading is not allowed in Auto-trading settings."
        return {"status": "live not allowed"}
    summary = {"opened": 0, "closed": 0, "skipped": 0}
    broker = get_capital(trader)
    positions = broker.positions()
    summary["closed"] = len(sync(broker, trader, positions, now))
    if summary["closed"]:
        positions = broker.positions()
    s = broker.summary()
    apply_account(trader, s, now)
    trader.last_error = ""
    if trader.enabled and not trader.halted_until:
        signals = fresh_signals(trader, now)
        if signals:
            pending = BrokerTrade.objects.filter(status=BrokerTrade.Status.PENDING).select_related("instrument")
            busy = {p["epic"] for p in positions} | {epic_for(bt.instrument) for bt in pending}
            account = {"nav": s["nav"], "currency": s["currency"], "open": len(positions) + pending.count()}
            for signal in signals:
                bt = open_position(signal, trader, broker, account, busy, now)
                if bt and bt.status == BrokerTrade.Status.OPEN:
                    summary["opened"] += 1
                elif bt:
                    summary["skipped"] += 1
    return summary


def capital_close_all(trader, reason):
    broker = get_capital(trader)
    closed, errors = 0, []
    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.OPEN, environment__startswith="capital"):
        try:
            level = broker.close_position(bt.broker_trade_id)["level"]
        except BrokerError as exc:
            errors.append(f"{bt.instrument}: {exc}")
            continue
        bt.status, bt.close_reason, bt.close_price = BrokerTrade.Status.CLOSED, "closed", level
        bt.closed_at, bt.message = datetime.now(timezone.utc), reason[:255]
        bt.save()
        closed += 1
    return closed, errors
