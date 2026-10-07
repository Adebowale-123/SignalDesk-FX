from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
from django.test import TestCase

from apps.market.models import Candle, Instrument
from apps.signals import indicators as ind
from apps.signals.engine import active_sessions, build_features, confidence_for, decide, market_open
from apps.signals.models import BacktestRun, Signal, StrategyProfile
from apps.signals.services import backtest, evaluate_outcome, result_in_r

UTC = timezone.utc


def ohlc(closes, start=datetime(2026, 1, 5, tzinfo=UTC), minutes=15):
    close = pd.Series(closes, dtype=float)
    index = pd.date_range(start, periods=len(close), freq=f"{minutes}min")
    return pd.DataFrame({"open": close.shift(1).fillna(close.iloc[0]).values, "high": (close + 0.0004).values,
                         "low": (close - 0.0004).values, "close": close.values, "volume": 0.0}, index=index)


def wave(n, seed=1):
    """A trending series with pullbacks and noise, so every rule gets exercised."""
    rng = np.random.default_rng(seed)
    t = np.arange(n)
    return 1.10 + 0.00002 * t + 0.003 * np.sin(t / 40) + rng.normal(0, 0.0003, n).cumsum() * 0.3


class IndicatorTests(TestCase):
    def test_indicators_are_causal(self):
        """Changing a future candle must never change an earlier indicator value."""
        df = ohlc(wave(400))
        changed = df.copy()
        changed.iloc[-1, changed.columns.get_loc("close")] *= 1.05
        changed.iloc[-1, changed.columns.get_loc("high")] *= 1.05
        for fn in (lambda d: ind.ema(d["close"], 20), lambda d: ind.rsi(d["close"]), ind.atr, ind.adx,
                   lambda d: ind.last_swing(d["low"], "low")):
            pd.testing.assert_series_equal(fn(df).iloc[:-1], fn(changed).iloc[:-1])

    def test_rsi_bounds(self):
        values = ind.rsi(ohlc(wave(300))["close"]).dropna()
        self.assertTrue(((values >= 0) & (values <= 100)).all())


class MarketHoursTests(TestCase):
    def test_weekend_closed(self):
        self.assertFalse(market_open(datetime(2026, 10, 10, 12, tzinfo=UTC)))  # Saturday
        self.assertFalse(market_open(datetime(2026, 10, 9, 21, tzinfo=UTC)))  # Friday 21:00
        self.assertFalse(market_open(datetime(2026, 10, 11, 20, tzinfo=UTC)))  # Sunday 20:00
        self.assertTrue(market_open(datetime(2026, 10, 11, 21, tzinfo=UTC)))  # Sunday 21:00

    def test_sessions(self):
        self.assertEqual(active_sessions(datetime(2026, 10, 7, 13, tzinfo=UTC)), ["london", "newyork"])
        self.assertEqual(active_sessions(datetime(2026, 10, 10, 13, tzinfo=UTC)), [])


class OutcomeTests(TestCase):
    T = datetime(2026, 1, 5, tzinfo=UTC)

    def test_stop_counted_first_when_both_hit(self):
        bars = [(self.T, 1.1100, 1.0900, 1.1000)]
        self.assertEqual(evaluate_outcome("buy", 1.1000, 1.0950, 1.1100, bars, 10)[0], Signal.Status.SL)

    def test_take_profit_and_expiry(self):
        bars = [(self.T, 1.1010, 1.0990, 1.1005), (self.T, 1.1101, 1.1000, 1.1090)]
        self.assertEqual(evaluate_outcome("buy", 1.1000, 1.0950, 1.1100, bars, 10)[0], Signal.Status.TP)
        self.assertEqual(evaluate_outcome("buy", 1.1000, 1.0950, 1.1200, bars, 2)[0], Signal.Status.EXPIRED)
        self.assertIsNone(evaluate_outcome("sell", 1.1000, 1.1050, 1.0900, bars[:1], 10)[0])

    def test_result_in_r(self):
        self.assertAlmostEqual(result_in_r("buy", 1.10, 1.09, 1.12), 2.0)
        self.assertAlmostEqual(result_in_r("sell", 1.10, 1.11, 1.11), -1.0)


class ConfidenceTests(TestCase):
    def run_with(self, buckets, wins=0, losses=0):
        return BacktestRun(buckets=buckets, wins=wins, losses=losses)

    def test_uncalibrated(self):
        self.assertEqual(confidence_for(70, None), (None, 0))
        self.assertEqual(confidence_for(70, self.run_with({}, 3, 4)), (None, 0))

    def test_uses_own_bucket_then_widens(self):
        run = self.run_with({"70": {"wins": 12, "losses": 8}, "80": {"wins": 2, "losses": 2}}, 14, 10)
        self.assertEqual(confidence_for(75, run), (round(100 * 13 / 22), 20))
        # 80 bucket has only 4 samples -> falls back to the whole run
        self.assertEqual(confidence_for(85, run), (round(100 * 15 / 26), 24))


class FeatureTests(TestCase):
    def setUp(self):
        self.profile = StrategyProfile.objects.create(name="T", entry_timeframe="15m", confirm_timeframes=["1h"])

    def test_higher_timeframe_has_no_lookahead(self):
        entry = ohlc(wave(800))
        hourly = entry.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        features = build_features(entry, {"1h": hourly}, self.profile)
        trend = ind.trend_series(hourly, self.profile.ema_mid, self.profile.ema_slow)
        for time, row in features.iloc[::37].iterrows():
            closed = trend[trend.index + timedelta(hours=1) <= row["close_time"]].dropna()
            expected = closed.iloc[-1] if len(closed) else np.nan
            if np.isnan(expected):
                self.assertTrue(np.isnan(row["htf_1h"]))
            else:
                self.assertEqual(row["htf_1h"], expected)

    def test_decide_waits_when_market_closed(self):
        instrument = Instrument(symbol="EUR/USD", pip_size=0.0001)
        entry = ohlc(wave(2000), start=datetime(2026, 9, 14, tzinfo=UTC))  # runs into a weekend
        hourly = entry.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        features = build_features(entry, {"1h": hourly}, self.profile)
        row = features[features["close_time"] == datetime(2026, 10, 3, 12, tzinfo=UTC)].iloc[0].to_dict()
        decision = decide(row, self.profile, instrument)
        self.assertFalse(decision.is_trade)
        self.assertIn("market closed", decision.headline)

    def test_trade_decisions_have_sane_levels(self):
        instrument = Instrument(symbol="EUR/USD", pip_size=0.0001)
        entry = ohlc(wave(3000, seed=7))
        hourly = entry.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        features = build_features(entry, {"1h": hourly}, self.profile)
        trades = 0
        for i in range(300, len(features)):
            row = features.iloc[i].to_dict()
            d = decide(row, self.profile, instrument)
            if not d.is_trade:
                continue
            trades += 1
            risk = abs(d.entry - d.stop_loss)
            self.assertGreaterEqual(risk, row["atr"] * 0.999)  # never tighter than one ATR
            self.assertAlmostEqual(abs(d.take_profit - d.entry), risk * float(self.profile.risk_reward), places=6)
            if d.direction == "buy":
                self.assertLess(d.stop_loss, d.entry)
            else:
                self.assertGreater(d.stop_loss, d.entry)
        self.assertGreater(trades, 0)


class BacktestTests(TestCase):
    def test_backtest_on_stored_candles(self):
        instrument = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, yahoo_symbol="EURUSD=X")
        profile = StrategyProfile.objects.create(name="T", entry_timeframe="15m", confirm_timeframes=["1h"])
        profile.instruments.add(instrument)
        entry = ohlc(wave(3000, seed=7))
        hourly = entry.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
        for tf, frame in (("15m", entry), ("1h", hourly)):
            Candle.objects.bulk_create([Candle(instrument=instrument, timeframe=tf, time=t, open=r.open, high=r.high,
                                               low=r.low, close=r.close) for t, r in frame.iterrows()])
        run = backtest(profile, download=False)
        self.assertGreater(run.trades, 0)
        self.assertEqual(run.trades, run.wins + run.losses + run.expired)
        self.assertNotIn("error", run.per_pair["EUR/USD"])

    def test_missing_higher_timeframe_is_reported(self):
        instrument = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, yahoo_symbol="EURUSD=X")
        profile = StrategyProfile.objects.create(name="T", entry_timeframe="15m", confirm_timeframes=["1d"])
        profile.instruments.add(instrument)
        entry = ohlc(wave(1000))
        Candle.objects.bulk_create([Candle(instrument=instrument, timeframe="15m", time=t, open=r.open, high=r.high,
                                           low=r.low, close=r.close) for t, r in entry.iterrows()])
        run = backtest(profile, download=False)
        self.assertEqual(run.trades, 0)
        self.assertIn("1d", run.per_pair["EUR/USD"]["error"])
