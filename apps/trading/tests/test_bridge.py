import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.market.models import Candle, Instrument
from apps.signals.models import Signal, StrategyProfile
from apps.trading import bridge
from apps.trading.models import AutoTrader, BrokerTrade

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 13, 5, tzinfo=UTC)

spec = importlib.util.spec_from_file_location("signaldesk_bridge",
                                              Path(settings.BASE_DIR) / "bridge" / "signaldesk_bridge.py")
program = importlib.util.module_from_spec(spec)
spec.loader.exec_module(program)


class BridgeProgramTests(TestCase):
    """The parts of the Windows program that don't need MetaTrader."""

    def test_symbol_names_with_broker_suffixes(self):
        names = ["EURUSD.m", "EURUSDx", "GBPUSD", "XAUUSD", "XAUUSD.m"]
        self.assertEqual(program.resolve_symbol("EURUSD", names), "EURUSDx")
        self.assertEqual(program.resolve_symbol("XAUUSD", names), "XAUUSD")
        self.assertIsNone(program.resolve_symbol("USDJPY", names))

    def test_lot_size_risks_the_chosen_amount(self):
        # EUR/USD: 20-pip stop, $1 per 0.00001 per lot -> $200 per lot; risk $100 -> 0.5 lots
        self.assertEqual(program.lot_size(100, 0.0020, 0.00001, 1.0, 0.01, 0.01, 100), 0.5)
        # Gold: $15 stop, $1 per 0.01 per lot -> $1,500 per lot; risk $100 -> 0.06 lots (rounded down)
        self.assertEqual(program.lot_size(100, 15.0, 0.01, 1.0, 0.01, 0.01, 100), 0.06)
        # Too small an account for the minimum lot
        self.assertEqual(program.lot_size(5, 15.0, 0.01, 1.0, 0.01, 0.01, 100), 0.0)

    def test_levels_keep_the_signal_distances(self):
        self.assertEqual(program.levels("buy", 2400.50, 15.0, 30.0, 2), (2385.5, 2430.5))
        self.assertEqual(program.levels("sell", 1.10000, 0.002, 0.004, 5), (1.102, 1.096))


class BridgeTestCase(TestCase):
    def setUp(self):
        self.trader = AutoTrader.load()
        self.trader.enabled = True
        self.trader.broker_type = AutoTrader.Broker.MT5
        self.trader.save()
        self.eur = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, decimals=5)
        self.profile = StrategyProfile.objects.create(name="Intraday", entry_timeframe="15m",
                                                      confirm_timeframes=["1h"])
        notify = mock.patch("apps.trading.bridge._notify")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def signal(self, instrument=None, age=5, **kw):
        return Signal.objects.create(
            profile=self.profile, instrument=instrument or self.eur, direction=kw.pop("direction", "buy"),
            candle_time=NOW - timedelta(minutes=age), entry=1.10000, stop_loss=1.09800, take_profit=1.10400,
            risk_reward=2.0, score=80, reasons=[], **kw)

    def payload(self, positions=None, closed=None, results=None, mode="demo"):
        return {"terminal": {"company": "ForexTime", "server": "FXTM-Demo", "login": 123, "mode": mode,
                             "algo_trading": True},
                "account": {"currency": "USD", "balance": 10000, "equity": 10000},
                "positions": positions or [], "closed": closed or [], "results": results or []}

    def check_in(self, now=NOW, **kw):
        return bridge.exchange(self.payload(**kw), now=now)


class ExchangeTests(BridgeTestCase):
    def test_fresh_signal_becomes_an_order_with_distances(self):
        sig = self.signal()
        reply = self.check_in()
        self.assertEqual(len(reply["orders"]), 1)
        order = reply["orders"][0]
        self.assertEqual((order["symbol"], order["side"], order["risk_percent"]), ("EURUSD", "buy", 1.0))
        self.assertAlmostEqual(order["sl_distance"], 0.0020)
        self.assertAlmostEqual(order["tp_distance"], 0.0040)
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.client_id), ("pending", order["id"]))
        self.assertLessEqual(len(order["id"]), 31)

    def test_orders_are_not_duplicated_and_reissued_if_unconfirmed(self):
        self.signal()
        first = self.check_in()["orders"]
        self.assertEqual(self.check_in(now=NOW + timedelta(seconds=20))["orders"], [])
        again = self.check_in(now=NOW + timedelta(seconds=90))["orders"]
        self.assertEqual([o["id"] for o in again], [first[0]["id"]])
        BrokerTrade.objects.update(issued_at=NOW - timedelta(minutes=11))
        self.check_in(now=NOW + timedelta(minutes=1))
        self.assertEqual(BrokerTrade.objects.get().status, "error")

    def test_full_lifecycle_open_then_take_profit(self):
        sig = self.signal()
        order = self.check_in()["orders"][0]
        opened = {"id": order["id"], "status": "opened", "ticket": 5551, "price": 1.10012, "lots": 0.47,
                  "sl": 1.09812, "tp": 1.10412, "risk": 94.0, "message": "Opened 0.47 lots"}
        self.check_in(now=NOW + timedelta(seconds=21), results=[opened],
                      positions=[{"ticket": 5551, "symbol": "EURUSD", "comment": order["id"], "ours": True}])
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.broker_trade_id, bt.lots, bt.stop_loss), ("open", "5551", 0.47, 1.09812))
        self.check_in(now=NOW + timedelta(hours=2),
                      closed=[{"ticket": 5551, "price": 1.10412, "profit": 188.0, "reason": "tp",
                               "comment": order["id"]}])
        bt.refresh_from_db()
        self.assertEqual((bt.status, bt.close_reason, bt.realized_pl), ("closed", "tp", 188.0))
        self.assertEqual(self.notify.call_count, 2)  # opened, closed

    def test_bridge_restart_is_recovered_from_mt5_records(self):
        sig = self.signal()
        order = self.check_in()["orders"][0]
        self.check_in(now=NOW + timedelta(seconds=40),
                      positions=[{"ticket": 9, "symbol": "EURUSD", "comment": order["id"], "open_price": 1.1001,
                                  "volume": 0.5, "ours": True}])
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.broker_trade_id), ("open", "9"))

    def test_skip_rules(self):
        busy = self.signal()
        self.check_in(positions=[{"ticket": 1, "symbol": "EURUSD.m", "comment": "manual", "ours": False}])
        self.assertIn("already open", BrokerTrade.objects.get(signal=busy).message)

    def test_too_late_uses_the_latest_price(self):
        Candle.objects.create(instrument=self.eur, timeframe="15m", time=NOW, open=1.1, high=1.101, low=1.1,
                              close=1.10100, volume=0)
        sig = self.signal()
        self.check_in()
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual(bt.status, "skipped")
        self.assertIn("too late", bt.message)

    def test_real_account_needs_permission(self):
        self.signal()
        reply = self.check_in(mode="real")
        self.assertEqual(reply["orders"], [])
        self.assertIn("REAL", reply["note"])
        self.trader.refresh_from_db()
        self.trader.allow_live = True
        self.trader.save()
        self.assertEqual(len(self.check_in(mode="real")["orders"]), 1)

    def test_switched_off_sends_nothing(self):
        self.trader.enabled = False
        self.trader.save()
        self.signal()
        self.assertEqual(self.check_in()["orders"], [])

    def test_expired_signal_is_closed_and_close_all(self):
        sig = self.signal()
        order = self.check_in()["orders"][0]
        pos = [{"ticket": 77, "symbol": "EURUSD", "comment": order["id"], "ours": True}]
        self.check_in(now=NOW + timedelta(seconds=21), positions=pos,
                      results=[{"id": order["id"], "status": "opened", "ticket": 77, "price": 1.1, "lots": 0.5}])
        Signal.objects.filter(pk=sig.pk).update(status="expired")
        self.assertEqual(self.check_in(now=NOW + timedelta(hours=12), positions=pos)["close"], ["77"])
        self.trader.refresh_from_db()
        self.trader.close_all_requested = True
        self.trader.save()
        self.assertTrue(self.check_in(now=NOW + timedelta(hours=12), positions=pos)["close_all"])
        self.assertFalse(self.check_in(now=NOW + timedelta(hours=12))["close_all"])  # all gone: flag cleared
        self.assertFalse(AutoTrader.load().close_all_requested)

    def test_daily_loss_limit_stops_new_orders(self):
        self.check_in()  # day starts at 10,000
        self.signal()
        payload = self.payload()
        payload["account"]["equity"] = 9600  # -4%
        reply = bridge.exchange(payload, now=NOW + timedelta(minutes=1))
        self.assertEqual(reply["orders"], [])
        self.assertIsNotNone(AutoTrader.load().halted_until)


class BridgeApiTests(BridgeTestCase):
    def test_needs_the_bridge_key(self):
        url = reverse("bridge_api")
        body = json.dumps(self.payload())
        self.assertEqual(self.client.post(url, body, content_type="application/json").status_code, 403)
        self.assertEqual(self.client.post(url, body, content_type="application/json",
                                          HTTP_X_BRIDGE_KEY="wrong").status_code, 403)
        ok = self.client.post(url, body, content_type="application/json", HTTP_X_BRIDGE_KEY=self.trader.bridge_key)
        self.assertEqual(ok.status_code, 200)
        self.assertTrue(ok.json()["ok"])
        self.assertEqual(AutoTrader.load().bridge_info["server"], "FXTM-Demo")

    def test_download_fills_in_address_and_key(self):
        admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(admin)
        text = self.client.get(reverse("trading:download_bridge")).content.decode()
        self.assertIn(f'BRIDGE_KEY = "{self.trader.bridge_key}"', text)
        self.assertIn('SITE_URL = "http://testserver"', text)
        page = self.client.get(reverse("trading:dashboard"))
        self.assertContains(page, "MetaTrader 5 bridge")
        self.assertContains(page, "Offline")
