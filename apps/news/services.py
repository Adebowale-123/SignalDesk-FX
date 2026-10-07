"""Economic calendar: download it automatically and tell the engine when news is too close to trade."""

import bisect
import logging
from datetime import datetime, timedelta, timezone

import requests
from django.utils import timezone as dj_tz

from .models import EconomicEvent

log = logging.getLogger("apps.news")

FEED_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
REFRESH = timedelta(hours=1)  # the feed is free but blocks clients that poll it too often
IMPACTS = {"High": "high", "Medium": "medium", "Low": "low", "Holiday": "holiday"}
# Gold is priced in dollars and moves on US data. "ALL" covers worldwide events.
EXTRA_CURRENCIES = {"XAU": ["USD"]}


class CalendarError(Exception):
    pass


def parse_feed(items):
    events = []
    for item in items:
        impact = IMPACTS.get(item.get("impact"))
        try:
            when = datetime.fromisoformat(item["date"]).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            continue
        if not impact:
            continue
        events.append({"title": (item.get("title") or "")[:160], "currency": (item.get("country") or "").upper()[:4],
                       "time": when, "impact": impact, "forecast": (item.get("forecast") or "")[:30],
                       "previous": (item.get("previous") or "")[:30]})
    return events


def store_events(events):
    """Save the week's events, removing ones the feed dropped or moved within that week."""
    if not events:
        return 0
    start, end = min(e["time"] for e in events), max(e["time"] for e in events)
    keep = {(e["title"], e["currency"], e["time"]) for e in events}
    stale = [e.pk for e in EconomicEvent.objects.filter(time__gte=start, time__lte=end)
             if (e.title, e.currency, e.time) not in keep]
    EconomicEvent.objects.filter(pk__in=stale).delete()
    for e in events:
        EconomicEvent.objects.update_or_create(
            title=e["title"], currency=e["currency"], time=e["time"],
            defaults={"impact": e["impact"], "forecast": e["forecast"], "previous": e["previous"]})
    return len(events)


def refresh_calendar(force=False):
    """Download the calendar at most once an hour. Never raises: a failed download keeps the stored events."""
    from apps.core.models import SiteSettings

    site = SiteSettings.load()
    now = dj_tz.now()
    if not force and site.calendar_fetched_at and now - site.calendar_fetched_at < REFRESH:
        return None
    SiteSettings.objects.filter(pk=site.pk).update(calendar_fetched_at=now)  # also spaces out retries after errors
    try:
        response = requests.get(FEED_URL, headers={"User-Agent": "Mozilla/5.0 SignalDesk"}, timeout=20)
        if response.status_code != 200:
            raise CalendarError(f"calendar feed answered {response.status_code}")
        count = store_events(parse_feed(response.json()))
    except (requests.RequestException, ValueError, CalendarError) as exc:
        log.warning("Economic calendar not updated: %s", exc)
        return None
    SiteSettings.objects.filter(pk=site.pk).update(calendar_ok_at=now)
    return count


def currencies_for(instrument):
    found = []
    for cur in instrument.currencies:
        for c in [cur.upper(), *EXTRA_CURRENCIES.get(cur.upper(), [])]:
            if c and c not in found:
                found.append(c)
    return found + ["ALL"]


class NewsCalendar:
    """Fast lookups of stored events for the engine, live and in backtests."""

    def __init__(self, events, *, covered_from=None, covered_until=None):
        self.events = sorted(events, key=lambda e: e["time"])
        self.times = [e["time"] for e in self.events]
        # The calendar only knows about this period (collection started at covered_from).
        self.covered_from, self.covered_until = covered_from, covered_until

    @classmethod
    def load(cls):
        from apps.core.models import SiteSettings

        events = [{"title": e.title, "currency": e.currency, "time": e.time, "impact": e.impact}
                  for e in EconomicEvent.objects.filter(impact__in=["high", "medium"])]
        times = EconomicEvent.objects.order_by("time").values_list("time", flat=True)
        first, last = times.first(), times.last()
        if not SiteSettings.load().calendar_ok_at:
            first = last = None
        return cls(events, covered_from=first, covered_until=last)

    def covers(self, when):
        return self.covered_from is not None and self.covered_from <= when <= self.covered_until

    @staticmethod
    def _relevant(event, currencies, include_medium):
        return event["currency"] in currencies and (event["impact"] == "high" or include_medium)

    def blocking(self, when, currencies, before_min, after_min, include_medium=False):
        """The first relevant event between `after_min` ago and `before_min` ahead, or None."""
        lo = bisect.bisect_left(self.times, when - timedelta(minutes=after_min))
        hi = bisect.bisect_right(self.times, when + timedelta(minutes=before_min))
        for event in self.events[lo:hi]:
            if self._relevant(event, currencies, include_medium):
                return event
        return None

    def upcoming(self, when, currencies, hours=24, include_medium=False):
        i = bisect.bisect_left(self.times, when)
        out = []
        for event in self.events[i:]:
            if event["time"] - when > timedelta(hours=hours):
                break
            if self._relevant(event, currencies, include_medium):
                out.append(event)
        return out
