"""Live analysis cycle, signal tracking and backtesting."""

import logging
from collections import defaultdict
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone as dj_tz

from apps.core.models import SiteSettings
from apps.market.data import load_frame, update_candles
from apps.macro.services import MacroData, refresh_macro
from apps.market.providers import ProviderError, get_provider
from apps.news.services import NewsCalendar, refresh_calendar

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
    return MacroData.cached().attach(build_features(entry_df, htf, profile), instrument)


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


def analyse_pair(profile, instrument, provider, run, news=None):
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
    decision = decide(row, profile, instrument, news)
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
    refresh_calendar()
    refresh_macro()
    news = NewsCalendar.load()
    for profile in StrategyProfile.objects.filter(is_active=True):
        run = profile.backtests.first()
        for instrument in profile.analysed_instruments():
            try:
                state, new_signal, closed = analyse_pair(profile, instrument, provider, run, news)
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
    if getattr(settings, "AUTO_MAINTENANCE", True):
        schedule_self_tuning()
        schedule_discovery(site)
    return dict(summary)


# A job that started this long ago without finishing was cut off (e.g. the server restarted): run it again.
RETRY_AFTER = timedelta(hours=3)


def schedule_discovery(site):
    """Search for new strategies in the background when due (monthly by default)."""
    from apps.core import background

    from .discovery import discover

    if not site.auto_discover:
        return
    from .models import DiscoveryRun

    if background.is_running("discover"):
        return
    now = dj_tz.now()
    last = DiscoveryRun.objects.first()
    interrupted = last is not None and not last.finished and now - last.started_at >= RETRY_AFTER
    due = site.last_discovered_at is None or now - site.last_discovered_at >= timedelta(days=site.discover_every_days)
    if due or interrupted:
        background.start("discover", discover)


def schedule_self_tuning():
    """Start self-tuning in the background for every active strategy that is due (weekly by default)."""
    from apps.core import background

    from .tuning import tune_profile

    now = dj_tz.now()
    for profile in StrategyProfile.objects.filter(is_active=True, auto_tune=True):
        last = profile.last_tuned_at
        if background.is_running(f"tune:{profile.pk}") or background.is_running(f"backtest:{profile.pk}"):
            continue
        due = last is None or now - last >= timedelta(days=profile.tune_every_days)
        interrupted = (last is not None and now - last >= RETRY_AFTER
                       and not profile.tunings.filter(started_at__gte=last).exists())
        if due or interrupted:
            background.start(f"tune:{profile.pk}", tune_profile, profile.pk)


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------


def prepare_features(profile, instrument, provider, *, download=True):
    """Full-history features for one pair. Returns (features, None) or (None, error message)."""
    if download:
        try:
            for tf in [profile.entry_timeframe, *profile.clean_confirm()]:
                update_candles(instrument, tf, provider, history=True)
        except ProviderError as exc:
            return None, f"Download failed: {exc}"
    features = load_features(profile, instrument, provider, limit=20000)
    if features is None or len(features) < 300:
        return None, "Not enough history"
    missing = [tf for tf in profile.clean_confirm() if features[f"htf_{tf}"].notna().sum() == 0]
    if missing:
        return None, f"No {', '.join(missing)} candles stored"
    return features, None


def backtest(profile, *, download=True, instruments=None):
    """Replay the profile over the stored history. One trade at a time per pair, like the live board."""
    from .tuning import buckets_of, scan, simulate, summarise

    provider = get_provider(SiteSettings.load())
    news = NewsCalendar.load()
    params = profile.tuned_params()
    per_pair, trades = {}, []
    start = end = None
    for instrument in list(instruments or profile.analysed_instruments()):
        features, error = prepare_features(profile, instrument, provider, download=download)
        if error:
            per_pair[instrument.symbol] = {"error": error}
            continue
        start = min(start, features.index[0]) if start is not None else features.index[0]
        end = max(end, features.index[-1]) if end is not None else features.index[-1]
        pair_trades = simulate(scan(features, profile, instrument, news), params, profile.max_hold_bars)
        stats = summarise(pair_trades)
        per_pair[instrument.symbol] = {
            "trades": stats["trades"], "wins": stats["wins"], "losses": stats["losses"], "expired": stats["expired"],
            "total_r": round(stats["total_r"], 2), "pips": round(sum(t["pips"] for t in pair_trades), 1),
            "win_rate": stats["win_rate"]}
        trades += pair_trades

    stats = summarise(trades)
    return BacktestRun.objects.create(
        profile=profile,
        period_start=start.to_pydatetime() if start is not None else None,
        period_end=end.to_pydatetime() if end is not None else None,
        trades=stats["trades"], wins=stats["wins"], losses=stats["losses"], expired=stats["expired"],
        win_rate=stats["win_rate"], avg_r=round(stats["avg_r"], 2) if stats["avg_r"] is not None else None,
        profit_factor=stats["profit_factor"], total_r=stats["total_r"],
        buckets=buckets_of(trades), per_pair=per_pair,
    )
