from datetime import datetime, timedelta, timezone

from django.test import TestCase

from apps.core.models import SiteSettings
from apps.market.models import Instrument
from apps.news.models import EconomicEvent
from apps.news.services import NewsCalendar, currencies_for, parse_feed, store_events

UTC = timezone.utc
FEED = [
    {"title": "CPI m/m", "country": "USD", "date": "2026-10-08T08:30:00-04:00", "impact": "High",
     "forecast": "0.3%", "previous": "0.2%"},
    {"title": "German Ifo", "country": "EUR", "date": "2026-10-08T04:00:00-04:00", "impact": "Medium"},
    {"title": "Bank Holiday", "country": "JPY", "date": "2026-10-09T00:00:00-04:00", "impact": "Holiday"},
    {"title": "Broken", "country": "USD", "date": "not a date", "impact": "High"},
    {"title": "Unknown impact", "country": "USD", "date": "2026-10-08T09:00:00-04:00", "impact": "Non-Economic"},
]


class CalendarTests(TestCase):
    def test_parse_converts_to_utc_and_skips_bad_rows(self):
        events = parse_feed(FEED)
        self.assertEqual(len(events), 3)
        cpi = events[0]
        self.assertEqual((cpi["currency"], cpi["impact"]), ("USD", "high"))
        self.assertEqual(cpi["time"], datetime(2026, 10, 8, 12, 30, tzinfo=UTC))

    def test_store_replaces_moved_events(self):
        store_events(parse_feed(FEED))
        moved = parse_feed(FEED)
        moved[0]["time"] += timedelta(hours=1)
        store_events(moved)
        self.assertEqual(EconomicEvent.objects.filter(title="CPI m/m").count(), 1)
        self.assertEqual(EconomicEvent.objects.get(title="CPI m/m").time, datetime(2026, 10, 8, 13, 30, tzinfo=UTC))

    def test_currencies(self):
        self.assertEqual(currencies_for(Instrument(symbol="EUR/USD")), ["EUR", "USD", "ALL"])
        self.assertEqual(currencies_for(Instrument(symbol="XAU/USD")), ["XAU", "USD", "ALL"])

    def test_blocking_window(self):
        store_events(parse_feed(FEED))
        SiteSettings.objects.update_or_create(pk=1, defaults={"calendar_ok_at": datetime.now(UTC)})
        news = NewsCalendar.load()
        cpi = datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
        usd = ["EUR", "USD", "ALL"]
        self.assertIsNotNone(news.blocking(cpi - timedelta(minutes=20), usd, 30, 30))
        self.assertIsNotNone(news.blocking(cpi + timedelta(minutes=20), usd, 30, 30))
        self.assertIsNone(news.blocking(cpi - timedelta(minutes=45), usd, 30, 30))
        self.assertIsNone(news.blocking(cpi, ["GBP", "JPY", "ALL"], 30, 30))
        ifo = datetime(2026, 10, 8, 8, 0, tzinfo=UTC)
        self.assertIsNone(news.blocking(ifo, usd, 30, 30))  # medium impact ignored by default
        self.assertIsNotNone(news.blocking(ifo, usd, 30, 30, include_medium=True))
        self.assertFalse(news.covers(datetime(2026, 9, 1, tzinfo=UTC)))  # before the calendar was collected
        self.assertTrue(news.covers(cpi))
