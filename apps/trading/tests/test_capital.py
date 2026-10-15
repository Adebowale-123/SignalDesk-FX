from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.market.models import Candle, Instrument
from apps.signals.models import Signal, StrategyProfile
from apps.trading import capital_trader
from apps.trading.capital import BrokerError, CapitalBroker, epic_for
from apps.trading.capital_trader import size_for
from apps.trading.models import AutoTrader, BrokerTrade
from apps.trading.services import close_all, trade_cycle

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 13, 5, tzinfo=UTC)


class FakeCapital:
    environment = "demo"

    def __init__(self, nav=10000.0, bid=1.10000, offer=1.10010):
        self.nav, self.bid, self.offer = nav, bid, offer
        self.open, self.orders, self.closed, self.reasons = [], [], [], {}
        self.reject = None

    def summary(self):
        return {"currency": "USD", "balance": self.nav, "nav": self.nav, "account_id": "1", "account_type": "CFD"}

    def positions(self):
        return list(self.open)

    def market(self, epic):
        if epic == "USDJPY":
            return {"bid": 158.0, "offer": 158.02, "status": "TRADEABLE", "decimals": 3, "currency": "JPY",
                    "lot_size": 1, "min_size": 100, "size_step": 1, "min_stop": 0.01}
        return {"bid": self.bid, "offer": self.offer, "status": "TRADEABLE", "decimals": 5, "currency": "USD",
                "lot_size": 1, "min_size": 100, "size_step": 1, "min_stop": 0.0001}

    def open_position(self, epic, direction, size, stop_level, profit_level):
        if self.reject:
            raise BrokerError(self.reject)
        deal = f"D{len(self.orders) + 1}"
        level = self.offer if direction == "buy" else self.bid
        self.orders.append({"epic": epic, "direction": direction, "size": size, "stop": stop_level,
                            "profit": profit_level})
        self.open.append({"deal_id": deal, "epic": epic, "direction": direction.upper(), "size": size,
                          "level": level, "upl": 0.0, "created": NOW.strftime("%Y-%m-%dT%H:%M:%S"),
                          "stop": stop_level, "profit": profit_level})
        return {"deal_id": deal, "level": level, "size": size}

    def close_position(self, deal_id):
        self.closed.append(deal_id)
        self.open = [p for p in self.open if p["deal_id"] != deal_id]
        return {"level": self.bid}

    def close_reason(self, deal_id, since):
        return self.reasons.get(deal_id, ("closed", None))


class CapitalTestCase(TestCase):
    def setUp(self):
        self.trader = AutoTrader.load()
        self.trader.broker_type = AutoTrader.Broker.CAPITAL
        self.trader.capital_api_key, self.trader.capital_identifier = "key", "me@example.com"
        self.trader.capital_password, self.trader.enabled = "pw", True
        self.trader.save()
        self.eur = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, decimals=5)
        self.jpy = Instrument.objects.create(symbol="USD/JPY", pip_size=0.01, decimals=3)
        self.profile = StrategyProfile.objects.create(name="Intraday", entry_timeframe="15m",
                                                      confirm_timeframes=["1h"])
        self.broker = FakeCapital()
        for target in ("get_capital",):
            patcher = mock.patch.object(capital_trader, target, return_value=self.broker)
            patcher.start()
            self.addCleanup(patcher.stop)
        notify = mock.patch.object(capital_trader, "_notify")
        self.notify = notify.start()
        self.addCleanup(notify.stop)

    def signal(self, instrument=None, age=5, entry=1.10000, sl=1.09800, tp=1.10400, **kw):
        return Signal.objects.create(
            profile=self.profile, instrument=instrument or self.eur, direction=kw.pop("direction", "buy"),
            candle_time=NOW - timedelta(minutes=age), entry=entry, stop_loss=sl, take_profit=tp, risk_reward=2.0,
            score=80, reasons=[], **kw)


class SizeTests(TestCase):
    def test_size_risks_the_chosen_amount(self):
        self.assertEqual(size_for(100, 0.0020, 1.0, 1, 100, 1), 50000)
        self.assertEqual(size_for(100, 15.0, 1.0, 1, 0.1, 0.1), 6.6)  # gold: $15 stop -> 6.6 oz
        self.assertEqual(size_for(1, 0.0020, 1.0, 1, 1000, 1), 0.0)  # below the minimum


class CapitalCycleTests(CapitalTestCase):
    def test_opens_with_distances_from_the_broker_price(self):
        sig = self.signal()
        self.assertEqual(trade_cycle(now=NOW)["opened"], 1)
        order = self.broker.orders[0]
        self.assertEqual((order["epic"], order["direction"]), ("EURUSD", "buy"))
        self.assertAlmostEqual(order["stop"], 1.10010 - 0.0020)  # 20-pip stop from the actual offer
        self.assertAlmostEqual(order["profit"], 1.10010 + 0.0040)
        self.assertEqual(order["size"], 50000)  # 1% of 10,000 over a 20-pip stop
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.broker_trade_id, bt.environment), ("open", "D1", "capital-demo"))

    def test_jpy_pair_converts_the_risk_to_dollars(self):
        self.signal(instrument=self.jpy, entry=158.0, sl=157.70, tp=158.60)
        trade_cycle(now=NOW)
        size = self.broker.orders[0]["size"]
        self.assertAlmostEqual(size * 0.30 / 158.02, 100, delta=1)

    def test_take_profit_closes_then_waits_for_next_signal(self):
        sig = self.signal()
        trade_cycle(now=NOW)
        self.broker.open = []
        self.broker.reasons["D1"] = ("tp", 1.10410)
        self.assertEqual(trade_cycle(now=NOW + timedelta(hours=1))["closed"], 1)
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.close_reason, bt.realized_pl), ("closed", "tp", 200.0))
        nxt = self.signal(age=0)
        Signal.objects.filter(pk=nxt.pk).update(candle_time=NOW + timedelta(hours=1))
        trade_cycle(now=NOW + timedelta(hours=1, minutes=2))
        self.assertEqual(len(self.broker.orders), 2)

    def test_expired_signal_closes_the_position(self):
        sig = self.signal()
        trade_cycle(now=NOW)
        Signal.objects.filter(pk=sig.pk).update(status="expired")
        trade_cycle(now=NOW + timedelta(hours=12))
        self.assertEqual(self.broker.closed, ["D1"])
        self.assertEqual(BrokerTrade.objects.get(signal=sig).close_reason, "expired")

    def test_safety_checks(self):
        self.broker.open = [{"deal_id": "X", "epic": "EURUSD", "direction": "BUY", "size": 1, "level": 1.1,
                             "upl": 0, "created": None, "stop": None, "profit": None}]
        busy = self.signal()
        trade_cycle(now=NOW)
        self.assertIn("already open", BrokerTrade.objects.get(signal=busy).message)

    def test_too_late_and_rejections(self):
        Candle.objects.create(instrument=self.eur, timeframe="15m", time=NOW, open=1.1, high=1.102, low=1.1,
                              close=1.10100, volume=0)
        late = self.signal()
        trade_cycle(now=NOW)
        self.assertIn("too late", BrokerTrade.objects.get(signal=late).message)
        self.broker.reject = "Order rejected: INSUFFICIENT_FUNDS"
        sig = self.signal(instrument=self.jpy, entry=158.0, sl=157.70, tp=158.60)
        trade_cycle(now=NOW)
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual(bt.status, "rejected")

    def test_loss_limit_live_guard_and_off(self):
        trade_cycle(now=NOW)
        self.broker.nav = 9600.0
        self.signal(age=1)
        trade_cycle(now=NOW + timedelta(minutes=1))
        self.assertEqual(self.broker.orders, [])
        self.assertIsNotNone(AutoTrader.load().halted_until)
        AutoTrader.objects.update(halted_until=None, capital_environment="live")
        self.assertEqual(trade_cycle(now=NOW + timedelta(minutes=2))["status"], "live not allowed")

    def test_interrupted_order_is_recovered(self):
        sig = self.signal()
        self.broker.open = [{"deal_id": "Z9", "epic": "EURUSD", "direction": "BUY", "size": 50000, "level": 1.1001,
                             "upl": 0, "created": NOW.strftime("%Y-%m-%dT%H:%M:%S"), "stop": None, "profit": None}]
        bt = BrokerTrade.objects.create(signal=sig, instrument=self.eur, direction="buy", status="pending",
                                        environment="capital-demo", issued_at=NOW - timedelta(minutes=1))
        trade_cycle(now=NOW + timedelta(minutes=5))
        bt.refresh_from_db()
        self.assertEqual((bt.status, bt.broker_trade_id), ("open", "Z9"))

    def test_oversized_position_is_blocked(self):
        big = self.broker.market

        def lots_market(epic):
            m = big(epic)
            return {**m, "lot_size": 100000, "min_stop": 0}  # sizes in lots of 100,000: size shrinks to match...
        self.broker.market = lots_market
        sig = self.signal(entry=1.10000, sl=1.09999, tp=1.10002)  # ...but a 0.1-pip stop needs huge exposure
        trade_cycle(now=NOW)
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual(bt.status, "skipped")
        self.assertIn("safety", bt.message)
        self.assertEqual(self.broker.orders, [])

    def test_close_all(self):
        self.signal()
        trade_cycle(now=NOW)
        self.assertEqual(close_all(), (1, []))
        self.assertEqual(self.broker.open, [])

    def test_dashboard(self):
        admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(admin)
        page = self.client.get(reverse("trading:dashboard"))
        self.assertContains(page, "Capital.com login email")
        self.assertContains(page, "Demo account")
        self.assertNotContains(page, "MetaTrader 5 bridge")


def response(status, data=None, headers=None):
    r = mock.Mock(status_code=status, headers=headers or {}, text="")
    r.json.return_value = data or {}
    return r


class CapitalParsingTests(TestCase):
    """The client against Capital.com's documented response shapes."""

    def test_login_order_and_confirm(self):
        broker = CapitalBroker("key", "me@example.com", "pw", "demo")
        login = response(200, {}, {"CST": "c", "X-SECURITY-TOKEN": "t"})
        placed = response(200, {"dealReference": "o_123"})
        confirmed = response(200, {"dealStatus": "ACCEPTED", "level": 1.10012, "size": 1000, "dealId": "x",
                                   "affectedDeals": [{"dealId": "pos-1", "status": "OPENED"}]})
        with mock.patch("requests.post", return_value=login) as post, \
                mock.patch("requests.request", side_effect=[placed, confirmed]) as req:
            fill = broker.open_position("EURUSD", "buy", 1000, 1.098, 1.104)
        self.assertEqual(fill, {"deal_id": "pos-1", "level": 1.10012, "size": 1000.0})
        self.assertEqual(post.call_args.kwargs["headers"]["X-CAP-API-KEY"], "key")
        body = req.call_args_list[0].kwargs["json"]
        self.assertEqual((body["direction"], body["stopLevel"], body["profitLevel"]), ("BUY", 1.098, 1.104))
        self.assertEqual(req.call_args_list[0].kwargs["headers"]["CST"], "c")

    def test_rejection_and_expired_session(self):
        broker = CapitalBroker("key", "me@example.com", "pw")
        broker.tokens = {"CST": "old", "X-SECURITY-TOKEN": "old"}
        rejected = response(200, {"dealStatus": "REJECTED", "reason": "MARKET_CLOSED"})
        with mock.patch("requests.request", side_effect=[response(401), rejected]), \
                mock.patch("requests.post", return_value=response(200, {}, {"CST": "n", "X-SECURITY-TOKEN": "n"})):
            with self.assertRaisesMessage(BrokerError, "MARKET_CLOSED"):
                broker.confirm("o_1")
        self.assertEqual(broker.tokens["CST"], "n")

    def test_market_rules_with_units(self):
        broker = CapitalBroker("key", "me@example.com", "pw")
        broker.tokens = {"CST": "c", "X-SECURITY-TOKEN": "t"}
        data = {"instrument": {"epic": "GOLD", "currency": "USD", "lotSize": 1},
                "dealingRules": {"minDealSize": {"unit": "POINTS", "value": 0.1},
                                 "minStopOrProfitDistance": {"unit": "PERCENTAGE", "value": 0.1}},
                "snapshot": {"bid": 2400.0, "offer": 2400.4, "decimalPlacesFactor": 2, "marketStatus": "TRADEABLE"}}
        with mock.patch("requests.request", return_value=response(200, data)):
            m = broker.market("GOLD")
        self.assertEqual((m["min_size"], m["decimals"]), (0.1, 2))
        self.assertAlmostEqual(m["min_stop"], 2.4002, places=3)  # 0.1% of the price

    def test_accounts_and_epics(self):
        broker = CapitalBroker("key", "me@example.com", "pw")
        broker.tokens = {"CST": "c", "X-SECURITY-TOKEN": "t"}
        data = {"accounts": [{"accountId": "A", "preferred": True, "currency": "USD",
                              "balance": {"balance": 10000, "profitLoss": -25.5, "available": 9000}}]}
        with mock.patch("requests.request", return_value=response(200, data)):
            s = broker.summary()
        self.assertEqual((s["currency"], s["balance"], s["nav"]), ("USD", 10000.0, 9974.5))
        self.assertEqual(epic_for(Instrument(symbol="XAU/USD")), "GOLD")
        self.assertEqual(epic_for(Instrument(symbol="GBP/USD")), "GBPUSD")
