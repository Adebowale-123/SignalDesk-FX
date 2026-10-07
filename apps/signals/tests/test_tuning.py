from datetime import datetime, timedelta, timezone
from unittest import mock

import numpy as np
import pandas as pd
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.market.models import Candle, Instrument
from apps.news.services import NewsCalendar
from apps.signals.engine import build_features, decide
from apps.signals.models import Signal, StrategyProfile, TuningRun
from apps.signals.services import evaluate_outcome, result_in_r
from apps.signals.tuning import scan, simulate, summarise, tune_profile, variations

from .test_engine import ohlc, wave

UTC = timezone.utc


def features_for(profile, n=3000, seed=7):
    entry = ohlc(wave(n, seed=seed))
    hourly = entry.resample("60min").agg({"open": "first", "high": "max", "low": "min", "close": "last"})
    return entry, hourly, build_features(entry, {"1h": hourly}, profile)


def brute_force(features, profile, instrument):
    """The plain loop the fast replay must reproduce: decide() on every candle, one trade at a time."""
    rs, i, n = [], 0, len(features)
    highs, lows, closes, times = (features["high"].to_numpy(), features["low"].to_numpy(),
                                  features["close"].to_numpy(), features.index)
    while i < n - 1:
        row = features.iloc[i].to_dict()
        d = decide(row, profile, instrument)
        if not d.is_trade:
            i += 1
            continue
        bars = zip(times[i + 1:], highs[i + 1:], lows[i + 1:], closes[i + 1:])
        status, price, _, used = evaluate_outcome(d.direction, d.entry, d.stop_loss, d.take_profit, bars,
                                                  profile.max_hold_bars)
        if status is None:
            break
        spread = float(instrument.spread_pips) * float(instrument.pip_size)
        risk = abs(d.entry - d.stop_loss)
        rs.append(round(float(result_in_r(d.direction, d.entry, d.stop_loss, price)) - spread / risk, 6))
        i += used + 1
    return rs


class ReplayTests(TestCase):
    def setUp(self):
        self.instrument = Instrument(symbol="EUR/USD", pip_size=0.0001, spread_pips=1)
        self.profile = StrategyProfile(name="T", entry_timeframe="15m", confirm_timeframes=["1h"], min_score=55)

    def test_fast_replay_matches_engine(self):
        _, _, features = features_for(self.profile)
        data = scan(features, self.profile, self.instrument)
        for params in [self.profile.tuned_params(),
                       {**self.profile.tuned_params(), "sl_method": "atr", "atr_multiplier": 2.0, "risk_reward": 1.5,
                        "sessions": ["london"], "adx_threshold": 25, "min_score": 65}]:
            variant = StrategyProfile(name="V", entry_timeframe="15m", confirm_timeframes=["1h"])
            variant.apply_params(params)
            fast = [round(t["r"], 6) for t in simulate(data, params, variant.max_hold_bars)]
            self.assertEqual(fast, brute_force(features, variant, self.instrument))
            self.assertGreater(len(fast), 0)

    def test_fast_replay_matches_engine_with_new_setups_and_filters(self):
        _, _, features = features_for(self.profile, seed=11)
        rng = np.random.default_rng(3)  # synthetic fundamentals, sentiment and positioning
        n = len(features)
        features["m_fund"] = rng.choice([-1.5, -0.5, 0.0, 0.5, 1.5], n)
        features["m_sent"] = rng.choice([-1.0, 0.0, 1.0], n)
        features["m_mood"], features["m_dxy"], features["m_yld"] = 1.0, -1.0, 0.0
        features["m_oil"], features["m_vix"] = 0.0, 18.0
        features["cot_b"] = rng.uniform(0, 100, n)
        features["cot_q"] = rng.uniform(0, 100, n)
        data = scan(features, self.profile, self.instrument)
        cases = [
            {"setups": ["rsi_cross"]}, {"setups": ["macd_cross"], "macro_filter": "not_against"},
            {"setups": ["pullback", "breakout"], "macro_filter": "aligned"},
            {"setups": ["pullback", "breakout", "rsi_cross", "macd_cross"], "avoid_crowded": True, "min_score": 50},
            {"setups": ["breakout", "macd_cross"], "macro_filter": "aligned", "avoid_crowded": True,
             "sl_method": "atr", "atr_multiplier": 1.5, "risk_reward": 3.0},
        ]
        for case in cases:
            params = {**self.profile.tuned_params(), **case}
            variant = StrategyProfile(name="V", entry_timeframe="15m", confirm_timeframes=["1h"])
            variant.apply_params(params)
            fast = [round(t["r"], 6) for t in simulate(data, params, variant.max_hold_bars)]
            self.assertEqual(fast, brute_force(features, variant, self.instrument), case)
            self.assertGreater(len(fast), 0, case)

    def test_news_blocks_trades(self):
        _, _, features = features_for(self.profile)
        clean = scan(features, self.profile, self.instrument)
        first = features.index[clean.idx[0]] + timedelta(minutes=15)
        events = [{"title": "CPI", "currency": "USD", "time": first + timedelta(minutes=10), "impact": "high"}]
        news = NewsCalendar(events, covered_from=features.index[0], covered_until=features.index[-1] + timedelta(1))
        blocked = scan(features, self.profile, self.instrument, news)
        self.assertNotIn(clean.idx[0], list(blocked.idx))
        d = decide(features.iloc[clean.idx[0]].to_dict(), self.profile, self.instrument, news)
        self.assertIn("news", d.headline)

    def test_search_space_size(self):
        self.assertEqual(len(list(variations())), 3072)


def stats(trades, total_r, pf=1.5, avg_r=0.2):
    return {"trades": trades, "wins": 0, "losses": 0, "expired": 0, "win_rate": 40.0, "avg_r": avg_r,
            "profit_factor": pf, "total_r": total_r}


class TuneProfileTests(TestCase):
    def setUp(self):
        self.instrument = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, yahoo_symbol="EURUSD=X")
        self.profile = StrategyProfile.objects.create(name="T", entry_timeframe="15m", confirm_timeframes=["1h"])
        entry, hourly, _ = features_for(self.profile)
        for tf, frame in (("15m", entry), ("1h", hourly)):
            Candle.objects.bulk_create([Candle(instrument=self.instrument, timeframe=tf, time=t, open=r.open,
                                               high=r.high, low=r.low, close=r.close) for t, r in frame.iterrows()])
        self.better = {**self.profile.tuned_params(), "min_score": 85, "risk_reward": 3.0}

    def fake(self, best_test, current_test):
        return {"variations": 1024, "train_start": datetime(2026, 1, 1, tzinfo=UTC),
                "test_start": datetime(2026, 2, 1, tzinfo=UTC), "test_end": datetime(2026, 3, 1, tzinfo=UTC),
                "current_train": stats(100, -5), "current_test": current_test, "best": self.better,
                "best_train": stats(80, 20), "best_test": best_test}

    def test_adopts_only_when_unseen_data_agrees(self):
        with mock.patch("apps.signals.tuning.walk_forward", return_value=self.fake(stats(60, 8), stats(70, -4))):
            run = tune_profile(self.profile.pk, download=False)
        self.profile.refresh_from_db()
        self.assertEqual(run.outcome, TuningRun.Outcome.ADOPTED)
        self.assertEqual((self.profile.min_score, float(self.profile.risk_reward)), (85, 3.0))
        self.assertIn(("Minimum score", 65, 85), run.changes)
        self.assertIsNotNone(self.profile.last_tuned_at)
        self.assertTrue(self.profile.backtests.exists())  # confidence recalibrated

    def test_keeps_settings_when_best_fails_on_unseen_data(self):
        losing = stats(60, -3, pf=0.9, avg_r=-0.05)
        with mock.patch("apps.signals.tuning.walk_forward", return_value=self.fake(losing, stats(70, -4, 0.8, -0.1))):
            run = tune_profile(self.profile.pk, download=False)
        self.profile.refresh_from_db()
        self.assertEqual((run.outcome, run.has_edge), (TuningRun.Outcome.KEPT, False))
        self.assertEqual(self.profile.min_score, 65)
        self.assertIn("no proven edge", run.message)

    def test_too_few_unseen_trades_is_not_enough(self):
        with mock.patch("apps.signals.tuning.walk_forward", return_value=self.fake(stats(5, 9), stats(70, -4))):
            run = tune_profile(self.profile.pk, download=False)
        self.assertEqual(run.outcome, TuningRun.Outcome.KEPT)

    def test_real_run_completes(self):
        run = tune_profile(self.profile.pk, download=False)
        self.assertIn(run.outcome, (TuningRun.Outcome.ADOPTED, TuningRun.Outcome.KEPT))
        self.assertEqual(run.variations, 3072)

    def test_undo_restores_previous_settings(self):
        with mock.patch("apps.signals.tuning.walk_forward", return_value=self.fake(stats(60, 8), stats(70, -4))):
            run = tune_profile(self.profile.pk, download=False)
        admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(admin)
        with mock.patch("apps.core.background.start") as start:
            response = self.client.post(reverse("signals:undo_tuning", args=[run.pk]))
        self.assertEqual(response.status_code, 302)
        start.assert_called_once()
        self.profile.refresh_from_db()
        run.refresh_from_db()
        self.assertEqual((self.profile.min_score, float(self.profile.risk_reward), run.reverted), (65, 2.0, True))
        page = self.client.get(reverse("signals:backtests") + f"?profile={self.profile.pk}")
        self.assertContains(page, "Adopted, then undone")
