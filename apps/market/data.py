"""Store candles and load them as pandas DataFrames for analysis."""

from datetime import datetime, timedelta, timezone

import pandas as pd

from .models import TIMEFRAMES, Candle

RESAMPLE_SOURCE = {"4h": "1h", "1h": "15m", "30m": "15m", "15m": "5m"}


def source_timeframe(provider, timeframe):
    """The timeframe actually downloaded for `timeframe` (itself, or a smaller one to resample)."""
    tf = timeframe
    while tf not in provider.native:
        tf = RESAMPLE_SOURCE[tf]
    return tf


def needs_update(instrument, timeframe, now=None):
    """True when a newer closed candle should exist than the last one stored."""
    now = now or datetime.now(timezone.utc)
    last = Candle.objects.filter(instrument=instrument, timeframe=timeframe).order_by("-time").values_list(
        "time", flat=True).first()
    if last is None:
        return True
    return last + timedelta(minutes=2 * TIMEFRAMES[timeframe]) <= now


def update_candles(instrument, timeframe, provider, *, history=False, force=False):
    """Download and store closed candles. Returns how many were new."""
    tf = source_timeframe(provider, timeframe)
    if not (history or force or needs_update(instrument, tf)):
        return 0
    if not Candle.objects.filter(instrument=instrument, timeframe=tf).exists():
        history = True  # first download: get enough past data for the long averages
    candles = provider.fetch(instrument, tf, history=history)
    if not candles:
        return 0
    existing = set(Candle.objects.filter(instrument=instrument, timeframe=tf, time__gte=candles[0]["time"])
                   .values_list("time", flat=True))
    new = [Candle(instrument=instrument, timeframe=tf, **c) for c in candles if c["time"] not in existing]
    Candle.objects.bulk_create(new, ignore_conflicts=True, batch_size=1000)
    return len(new)


def _frame(rows):
    df = pd.DataFrame.from_records(rows, columns=["time", "open", "high", "low", "close", "volume"])
    if df.empty:
        return df
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time").sort_index()


def load_frame(instrument, timeframe, provider, *, limit=3000):
    """Closed candles for `timeframe`, resampled from a smaller timeframe when needed."""
    tf = source_timeframe(provider, timeframe)
    factor = TIMEFRAMES[timeframe] // TIMEFRAMES[tf]
    rows = list(Candle.objects.filter(instrument=instrument, timeframe=tf).order_by("-time")
                .values_list("time", "open", "high", "low", "close", "volume")[: limit * factor])
    df = _frame(list(reversed(rows)))
    if df.empty or tf == timeframe:
        return df
    rule = f"{TIMEFRAMES[timeframe]}min"
    agg = df.resample(rule, label="left", closed="left", origin="epoch").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    counts = df["close"].resample(rule, label="left", closed="left", origin="epoch").count()
    agg = agg[counts > 0].dropna()
    # Drop the last bucket if it hasn't finished yet.
    last_source_close = df.index[-1] + timedelta(minutes=TIMEFRAMES[tf])
    if len(agg) and agg.index[-1] + timedelta(minutes=TIMEFRAMES[timeframe]) > last_source_close:
        agg = agg.iloc[:-1]
    return agg


def latest_price(instrument):
    candle = Candle.objects.filter(instrument=instrument).order_by("-time").only("close", "time").first()
    return candle.close if candle else None
