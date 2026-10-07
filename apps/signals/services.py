"""Live analysis cycle, signal tracking and backtesting."""

import logging
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from django.db import transaction
from django.utils import timezone as dj_tz

from apps.core.models import SiteSettings
from apps.market.data import load_frame, update_candles
from apps.market.providers import ProviderError, get_provider

from .engine import build_features, confidence_for, decide
from .models import BacktestRun, PairState, Signal, StrategyProfile

log = logging.getLogger("apps.signals")


def _row(features, i):
    row = features.iloc[i].to_dict()
    row["time"] = features.index[i]
    return row


def evaluate_outcome(direction, entry, stop, target, bars, max_bars):
    """Walk forward through (time, high, low, close) bars. If SL and TP fall in the same bar, count SL (cautious).

    Returns (status, exit_price, exit_time, bars_used) or (None, ...) if still open.
    """
    bull = direction == "buy"
    used = 0
    last = None
    for time, high, low, close in bars:
        used += 1
        last = (time, close)
        if bull:
            if low <= stop:
                return Signal.Status.SL, stop, time, used
            if high >= target:
                return Signal.Status.TP, target, time, used
        else:
            if high >= stop:
                return Signal.Status.SL, stop, time, used
            if low <= target:
                return Signal.Status.TP, target, time, used
        if used >= max_bars:
            return Signal.Status.EXPIRED, close, time, used
    return None, (last[1] if last else None), (last[0] if last else None), used


def result_in_r(direction, entry, stop, exit_price):
    risk = abs(entry - stop)
    move = exit_price - entry if direction == "buy" else entry - exit_price
    return move / risk if risk else 0.0


def load_features(profile, instrument, provider, *, limit=600):
    entry_df = load_frame(instrument, profile.entry_timeframe, provider, limit=limit)
    htf = {tf: load_frame(instrument, tf, provider, limit=max(300, limit // 4)) for tf in profile.clean_confirm()}
    if entry_df.empty:
        return None
    return build_features(entry_df, htf, profile)


# ---------------------------------------------------------------------------
# Live cycle
# ---------------------------------------------------------------------------


def update_open_signals(profile, instrument, features):
    """Close open signals that have hit TP/SL or expired. Returns the signals closed now."""
    closed = []
    for sig in Signal.objects.filter(profile=profile, instrument=instrument, status=Signal.Status.OPEN):
        after = features[features.index >= sig.candle_time]
        bars = zip(after.index, after["high"], after["low"], after["close"])
        status, price, when, _ = evaluate_outcome(sig.direction, sig.entry, sig.stop_loss, sig.take_profit, bars,
                                                  profile.max_hold_bars)
        if status:
            sig.status = status
            sig.exit_price = price
            sig.closed_at = when.to_pydatetime() if hasattr(when, "to_pydatetime") else when
            sig.result_r = round(result_in_r(sig.direction, sig.entry, sig.stop_loss, price), 2)
            move = price - sig.entry if sig.direction == "buy" else sig.entry - price
            sig.pips = round(move / float(instrument.pip_size), 1)
            sig.save()
            closed.append(sig)
    return closed


def analyse_pair(profile, instrument, provider, run):
    """Refresh data, update open signals and store the latest decision. Returns (state, new_signal, closed)."""
    for tf in [profile.entry_timeframe, *profile.clean_confirm()]:
        update_candles(instrument, tf, provider)
    features = load_features(profile, instrument, provider)
    state, _ = PairState.objects.get_or_create(profile=profile, instrument=instrument)
    if features is None or features.empty:
        state.error = "No price data yet."
        state.save()
        return state, None, []

    closed = update_open_signals(profile, instrument, features)
    row = _row(features, -1)
    decision = decide(row, profile, instrument)
    confidence, samples = confidence_for(decision.score, run)

    new_signal = None
    open_signal = Signal.objects.filter(profile=profile, instrument=instrument, status=Signal.Status.OPEN).first()
    if decision.is_trade and open_signal is None:
        new_signal, created = Signal.objects.get_or_create(
            profile=profile, instrument=instrument, candle_time=decision.close_time.to_pydatetime(),
            defaults=dict(direction=decision.direction, entry=decision.entry, stop_loss=decision.stop_loss,
                          take_profit=decision.take_profit, risk_reward=decision.risk_reward, score=decision.score,
                          confidence=confidence, sample_size=samples, reasons=decision.reasons,
                          summary=decision.summary),
        )
        if not created:
            new_signal = None
        open_signal = open_signal or Signal.objects.filter(profile=profile, instrument=instrument,
                                                           status=Signal.Status.OPEN).first()

    state.decision = decision.decision
    state.direction = decision.direction
    state.score = decision.score
    state.confidence = confidence
    state.price = float(row["close"])
    state.entry, state.stop_loss, state.take_profit = decision.entry, decision.stop_loss, decision.take_profit
    state.headline = decision.headline[:160]
    state.reasons = decision.reasons
    state.summary = decision.summary
    state.candle_time = decision.close_time.to_pydatetime()
    state.active_signal = open_signal
    state.error = ""
    state.save()
    return state, new_signal, closed


def run_cycle():
    """One pass over every active profile and pair. Returns a summary dict."""
    from apps.alerts.services import alert_closed, alert_new_signal

    site = SiteSettings.load()
    summary = defaultdict(int)
    try:
        provider = get_provider(site)
    except ProviderError as exc:
        log.error("Data source not ready: %s", exc)
        return {"error": str(exc)}
    for profile in StrategyProfile.objects.filter(is_active=True):
        run = profile.backtests.first()
        for instrument in profile.analysed_instruments():
            try:
                state, new_signal, closed = analyse_pair(profile, instrument, provider, run)
            except ProviderError as exc:
                PairState.objects.update_or_create(profile=profile, instrument=instrument,
                                                   defaults={"error": str(exc)[:255]})
                summary["errors"] += 1
                continue
            summary["pairs"] += 1
            if new_signal:
                summary["new_signals"] += 1
                if new_signal.confidence is None or new_signal.confidence >= profile.alert_min_confidence:
                    transaction.on_commit(lambda s=new_signal: alert_new_signal(s))
            for sig in closed:
                summary["closed"] += 1
                if site.alert_on_close:
                    transaction.on_commit(lambda s=sig: alert_closed(s))
    return dict(summary)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def backtest(profile, *, download=True, instruments=None):
    """Replay the profile over the stored history. One trade at a time per pair, like the live board."""
    site = SiteSettings.load()
    provider = get_provider(site)
    instruments = list(instruments or profile.analysed_instruments())
    buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "expired": 0})
    per_pair = {}
    results = []
    start = end = None
    for instrument in instruments:
        if download:
            try:
                for tf in [profile.entry_timeframe, *profile.clean_confirm()]:
                    update_candles(instrument, tf, provider, history=True)
            except ProviderError as exc:
                per_pair[instrument.symbol] = {"error": f"Download failed: {exc}"}
                continue
        features = load_features(profile, instrument, provider, limit=20000)
        if features is None or len(features) < 300:
            per_pair[instrument.symbol] = {"error": "Not enough history"}
            continue
        missing = [tf for tf in profile.clean_confirm() if features[f"htf_{tf}"].notna().sum() == 0]
        if missing:
            per_pair[instrument.symbol] = {"error": f"No {', '.join(missing)} candles stored"}
            continue
        start = min(start, features.index[0]) if start else features.index[0]
        end = max(end, features.index[-1]) if end else features.index[-1]
        highs, lows, closes = features["high"].to_numpy(), features["low"].to_numpy(), features["close"].to_numpy()
        times = features.index
        stats = {"trades": 0, "wins": 0, "losses": 0, "expired": 0, "total_r": 0.0, "pips": 0.0}
        i, n = 0, len(features)
        while i < n - 1:
            d = decide(_row(features, i), profile, instrument)
            if not d.is_trade:
                i += 1
                continue
            bars = zip(times[i + 1:], highs[i + 1:], lows[i + 1:], closes[i + 1:])
            status, price, _, used = evaluate_outcome(d.direction, d.entry, d.stop_loss, d.take_profit, bars,
                                                      profile.max_hold_bars)
            if status is None:  # still running at the end of the data
                break
            # Charge the typical spread on every trade so results match what a trader would get.
            spread = float(instrument.spread_pips) * float(instrument.pip_size)
            risk = abs(d.entry - d.stop_loss)
            r = float(result_in_r(d.direction, d.entry, d.stop_loss, price)) - (spread / risk if risk else 0)
            move = (price - d.entry if d.direction == "buy" else d.entry - price) - spread
            key = d.score // 10 * 10
            outcome = {"tp": "wins", "sl": "losses", "expired": "expired"}[status]
            buckets[key][outcome] += 1
            stats[outcome] += 1
            stats["trades"] += 1
            stats["total_r"] += r
            stats["pips"] += move / float(instrument.pip_size)
            results.append(r)
            i += used + 1
        decided = stats["wins"] + stats["losses"]
        stats["win_rate"] = round(100 * stats["wins"] / decided, 1) if decided else None
        stats["total_r"] = round(float(stats["total_r"]), 2)
        stats["pips"] = round(float(stats["pips"]), 1)
        per_pair[instrument.symbol] = stats

    wins = sum(b["wins"] for b in buckets.values())
    losses = sum(b["losses"] for b in buckets.values())
    expired = sum(b["expired"] for b in buckets.values())
    gains = sum(r for r in results if r > 0)
    pains = -sum(r for r in results if r < 0)
    return BacktestRun.objects.create(
        profile=profile,
        period_start=start.to_pydatetime() if start is not None else None,
        period_end=end.to_pydatetime() if end is not None else None,
        trades=len(results), wins=wins, losses=losses, expired=expired,
        win_rate=round(100 * wins / (wins + losses), 1) if wins + losses else None,
        avg_r=round(float(np.mean(results)), 2) if results else None,
        profit_factor=round(gains / pains, 2) if pains else None,
        total_r=round(sum(results), 2),
        buckets={str(k): v for k, v in sorted(buckets.items())},
        per_pair=per_pair,
    )
