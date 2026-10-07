"""Technical indicators on pandas OHLC frames. All are causal: a value at a candle uses only that candle and earlier ones."""

import numpy as np
import pandas as pd


def ema(series, length):
    return series.ewm(span=length, adjust=False, min_periods=length).mean()


def rsi(close, length=14):
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    rs = gain / loss.replace(0, np.nan)
    return (100 - 100 / (1 + rs)).fillna(100.0).where(loss.notna())


def macd_histogram(close, fast=12, slow=26, signal=9):
    line = ema(close, fast) - ema(close, slow)
    return line - line.ewm(span=signal, adjust=False, min_periods=signal).mean()


def true_range(df):
    prev_close = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
                     axis=1).max(axis=1)


def atr(df, length=14):
    return true_range(df).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def adx(df, length=14):
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    tr = true_range(df).ewm(alpha=1 / length, adjust=False, min_periods=length).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean() / tr
    minus_di = 100 * minus_dm.ewm(alpha=1 / length, adjust=False, min_periods=length).mean() / tr
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / length, adjust=False, min_periods=length).mean()


def last_swing(series, kind, k=2):
    """Most recent confirmed swing low/high: a pivot is only known k candles after it forms."""
    window = 2 * k + 1
    if kind == "low":
        pivot = series == series.rolling(window, center=True).min()
    else:
        pivot = series == series.rolling(window, center=True).max()
    values = series.where(pivot)
    return values.shift(k).ffill()


def rolling_percentile(series, window=100):
    """Where the latest value sits within the last `window` values (0-100)."""
    return series.rolling(window, min_periods=window // 2).apply(lambda w: (w[:-1] < w[-1]).mean() * 100, raw=True)


def trend_series(df, fast, slow, slope_bars=5):
    """+1 uptrend, -1 downtrend, 0 unclear: price vs two EMAs and the slope of the faster one."""
    f, s = ema(df["close"], fast), ema(df["close"], slow)
    slope = f - f.shift(slope_bars)
    up = (df["close"] > f) & (f > s) & (slope > 0)
    down = (df["close"] < f) & (f < s) & (slope < 0)
    return pd.Series(np.select([up, down], [1, -1], 0), index=df.index).where(s.notna())
