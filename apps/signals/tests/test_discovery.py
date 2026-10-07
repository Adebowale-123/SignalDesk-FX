from datetime import datetime, timezone
from unittest import mock

from django.test import TestCase

from apps.core.models import SiteSettings
from apps.market.models import Candle, Instrument
from apps.signals import discovery
from apps.signals.models import DiscoveryRun, StrategyProfile, TuningRun
from apps.signals.tuning import tune_profile

from .test_tuning import features_for

UTC = timezone.utc
PARAMS = {"setups": ["rsi_cross"], "macro_filter": "not_against", "avoid_crowded": True, "min_score": 70,
          "risk_reward": 2.0, "sl_method": "atr", "atr_multiplier": 1.5, "adx_threshold": 25,
          "sessions": ["london", "newyork"]}


def stats(trades, total_r, pf):
    return {"trades": trades, "wins": 0, "losses": 0, "expired": 0, "win_rate": 40.0, "avg_r": total_r / trades,
            "profit_factor": pf, "total_r": total_r}


def result(passing):
    test = stats(80, 12.0, 1.4) if passing else stats(80, -3.0, 0.9)
    return {"variations": 3888, "train_start": datetime(2026, 1, 1, tzinfo=UTC),
            "test_start": datetime(2026, 5, 1, tzinfo=UTC), "test_end": datetime(2026, 7, 1, tzinfo=UTC),
            "current_train": {}, "current_test": {}, "best": dict(PARAMS), "best_train": stats(150, 30.0, 1.5),
            "best_test": test}


class DiscoveryTests(TestCase):
    def setUp(self):
        self.instrument = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, yahoo_symbol="EURUSD=X")
        shape = StrategyProfile(name="x", entry_timeframe="15m", confirm_timeframes=["1h"])
        entry, hourly, _ = features_for(shape)
        for tf, frame in (("15m", entry), ("1h", hourly)):
            Candle.objects.bulk_create([Candle(instrument=self.instrument, timeframe=tf, time=t, open=r.open,
                                               high=r.high, low=r.low, close=r.close) for t, r in frame.iterrows()])
        self.family = {"entry": "15m", "confirm": ["1h"], "max_hold": 48, "label": "15m entry, 1H trend"}

    def run_discovery(self, passing):
        with mock.patch.object(discovery, "FAMILIES", [self.family]), \
                mock.patch.object(discovery, "walk_forward", return_value=result(passing)):
            return discovery.discover(download=False)

    def test_creates_and_switches_on_a_strategy_that_passes(self):
        run = self.run_discovery(True)
        profile = StrategyProfile.objects.get(discovered=True)
        self.assertTrue(profile.is_active)
        self.assertEqual((profile.setups, profile.macro_filter, profile.min_score), (["rsi_cross"], "not_against", 70))
        self.assertTrue(profile.name.startswith("Auto · 15M RSI turn"))
        self.assertEqual(list(run.created.all()), [profile])
        self.assertTrue(profile.backtests.exists())
        self.assertIsNotNone(SiteSettings.load().last_discovered_at)

    def test_does_not_duplicate_or_add_unproven(self):
        self.run_discovery(True)
        again = self.run_discovery(True)
        self.assertEqual(StrategyProfile.objects.filter(discovered=True).count(), 1)
        self.assertIn("already have", again.message)
        failed = self.run_discovery(False)
        self.assertIn("Nothing was added", failed.message)

    def test_respects_the_limit(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"max_discovered_active": 0})
        self.run_discovery(True)
        self.assertFalse(StrategyProfile.objects.get(discovered=True).is_active)

    def test_real_search_runs(self):
        with mock.patch.object(discovery, "FAMILIES", [self.family]), \
                mock.patch.object(discovery, "SPACE", {**discovery.SPACE, "min_score": [55], "risk_reward": [2.0],
                                                       "stop": [("atr", 1.5)]}):
            run = discovery.discover(download=False)
        self.assertTrue(run.finished)
        self.assertEqual(run.combinations, 6 * 3 * 2 * 2 * 2)

    def test_discovered_strategy_is_retired_when_it_stops_working(self):
        self.run_discovery(True)
        profile = StrategyProfile.objects.get(discovered=True)
        failing = {"variations": 3072, "train_start": datetime(2026, 1, 1, tzinfo=UTC),
                   "test_start": datetime(2026, 5, 1, tzinfo=UTC), "test_end": datetime(2026, 7, 1, tzinfo=UTC),
                   "current_train": stats(50, -5.0, 0.8), "current_test": stats(40, -4.0, 0.8), "best": None,
                   "best_train": {}, "best_test": {}}
        with mock.patch("apps.signals.tuning.walk_forward", return_value=failing):
            run = tune_profile(profile.pk, download=False)
        profile.refresh_from_db()
        self.assertEqual(run.outcome, TuningRun.Outcome.KEPT)
        self.assertFalse(profile.is_active)
        self.assertIn("switched off", run.message)
