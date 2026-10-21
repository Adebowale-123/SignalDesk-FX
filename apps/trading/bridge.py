"""
Server side of the MetaTrader 5 bridge.

The bridge (bridge/signaldesk_bridge.py) runs on a Windows computer next to the MT5 terminal. Every few seconds
it posts what MT5 knows (account, open positions, recently closed deals, results of orders it placed) and gets
back what to do (orders to place, positions to close). All decisions and safety rules stay here; the bridge only
executes.

Orders carry stop-loss and take-profit *distances*, not prices: the bridge applies them to its own fill price, so a
small price difference between this portal's data feed and the broker (e.g. gold futures vs spot) doesn't change
the risk or reward of the trade.
"""

import logging
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.utils import timezone as dj_tz

from apps.market.data import latest_price
from apps.market.models import TIMEFRAMES

from .models import AutoTrader, BrokerTrade
from .oanda import client_id
from .services import FRESH_GRACE, _blocking_news, _notify, apply_account, fresh_signals

log = logging.getLogger("apps.trading")

POLL_SECONDS = 20
REISSUE_AFTER = timedelta(seconds=60)  # an order the bridge hasn't confirmed is handed out again (it de-duplicates)
GIVE_UP_AFTER = timedelta(minutes=10)


def mt5_symbol(instrument):
    return instrument.symbol.replace("/", "").upper()


def _norm(symbol):
    return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())


def _matches(position_symbol, instrument):
    return _norm(position_symbol).startswith(mt5_symbol(instrument))


# ---------------------------------------------------------------------------
# Results, positions and closed deals reported by the bridge
# ---------------------------------------------------------------------------


def _apply_results(results, trader, now):
    for r in results:
        bt = BrokerTrade.objects.filter(client_id=r.get("id"), status=BrokerTrade.Status.PENDING) \
            .select_related("instrument", "signal__profile").first()
        if bt is None:
            continue
        status = r.get("status")
        bt.message = (r.get("message") or "")[:255]
        if status == "opened":
            bt.status = BrokerTrade.Status.OPEN
            bt.broker_trade_id = str(r.get("ticket") or "")
            bt.fill_price, bt.lots = r.get("price"), r.get("lots")
            bt.stop_loss, bt.take_profit = r.get("sl"), r.get("tp")
            bt.risk_amount = r.get("risk")
            bt.opened_at = now
            bt.save()
            i = bt.instrument
            _notify(f"Opened {bt.direction.upper()} {i}", [
                f"{'🟢' if bt.direction == 'buy' else '🔴'} {bt.direction.upper()} {i} — {bt.lots} lots at "
                f"{i.fmt(bt.fill_price) if bt.fill_price else '?'}",
                f"Stop loss {i.fmt(bt.stop_loss) if bt.stop_loss else '?'} · "
                f"Take profit {i.fmt(bt.take_profit) if bt.take_profit else '?'}",
                f"Risk about {bt.risk_amount or 0:.2f} {trader.account_currency} · "
                f"{'REAL' if trader.bridge_account_is_real else 'DEMO'} account · {bt.signal.profile}"])
        else:
            bt.status = {"skipped": BrokerTrade.Status.SKIPPED, "rejected": BrokerTrade.Status.REJECTED}.get(
                status, BrokerTrade.Status.ERROR)
            bt.save()
            if bt.status != BrokerTrade.Status.SKIPPED:
                _notify(f"Order for {bt.instrument} not placed", [f"{bt.instrument}: {bt.message}"])


def _sync(positions, closed, trader, now):
    tickets = {str(p.get("ticket")) for p in positions}
    by_comment = {p.get("comment"): p for p in positions if p.get("comment")}
    deals = {str(d.get("ticket")): d for d in closed}
    deals_by_comment = {d.get("comment"): d for d in closed if d.get("comment")}

    # Orders sent but never confirmed (bridge restarted mid-way): recover them from MT5's own records.
    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.PENDING, issued_at__isnull=False):
        found = by_comment.get(bt.client_id) or deals_by_comment.get(bt.client_id)
        if found:
            bt.status = BrokerTrade.Status.OPEN
            bt.broker_trade_id = str(found.get("ticket"))
            bt.fill_price, bt.lots = found.get("open_price"), found.get("volume")
            bt.opened_at, bt.message = now, "Recovered from MetaTrader after an interruption."
            bt.save()
        elif now - bt.issued_at > GIVE_UP_AFTER:
            bt.status, bt.message = BrokerTrade.Status.ERROR, "The bridge never confirmed this order."
            bt.save(update_fields=["status", "message"])

    for bt in BrokerTrade.objects.filter(status=BrokerTrade.Status.OPEN).select_related("signal", "instrument"):
        if bt.broker_trade_id in tickets:
            continue
        deal = deals.get(bt.broker_trade_id)
        if deal is None:
            continue  # not reported yet; it will be on a later check-in
        reason = deal.get("reason") or "closed"
        if reason == "closed" and bt.signal.status == "expired":
            reason = "expired"
        bt.status, bt.close_reason = BrokerTrade.Status.CLOSED, reason
        bt.close_price, bt.realized_pl, bt.closed_at = deal.get("price"), deal.get("profit"), now
        bt.save()
        label = {"tp": "take profit", "sl": "stop loss", "expired": "signal expired"}.get(reason, "closed")
        _notify(f"Closed {bt.instrument} ({label})", [
            f"{bt.instrument} {bt.direction.upper()} closed: {label}",
            f"Result: {(bt.realized_pl or 0):+.2f} {trader.account_currency}"])


# ---------------------------------------------------------------------------
# Orders for the bridge
# ---------------------------------------------------------------------------


def _order(bt, signal, trader, now):
    length = timedelta(minutes=TIMEFRAMES[signal.profile.entry_timeframe])
    return {"id": bt.client_id, "symbol": mt5_symbol(signal.instrument), "side": signal.direction,
            "sl_distance": abs(signal.entry - signal.stop_loss), "tp_distance": abs(signal.take_profit - signal.entry),
            "risk_percent": float(trader.risk_percent), "signal_entry": signal.entry,
            "expires_at": (signal.candle_time + min(length, timedelta(minutes=60)) + FRESH_GRACE).isoformat(),
            "label": f"{signal.profile.name} {signal.direction} {signal.instrument}"}


def _skip(signal, message):
    return BrokerTrade.objects.create(signal=signal, instrument=signal.instrument, direction=signal.direction,
                                      environment="mt5", status=BrokerTrade.Status.SKIPPED, message=message[:255],
                                      client_id=client_id(signal))


def _new_orders(trader, positions, now):
    orders = []
    pending = list(BrokerTrade.objects.filter(status=BrokerTrade.Status.PENDING).select_related(
        "signal__profile", "signal__instrument"))
    for bt in pending:  # hand out unconfirmed orders again; the bridge never places the same ID twice
        if bt.issued_at and now - bt.issued_at >= REISSUE_AFTER and now - bt.issued_at < GIVE_UP_AFTER:
            orders.append(_order(bt, bt.signal, trader, now))
    busy = [p.get("symbol") for p in positions] + [mt5_symbol(bt.instrument) for bt in pending]
    open_count = len(positions) + len(pending)
    for signal in fresh_signals(trader, now):
        i = signal.instrument
        if trader.min_confidence and (signal.confidence is None or signal.confidence < trader.min_confidence):
            _skip(signal, f"Confidence {signal.confidence if signal.confidence is not None else 'not calibrated'} "
                          f"is below your minimum of {trader.min_confidence}%.")
            continue
        if any(_matches(sym, i) for sym in busy):
            _skip(signal, f"A trade on {i} is already open.")
            continue
        if open_count >= trader.max_open_trades:
            _skip(signal, f"Already {open_count} open trades (your maximum is {trader.max_open_trades}).")
            continue
        event = _blocking_news(signal, now)
        if event:
            _skip(signal, f"News: {event['currency']} {event['title']} at {event['time']:%H:%M} UTC.")
            continue
        price = latest_price(i)
        risk_dist = abs(signal.entry - signal.stop_loss)
        if price is not None and abs(price - signal.entry) > float(trader.max_slippage_r) * risk_dist:
            _skip(signal, f"Price is {abs(price - signal.entry) / float(i.pip_size):.1f} pips away from the signal's "
                          f"entry: too late to enter.")
            continue
        try:
            with transaction.atomic():
                bt = BrokerTrade.objects.create(
                    signal=signal, instrument=i, direction=signal.direction, environment="mt5",
                    status=BrokerTrade.Status.PENDING, client_id=client_id(signal), issued_at=now,
                    stop_loss=signal.stop_loss, take_profit=signal.take_profit, message="Sent to the bridge…")
        except IntegrityError:
            continue
        orders.append(_order(bt, signal, trader, now))
        busy.append(mt5_symbol(i))
        open_count += 1
    return orders


def exchange(payload, now=None):
    """One check-in from the bridge. Returns what it should do next."""
    now = now or dj_tz.now()
    trader = AutoTrader.load()
    trader.bridge_seen_at = now
    trader.bridge_info = payload.get("terminal") or trader.bridge_info
    positions = [p for p in payload.get("positions") or [] if p.get("ours")]
    all_positions = payload.get("positions") or []
    _apply_results(payload.get("results") or [], trader, now)
    _sync(positions, payload.get("closed") or [], trader, now)

    orders, closes, note = [], [], ""
    account = payload.get("account")
    if account:
        apply_account(trader, {"currency": account.get("currency", ""), "balance": float(account.get("balance", 0)),
                               "nav": float(account.get("equity", 0))}, now)
    if trader.broker_type != AutoTrader.Broker.MT5:
        note = "Auto-trading is set to OANDA, not MetaTrader 5."
    elif trader.bridge_account_is_real and not trader.allow_live:
        note = "This MT5 account is a REAL account: allow live trading on the Auto-trading page first."
    elif trader.enabled and not trader.halted_until and account:
        orders = _new_orders(trader, all_positions, now)
    if trader.close_on_expiry:
        closes = [bt.broker_trade_id for bt in BrokerTrade.objects.filter(
            status=BrokerTrade.Status.OPEN, signal__status="expired").exclude(broker_trade_id="")]
    close_all = trader.close_all_requested
    if close_all and not positions:
        trader.close_all_requested = close_all = False
    trader.last_error = note
    trader.last_sync_at = now
    trader.save_state(*trader.BRIDGE_FIELDS)
    return {"ok": True, "enabled": trader.enabled, "orders": orders, "close": closes, "close_all": close_all,
            "poll_seconds": POLL_SECONDS, "note": note}
