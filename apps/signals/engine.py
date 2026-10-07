"""
The decision engine: turns candles into BUY / SELL / WAIT with entry, stop loss, take profit and the reasons.

The same `decide()` is used live and in backtests, so the backtest measures exactly what the board shows.

Score (0-100), for the direction the higher timeframes point to:
  Multi-timeframe agreement   30
  Entry-timeframe trend       15
  Momentum (RSI, MACD)        15
  Entry setup                 20   (pullback to the fast EMA, or breakout of the 20-candle range)
  Market condition            20   (trending ADX, normal volatility, active session)
Hard "WAIT" rules regardless of score: market closed, outside your sessions, higher timeframes disagree,
ranging market, no entry setup, overstretched move, volatility spike, high-impact news due or just out.
"""

from dataclasses import dataclass, field
from datetime import timedelta

import numpy as np
import pandas as pd

from apps.market.models import TIMEFRAMES
from apps.news.services import currencies_for

from . import indicators as ind

SESSIONS_UTC = {"asia": (0, 9), "london": (7, 16), "newyork": (12, 21)}
SESSION_NAMES = {"asia": "Asia", "london": "London", "newyork": "New York"}


# ---------------------------------------------------------------------------
# Market hours
# ---------------------------------------------------------------------------


def market_open(ts):
    """Forex trades from Sunday ~21:00 UTC to Friday ~21:00 UTC."""
    wd, hour = ts.weekday(), ts.hour
    if wd == 5:
        return False
    if wd == 4 and hour >= 21:
        return False
    if wd == 6 and hour < 21:
        return False
    return True


def active_sessions(ts):
    if not market_open(ts):
        return []
    return [name for name, (start, end) in SESSIONS_UTC.items() if start <= ts.hour < end]


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------


def build_features(entry_df, htf_frames, profile):
    """One row per entry candle with every value the rules need (only past data per row)."""
    df = entry_df.copy()
    length = timedelta(minutes=TIMEFRAMES[profile.entry_timeframe])
    df["close_time"] = df.index + length
    close = df["close"]
    df["ema_f"] = ind.ema(close, profile.ema_fast)
    df["ema_m"] = ind.ema(close, profile.ema_mid)
    df["ema_s"] = ind.ema(close, profile.ema_slow)
    df["rsi"] = ind.rsi(close, profile.rsi_length)
    df["rsi_prev"] = df["rsi"].shift(1)
    df["hist"] = ind.macd_histogram(close)
    df["hist_prev"] = df["hist"].shift(1)
    df["atr"] = ind.atr(df)
    df["adx"] = ind.adx(df)
    df["atr_pct"] = ind.rolling_percentile(df["atr"], 100)
    df["hh"] = df["high"].rolling(20).max().shift(1)
    df["ll"] = df["low"].rolling(20).min().shift(1)
    df["low5"] = df["low"].rolling(5).min()
    df["high5"] = df["high"].rolling(5).max()
    df["swing_low"] = ind.last_swing(df["low"], "low")
    df["swing_high"] = ind.last_swing(df["high"], "high")
    df["entry_trend"] = ind.trend_series(df, profile.ema_fast, profile.ema_mid)

    # Higher timeframe trends, each aligned to the entry candle by *close* time (no peeking ahead).
    df = df.reset_index().rename(columns={"index": "time"})
    if "time" not in df.columns:
        df = df.rename(columns={df.columns[0]: "time"})
    for tf, hdf in htf_frames.items():
        if hdf is None or hdf.empty:
            df[f"htf_{tf}"] = np.nan
            continue
        trend = ind.trend_series(hdf, profile.ema_mid, profile.ema_slow).rename(f"htf_{tf}").to_frame()
        trend["htf_close_time"] = trend.index + timedelta(minutes=TIMEFRAMES[tf])
        trend = trend.dropna().sort_values("htf_close_time")
        df = pd.merge_asof(df.sort_values("close_time"), trend[["htf_close_time", f"htf_{tf}"]],
                           left_on="close_time", right_on="htf_close_time", direction="backward").drop(
            columns=["htf_close_time"])
    return df.set_index("time")


# ---------------------------------------------------------------------------
# Decision
# ---------------------------------------------------------------------------


@dataclass
class Decision:
    decision: str = "wait"  # trade / wait
    direction: str = ""  # buy / sell
    score: int = 0
    headline: str = ""
    reasons: list = field(default_factory=list)
    summary: str = ""
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_reward: float | None = None
    close_time: object = None

    @property
    def is_trade(self):
        return self.decision == "trade"


def _reason(label, value, tone="neutral"):
    return {"label": label, "value": value, "tone": tone}


def _missing(row, keys):
    return any(row.get(k) is None or (isinstance(row.get(k), float) and np.isnan(row.get(k))) for k in keys)


REQUIRED = ["close", "ema_f", "ema_m", "ema_s", "rsi", "rsi_prev", "hist", "hist_prev", "atr", "adx", "hh", "ll",
            "entry_trend"]


def _calendar(news, when, profile, instrument, blockers):
    """The economic-calendar reason; adds a blocker when relevant news is too close."""
    if news is None or not news.covers(when):
        return _reason("Economic calendar", "Not available for this time — check for news before trading", "neutral")
    currencies = currencies_for(instrument)
    medium = profile.news_include_medium
    event = news.blocking(when, currencies, profile.news_minutes_before, profile.news_minutes_after, medium)
    if event:
        due = event["time"] >= when
        text = f"{event['currency']} {event['title']} at {event['time']:%H:%M} UTC"
        if profile.news_filter:
            blockers.insert(0, f"{'high-impact news is due' if due else 'high-impact news just came out'} ({text})")
        return _reason("Economic calendar", f"{'News due' if due else 'Just released'}: {text}", "bad")
    upcoming = news.upcoming(when, currencies, hours=24 * 7, include_medium=medium)
    if upcoming:
        e = upcoming[0]
        return _reason("Economic calendar", f"Clear — next: {e['currency']} {e['title']} {e['time']:%a %H:%M} UTC",
                       "good")
    return _reason("Economic calendar", "Clear — no major news ahead this week", "good")


def decide(row, profile, instrument, news=None):
    """Decide for one entry candle. `row` is a mapping of the feature columns; `news` a NewsCalendar or None."""
    confirm = profile.clean_confirm()
    close_time = row.get("close_time")
    d = Decision(close_time=close_time)
    fmt = instrument.fmt

    if _missing(row, REQUIRED + [f"htf_{tf}" for tf in confirm]):
        d.headline = "WAIT — not enough price history yet"
        d.reasons = [_reason("Data", "Collecting history", "neutral")]
        return d

    sessions = active_sessions(close_time)
    if not market_open(close_time):
        d.headline = "WAIT — market closed"
        d.reasons = [_reason("Session", "Market closed (weekend)", "bad")]
        d.summary = "The forex market is closed. Signals resume when it reopens on Sunday evening (UTC)."
        return d

    # Direction from the higher timeframes.
    votes = {tf: int(row[f"htf_{tf}"]) for tf in confirm}
    ups = sum(1 for v in votes.values() if v == 1)
    downs = sum(1 for v in votes.values() if v == -1)
    n = len(confirm)
    needed = n if profile.require_all_confirm else n // 2 + 1
    if n == 0:
        direction = int(row["entry_trend"])
        agree = 1 if direction else 0
    elif ups >= needed:
        direction, agree = 1, ups
    elif downs >= needed:
        direction, agree = -1, downs
    else:
        direction, agree = 0, max(ups, downs)
    side = {1: "buy", -1: "sell"}.get(direction, "")
    bull = direction == 1

    def tone(ok):
        return "good" if ok else "bad"

    reasons, score, blockers = [], 0, []
    trend_word = {1: "Bullish", -1: "Bearish", 0: "Unclear"}

    # 1. Multi-timeframe agreement (30).
    if n:
        tf_list = ", ".join(tf.upper() for tf in confirm)
        if direction:
            score += round(30 * agree / n)
            label = "Yes" if agree == n else f"Partial ({agree} of {n})"
            reasons.append(_reason("Multi-timeframe confirmation", f"{label} — {tf_list} {trend_word[direction].lower()}",
                                   tone(agree == n)))
        else:
            detail = ", ".join(f"{tf.upper()} {trend_word[v].lower()}" for tf, v in votes.items())
            reasons.append(_reason("Multi-timeframe confirmation", f"No — {detail}", "bad"))
            blockers.append("the higher timeframes don't agree on a direction")
    else:
        score += 15 if direction else 0

    # 2. Entry-timeframe trend (15).
    et = int(row["entry_trend"])
    if direction:
        if et == direction:
            score += 15
        elif (row["close"] > row["ema_m"]) == bull:
            score += 8
    reasons.append(_reason(f"Trend ({profile.entry_timeframe})", trend_word[et],
                           tone(et == direction) if direction else "neutral"))

    # 3. Momentum (15).
    rsi, rsi_prev, hist, hist_prev = row["rsi"], row["rsi_prev"], row["hist"], row["hist_prev"]
    mom = 0
    if direction:
        rising = rsi > rsi_prev if bull else rsi < rsi_prev
        in_zone = (50 <= rsi <= 70) if bull else (30 <= rsi <= 50)
        near_zone = (45 <= rsi < 50) if bull else (50 < rsi <= 55)
        mom += 10 if (in_zone and rising) else 5 if (near_zone and rising) else 0
        macd_ok = hist > 0 if bull else hist < 0
        macd_build = hist > hist_prev if bull else hist < hist_prev
        mom += 5 if (macd_ok and macd_build) else 3 if macd_ok else 0
        score += mom
    rsi_dir = "rising" if rsi > rsi_prev else "falling"
    macd_word = "positive" if hist > 0 else "negative"
    reasons.append(_reason("Momentum", f"RSI {rsi:.0f} {rsi_dir}, MACD {macd_word}",
                           ("good" if mom >= 10 else "neutral" if mom else "bad") if direction else "neutral"))

    # 4. Entry setup (20).
    setup = ""
    if direction:
        atr = row["atr"]
        if bull:
            pullback = row["low5"] <= row["ema_f"] + 0.25 * atr and row["close"] > row["ema_f"] and row["close"] > row["open"]
            breakout = row["close"] > row["hh"]
        else:
            pullback = row["high5"] >= row["ema_f"] - 0.25 * atr and row["close"] < row["ema_f"] and row["close"] < row["open"]
            breakout = row["close"] < row["ll"]
        if pullback:
            setup, points = f"Pullback to the {profile.ema_fast} EMA, then a {'bullish' if bull else 'bearish'} close", 20
        elif breakout:
            setup, points = f"Breakout {'above' if bull else 'below'} the 20-candle {'high' if bull else 'low'}", 15
        else:
            points = 0
        score += points
    reasons.append(_reason("Technical setup", setup or "No entry setup yet", "good" if setup else "neutral"))
    if direction and not setup:
        blockers.append("there is no clean entry setup yet")

    # 5. Market condition (20).
    trending = row["adx"] >= profile.adx_threshold
    if direction and trending:
        score += 10
    reasons.append(_reason("Market condition", f"{'Trending' if trending else 'Ranging'} (ADX {row['adx']:.0f})",
                           tone(trending)))
    if direction and not trending:
        blockers.append("the market is ranging")

    pct = row.get("atr_pct")
    pct = 50.0 if pct is None or (isinstance(pct, float) and np.isnan(pct)) else pct
    if pct > 97:
        vol_word, vol_ok = "Spike (unusually high)", False
        blockers.append("volatility is spiking (often news)")
    elif pct < 20:
        vol_word, vol_ok = "Low (quiet market)", False
    else:
        vol_word, vol_ok = "Normal", True
    if direction and vol_ok:
        score += 5
    reasons.append(_reason("Volatility", vol_word, tone(vol_ok)))

    wanted = profile.sessions or []
    in_session = (not wanted) or any(s in wanted for s in sessions)
    session_text = " + ".join(SESSION_NAMES[s] for s in sessions) or "Between sessions"
    if direction and in_session:
        score += 5
    reasons.append(_reason("Session", f"{session_text} open" if sessions else session_text, tone(in_session)))
    if wanted and not in_session:
        blockers.append("it is outside your chosen trading sessions")

    if direction and ((bull and rsi > 75) or (not bull and rsi < 25)):
        blockers.append("the move is already stretched (RSI " + f"{rsi:.0f})")

    reasons.append(_calendar(news, close_time, profile, instrument, blockers))
    # Analyses that are not connected yet are shown honestly, never invented.
    reasons.append(_reason("Fundamental", "Not connected yet", "neutral"))

    d.score = int(min(100, max(0, score)))
    d.reasons = reasons
    d.direction = side

    if not direction or blockers or d.score < profile.min_score:
        why = blockers[0] if blockers else f"the setup score is {d.score}, below your minimum of {profile.min_score}"
        d.headline = f"WAIT — {why[0].upper()}{why[1:]}"
        d.summary = f"No trade for {instrument}: {why}."
        return d

    # Levels.
    entry, atr = float(row["close"]), float(row["atr"])
    rr = float(profile.risk_reward)
    risk = None
    if profile.sl_method == profile.SlMethod.SWING:
        swing = row.get("swing_low") if bull else row.get("swing_high")
        if swing is not None and not np.isnan(swing):
            stop = swing - 0.2 * atr if bull else swing + 0.2 * atr
            candidate = abs(entry - stop)
            if 1.0 * atr <= candidate <= 3 * atr:  # never tighter than one average candle
                risk = candidate
    if risk is None:
        risk = max(1.0, float(profile.atr_multiplier)) * atr
    d.decision = "trade"
    d.entry = entry
    d.stop_loss = entry - risk if bull else entry + risk
    d.take_profit = entry + rr * risk if bull else entry - rr * risk
    d.risk_reward = rr
    d.headline = f"{instrument} — {side.upper()}"
    pip = float(instrument.pip_size)
    d.summary = (
        f"{instrument} is {'rising' if bull else 'falling'} on "
        f"{', '.join(tf.upper() for tf in confirm) or profile.entry_timeframe} and the {profile.entry_timeframe} chart "
        f"shows: {setup.lower()}. Enter around {fmt(entry)}, stop loss {fmt(d.stop_loss)} "
        f"({risk / pip:.0f} pips), take profit {fmt(d.take_profit)} ({rr * risk / pip:.0f} pips, 1:{rr:g})."
    )
    return d


# ---------------------------------------------------------------------------
# Confidence from backtest results
# ---------------------------------------------------------------------------

MIN_SAMPLES = 15


def confidence_for(score, run):
    """Historical TP-before-SL rate for setups that scored like this one. Returns (percent, samples)."""
    if run is None or not run.buckets:
        return None, 0
    buckets = {int(k): v for k, v in run.buckets.items()}
    key = score // 10 * 10

    def rate(items):
        wins = sum(b.get("wins", 0) for b in items)
        losses = sum(b.get("losses", 0) for b in items)
        return wins, losses

    wins, losses = rate([buckets[key]] if key in buckets else [])
    if wins + losses < MIN_SAMPLES:  # widen to every setup scoring at least this bucket
        wins, losses = rate([b for k, b in buckets.items() if k >= key])
    if wins + losses < MIN_SAMPLES:
        wins, losses = run.wins, run.losses
    if wins + losses < MIN_SAMPLES:
        return None, wins + losses
    return round(100 * (wins + 1) / (wins + losses + 2)), wins + losses
