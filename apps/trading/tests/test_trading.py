from datetime import datetime, timedelta, timezone
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from apps.core.models import SiteSettings
from apps.market.models import Instrument
from apps.signals.models import Signal, StrategyProfile
from apps.trading import services
from apps.trading.models import AutoTrader, BrokerTrade
from apps.trading.oanda import BrokerError, OandaBroker
from apps.trading.services import position_units, trade_cycle

UTC = timezone.utc
NOW = datetime(2026, 10, 7, 13, 5, tzinfo=UTC)  # Wednesday, London/New York overlap


class FakeBroker:
    """Stands in for OANDA: same methods, no network."""

    environment = "practice"

    def __init__(self, nav=10000.0, bid=1.10000, ask=1.10010, open_trades=None):
        self.nav, self.bid, self.ask = nav, bid, ask
        self.orders, self.closed = [], []
        self.trades = {}
        self.existing = list(open_trades or [])
        self.reject = None

    def summary(self):
        return {"currency": "USD", "balance": self.nav, "nav": self.nav,
                "open_trades": len(self.existing) + sum(t["state"] == "OPEN" for t in self.trades.values()),
                "margin_available": self.nav, "unrealized_pl": 0.0}

    def instrument_info(self, name):
        return {"precision": 5, "units_precision": 0, "min_units": 1.0}

    def price(self, name):
        return {"bid": self.bid, "ask": self.ask, "tradeable": True, "loss_factor": 1.0}

    def open_trades(self):
        return [{"id": "x", "instrument": i, "units": 1} for i in self.existing] + \
            [{"id": k, "instrument": t["instrument"], "units": t["units"]} for k, t in self.trades.items()
             if t["state"] == "OPEN"]

    def market_order(self, name, units, stop_loss, take_profit, precision, client_id, comment=""):
        if self.reject:
            raise BrokerError(self.reject)
        trade_id = str(100 + len(self.orders))
        self.orders.append({"name": name, "units": units, "sl": stop_loss, "tp": take_profit, "client_id": client_id})
        price = self.ask if units > 0 else self.bid
        self.trades[trade_id] = {"instrument": name, "units": units, "state": "OPEN", "price": price,
                                 "client_id": client_id}
        return {"trade_id": trade_id, "units": float(units), "price": price}

    def trade(self, trade_id):
        if trade_id.startswith("@"):
            match = [(k, t) for k, t in self.trades.items() if t["client_id"] == trade_id[1:]]
            if not match:
                raise BrokerError("OANDA 404: trade not found")
            trade_id = match[0][0]
        t = self.trades[trade_id]
        return {"id": trade_id, "state": t["state"], "realized_pl": t.get("pl", 0.0), "unrealized_pl": 0.0,
                "close_price": t.get("close"), "close_time": None, "reason": t.get("reason", "closed"),
                "price": t["price"], "units": t["units"]}

    def close_trade(self, trade_id):
        self.closed.append(trade_id)
        self.trades[trade_id].update(state="CLOSED", close=self.bid, pl=-12.5)
        return {"price": self.bid, "realized_pl": -12.5}


class TradingTestCase(TestCase):
    def setUp(self):
        SiteSettings.objects.update_or_create(pk=1, defaults={"data_provider": "oanda", "oanda_api_token": "tok"})
        self.trader = AutoTrader.load()
        self.trader.account_id, self.trader.enabled = "101-004-1-001", True
        self.trader.save()
        self.eur = Instrument.objects.create(symbol="EUR/USD", pip_size=0.0001, decimals=5)
        self.gbp = Instrument.objects.create(symbol="GBP/USD", pip_size=0.0001, decimals=5)
        self.profile = StrategyProfile.objects.create(name="Intraday", entry_timeframe="15m",
                                                      confirm_timeframes=["1h"])
        self.broker = FakeBroker()
        patcher = mock.patch.object(services, "get_broker", return_value=self.broker)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.notes = []
        notify = mock.patch.object(services, "_notify", side_effect=lambda s, lines: self.notes.append(s))
        notify.start()
        self.addCleanup(notify.stop)

    def signal(self, instrument=None, direction="buy", entry=1.10000, sl=1.09800, tp=1.10400, age=5, **kw):
        return Signal.objects.create(
            profile=self.profile, instrument=instrument or self.eur, direction=direction,
            candle_time=NOW - timedelta(minutes=age), entry=entry, stop_loss=sl, take_profit=tp,
            risk_reward=2.0, score=80, reasons=[], **kw)


class SizingTests(TestCase):
    def test_units_risk_the_chosen_amount(self):
        self.assertEqual(position_units(100, 1.1000, 1.0980, 1.0, 0, 1), 50000)  # 20 pips on EUR/USD in USD
        jpy = position_units(100, 158.00, 157.70, 1 / 158.0, 0, 1)  # 30 pips on USD/JPY, loss converted from JPY
        self.assertAlmostEqual(jpy * 0.30 / 158.0, 100, delta=0.01)
        self.assertEqual(position_units(1, 1.1, 1.0, 1.0, 0, 100), 0)  # below broker minimum


class OpenTradeTests(TradingTestCase):
    def test_opens_fresh_signal_with_attached_stop_and_target(self):
        sig = self.signal()
        summary = trade_cycle(now=NOW)
        self.assertEqual(summary["opened"], 1)
        order = self.broker.orders[0]
        self.assertEqual((order["name"], order["sl"], order["tp"]), ("EUR_USD", 1.09800, 1.10400))
        # 1% of 10,000 = 100 USD over (1.10010 - 1.09800) = 21 pips -> 47,619 units
        self.assertEqual(order["units"], 47619)
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.units, bt.fill_price), ("open", 47619, 1.10010))
        self.assertIn("Opened BUY EUR/USD", self.notes[0])

    def test_sell_units_are_negative(self):
        self.signal(direction="sell", entry=1.10000, sl=1.10200, tp=1.09600)
        trade_cycle(now=NOW)
        self.assertLess(self.broker.orders[0]["units"], 0)

    def test_never_places_the_same_signal_twice(self):
        self.signal()
        trade_cycle(now=NOW)
        trade_cycle(now=NOW + timedelta(minutes=1))
        self.assertEqual(len(self.broker.orders), 1)

    def test_old_signals_are_not_traded(self):
        self.signal(age=60)  # a 15m signal from an hour ago
        trade_cycle(now=NOW)
        self.assertEqual(self.broker.orders, [])

    def skipped(self, sig):
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual(bt.status, "skipped")
        return bt.message

    def test_skip_reasons(self):
        self.broker.existing = ["GBP_USD"]
        self.assertIn("already open", self.skipped_after(self.signal(instrument=self.gbp)))

    def skipped_after(self, sig):
        trade_cycle(now=NOW)
        return self.skipped(sig)

    def test_skips_when_price_moved_too_far(self):
        self.broker.bid, self.broker.ask = 1.10090, 1.10100  # 10 pips past entry, limit 0.25 × 20 pips
        self.assertIn("too late", self.skipped_after(self.signal()))

    def test_skips_when_max_open_trades_reached(self):
        self.trader.max_open_trades = 1
        self.trader.save()
        self.broker.existing = ["USD_JPY"]
        self.assertIn("maximum", self.skipped_after(self.signal()))

    def test_skips_low_confidence_when_minimum_set(self):
        self.trader.min_confidence = 40
        self.trader.save()
        self.assertIn("below your minimum", self.skipped_after(self.signal(confidence=31)))

    def test_skips_near_news(self):
        from apps.news.services import NewsCalendar

        news = NewsCalendar([{"title": "CPI", "currency": "USD", "time": NOW + timedelta(minutes=10),
                              "impact": "high"}], covered_from=NOW - timedelta(days=1), covered_until=NOW + timedelta(1))
        with mock.patch("apps.news.services.NewsCalendar.load", return_value=news):
            self.assertIn("CPI", self.skipped_after(self.signal()))

    def test_broker_rejection_is_recorded(self):
        self.broker.reject = "Order cancelled: INSUFFICIENT_MARGIN"
        sig = self.signal()
        trade_cycle(now=NOW)
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual(bt.status, "rejected")
        self.assertIn("INSUFFICIENT_MARGIN", bt.message)

    def test_off_means_no_new_trades(self):
        self.trader.enabled = False
        self.trader.save()
        self.signal()
        trade_cycle(now=NOW)
        self.assertEqual(self.broker.orders, [])

    def test_requires_oanda_prices_and_live_permission(self):
        SiteSettings.objects.filter(pk=1).update(data_provider="yahoo")
        self.signal()
        self.assertEqual(trade_cycle(now=NOW)["status"], "needs oanda data")
        SiteSettings.objects.filter(pk=1).update(data_provider="oanda", oanda_environment="live")
        self.assertEqual(trade_cycle(now=NOW)["status"], "live not allowed")
        self.assertEqual(self.broker.orders, [])

    def test_only_chosen_strategies(self):
        other = StrategyProfile.objects.create(name="Other")
        self.trader.strategies.set([other])
        self.signal()
        trade_cycle(now=NOW)
        self.assertEqual(self.broker.orders, [])


class LifecycleTests(TradingTestCase):
    def test_take_profit_closes_and_it_waits_for_the_next_signal(self):
        sig = self.signal()
        trade_cycle(now=NOW)
        trade_id = BrokerTrade.objects.get(signal=sig).broker_trade_id
        self.broker.trades[trade_id].update(state="CLOSED", close=1.104, pl=95.2, reason="tp")
        summary = trade_cycle(now=NOW + timedelta(hours=1))
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.close_reason, bt.realized_pl), ("closed", "tp", 95.2))
        self.assertEqual(summary["closed"], 1)
        nxt = self.signal(age=0)
        Signal.objects.filter(pk=nxt.pk).update(candle_time=NOW + timedelta(hours=1))
        trade_cycle(now=NOW + timedelta(hours=1, minutes=2))
        self.assertEqual(len(self.broker.orders), 2)

    def test_expired_signal_closes_the_trade(self):
        sig = self.signal()
        trade_cycle(now=NOW)
        Signal.objects.filter(pk=sig.pk).update(status="expired")
        trade_cycle(now=NOW + timedelta(hours=12))
        bt = BrokerTrade.objects.get(signal=sig)
        self.assertEqual((bt.status, bt.close_reason), ("closed", "expired"))
        self.assertEqual(len(self.broker.closed), 1)

    def test_daily_loss_limit_pauses_trading(self):
        trade_cycle(now=NOW)  # records the day's starting value: 10,000
        self.broker.nav = 9650.0  # -3.5%
        self.signal(age=1)
        trade_cycle(now=NOW + timedelta(minutes=1))
        self.trader.refresh_from_db()
        self.assertIsNotNone(self.trader.halted_until)
        self.assertEqual(self.broker.orders, [])
        self.assertIn("Daily loss limit", self.trader.halt_reason)
        nxt = self.signal(instrument=self.gbp, age=0)
        Signal.objects.filter(pk=nxt.pk).update(candle_time=NOW + timedelta(days=1, minutes=1))
        trade_cycle(now=NOW + timedelta(days=1, minutes=3))  # next day: resumes
        self.trader.refresh_from_db()
        self.assertIsNone(self.trader.halted_until)
        self.assertEqual(len(self.broker.orders), 1)

    def test_interrupted_order_is_recovered(self):
        sig = self.signal()
        cid = services.client_id(sig)
        self.broker.trades["77"] = {"instrument": "EUR_USD", "units": 1000, "state": "OPEN", "price": 1.1001,
                                    "client_id": cid}
        bt = BrokerTrade.objects.create(signal=sig, instrument=self.eur, direction="buy", status="pending")
        BrokerTrade.objects.filter(pk=bt.pk).update(created_at=NOW - timedelta(minutes=5))
        trade_cycle(now=NOW)
        bt.refresh_from_db()
        self.assertEqual((bt.status, bt.broker_trade_id, bt.units), ("open", "77", 1000))

    def test_close_all_kill_switch(self):
        self.signal()
        trade_cycle(now=NOW)
        closed, errors = services.close_all()
        self.assertEqual((closed, errors), (1, []))
        self.assertEqual(BrokerTrade.objects.filter(status="open").count(), 0)


class DashboardTests(TradingTestCase):
    def test_pages_and_switch(self):
        admin = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(admin)
        self.signal()
        trade_cycle(now=NOW)
        page = self.client.get(reverse("trading:dashboard"))
        self.assertContains(page, "EUR/USD")
        self.assertContains(page, "Practice account")
        self.client.post(reverse("trading:switch"))
        self.assertFalse(AutoTrader.load().enabled)
        SiteSettings.objects.filter(pk=1).update(data_provider="yahoo")
        response = self.client.post(reverse("trading:switch"))
        self.assertFalse(AutoTrader.load().enabled)  # setup not finished: stays off
        self.assertEqual(response.status_code, 302)

    def test_team_members_cannot_see_it(self):
        member = get_user_model().objects.create_user("trader", "t@example.com", "pw")
        self.client.force_login(member)
        self.assertEqual(self.client.get(reverse("trading:dashboard")).status_code, 302)


def fake_response(status, data):
    response = mock.Mock(status_code=status)
    response.json.return_value = data
    return response


class OandaParsingTests(TestCase):
    """The client against OANDA's documented response shapes."""

    def broker(self):
        return OandaBroker("tok", "101-1", "practice")

    def test_market_order_fill_and_cancel(self):
        filled = {"orderFillTransaction": {"price": "1.10012", "tradeOpened": {"tradeID": "6", "units": "1000",
                                                                               "price": "1.10012"}}}
        with mock.patch("requests.request", return_value=fake_response(201, filled)) as req:
            fill = self.broker().market_order("EUR_USD", 1000, 1.098, 1.104, 5, "sd-1-2")
        self.assertEqual(fill, {"trade_id": "6", "units": 1000.0, "price": 1.10012})
        body = req.call_args.kwargs["json"]["order"]
        self.assertEqual((body["stopLossOnFill"]["price"], body["takeProfitOnFill"]["price"]), ("1.09800", "1.10400"))
        self.assertEqual(body["clientExtensions"]["id"], "sd-1-2")
        cancelled = {"orderCancelTransaction": {"reason": "MARKET_HALTED"}}
        with mock.patch("requests.request", return_value=fake_response(201, cancelled)):
            with self.assertRaisesMessage(BrokerError, "MARKET_HALTED"):
                self.broker().market_order("EUR_USD", 1000, 1.098, 1.104, 5, "sd-1-3")

    def test_errors_and_price_conversion(self):
        with mock.patch("requests.request", return_value=fake_response(401, {"errorMessage": "Insufficient auth"})):
            with self.assertRaisesMessage(BrokerError, "Insufficient auth"):
                self.broker().summary()
        priced = {"prices": [{"bids": [{"price": "157.990"}], "asks": [{"price": "158.010"}], "tradeable": True}],
                  "homeConversions": [{"currency": "JPY", "accountLoss": "0.0063331", "accountGain": "0.0063"}]}
        with mock.patch("requests.request", return_value=fake_response(200, priced)):
            p = self.broker().price("USD_JPY")
        self.assertEqual((p["bid"], p["ask"], p["loss_factor"]), (157.99, 158.01, 0.0063331))

    def test_trade_close_reason(self):
        t = {"trade": {"id": "6", "state": "CLOSED", "realizedPL": "-21.3", "averageClosePrice": "1.098",
                       "price": "1.1001", "initialUnits": "1000", "stopLossOrder": {"state": "FILLED"},
                       "takeProfitOrder": {"state": "CANCELLED"}}}
        with mock.patch("requests.request", return_value=fake_response(200, t)):
            info = self.broker().trade("6")
        self.assertEqual((info["state"], info["reason"], info["realized_pl"]), ("CLOSED", "sl", -21.3))


class HistoryPagingTests(TestCase):
    def test_oanda_history_pages_backwards(self):
        from apps.market.providers import OandaProvider

        start = datetime(2024, 1, 1, tzinfo=UTC)

        def page(n, end):
            return [{"time": end - timedelta(hours=n - k), "open": 1, "high": 1, "low": 1, "close": 1, "volume": 0}
                    for k in range(n)]

        pages = [page(5000, start + timedelta(hours=20000)), page(5000, start + timedelta(hours=15000)),
                 page(1200, start + timedelta(hours=10000))]
        provider = OandaProvider("tok")
        with mock.patch.object(provider, "_fetch_page", side_effect=pages) as fetch:
            candles = provider.fetch(Instrument(symbol="EUR/USD"), "1h", history=True)
        self.assertEqual(len(candles), 11200)
        self.assertEqual(fetch.call_count, 3)
        times = [c["time"] for c in candles]
        self.assertEqual(times, sorted(times))
