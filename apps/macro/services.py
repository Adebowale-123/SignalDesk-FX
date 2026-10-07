"""
Fundamentals and sentiment from free data, turned into a bias per currency pair with no look-ahead.

Data (no keys needed):
  Yahoo daily closes: VIX, S&P 500, US dollar index, US 10-year yield, crude oil.
  CFTC Commitments of Traders (weekly): large speculators' net position in each currency future and gold.

Per currency:
  Fundamental  USD: dollar-index trend + US yields direction. Gold: the opposite (it pays no yield).
               CAD: oil trend.
  Sentiment    Risk mood (S&P 500 trend, VIX stress): risk-on lifts AUD/NZD/CAD, risk-off lifts JPY/CHF/gold/USD.
               Speculator positioning: net long supports a currency; a 3-year extreme marks a crowded trade.
A pair's bias is base minus quote. Every value is used only after it was published (daily closes the next day,
COT reports from the Saturday after their Tuesday date).
"""

import csv
import io
import logging
import time as time_module
import zipfile
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import requests
from django.utils import timezone as dj_tz

from .models import CotReport, MacroBar

log = logging.getLogger("apps.macro")

SERIES = {"VIX": "^VIX", "SPX": "^GSPC", "DXY": "DX-Y.NYB", "US10Y": "^TNX", "OIL": "CL=F"}
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
COT_URL = "https://www.cftc.gov/files/dea/history/deacot{year}.zip"
COT_CODES = {"099741": "EUR", "096742": "GBP", "097741": "JPY", "232741": "AUD", "090741": "CAD",
             "092741": "CHF", "112741": "NZD", "088691": "XAU", "098662": "USD"}
COT_YEARS = 6  # enough for a 3-year positioning range across the whole backtest history
REFRESH = timedelta(hours=6)
HEADERS = {"User-Agent": "Mozilla/5.0 SignalDesk"}

RISK_WEIGHT = {"AUD": 1.0, "NZD": 1.0, "CAD": 0.5, "JPY": -1.0, "CHF": -1.0, "USD": -0.5, "XAU": -0.5}
CROWDED_HIGH, CROWDED_LOW = 90, 10
COLUMNS = ["m_fund", "m_sent", "m_mood", "m_dxy", "m_yld", "m_oil", "m_vix", "cot_b", "cot_q"]


class MacroError(Exception):
    pass


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


def fetch_yahoo_daily(symbol, span):
    """{trading date: close} for a Yahoo symbol."""
    try:
        data = requests.get(YAHOO_URL.format(symbol=symbol), params={"interval": "1d", "range": span},
                            headers=HEADERS, timeout=30).json()
        result = data["chart"]["result"][0]
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
        raise MacroError(f"Yahoo {symbol}: {exc.__class__.__name__}") from exc
    offset = (result.get("meta") or {}).get("gmtoffset") or 0
    closes = ((result.get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    out = {}
    for ts, close in zip(result.get("timestamp") or [], closes):
        if close is not None:
            out[datetime.fromtimestamp(ts + offset, timezone.utc).date()] = float(close)
    return out


def store_series(code, values):
    existing = dict(MacroBar.objects.filter(code=code, date__in=list(values)).values_list("date", "close"))
    new = [MacroBar(code=code, date=d, close=c) for d, c in values.items() if d not in existing]
    MacroBar.objects.bulk_create(new, ignore_conflicts=True, batch_size=1000)
    for d, c in values.items():  # the latest day can still be moving when first downloaded
        if d in existing and existing[d] != c:
            MacroBar.objects.filter(code=code, date=d).update(close=c)
    return len(new)


def parse_cot(zip_bytes):
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
        text = z.read(z.namelist()[0]).decode("latin-1")
    rows = csv.reader(io.StringIO(text))
    header = [h.strip() for h in next(rows)]
    col = {name: header.index(name) for name in (
        "As of Date in Form YYYY-MM-DD", "CFTC Contract Market Code", "Open Interest (All)",
        "Noncommercial Positions-Long (All)", "Noncommercial Positions-Short (All)")}
    out = []
    for r in rows:
        currency = COT_CODES.get(r[col["CFTC Contract Market Code"]].strip())
        if not currency:
            continue
        try:
            out.append({"currency": currency,
                        "report_date": datetime.strptime(r[col["As of Date in Form YYYY-MM-DD"]].strip(), "%Y-%m-%d").date(),
                        "long": int(r[col["Noncommercial Positions-Long (All)"]]),
                        "short": int(r[col["Noncommercial Positions-Short (All)"]]),
                        "open_interest": int(r[col["Open Interest (All)"]])})
        except (ValueError, IndexError):
            continue
    return out


def store_cot(rows):
    existing = set(CotReport.objects.values_list("currency", "report_date"))
    new = [CotReport(**r) for r in rows if (r["currency"], r["report_date"]) not in existing]
    CotReport.objects.bulk_create(new, ignore_conflicts=True, batch_size=1000)
    return len(new)


def fetch_cot_year(year):
    try:
        response = requests.get(COT_URL.format(year=year), headers=HEADERS, timeout=60)
    except requests.RequestException as exc:
        raise MacroError(f"CFTC {year}: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise MacroError(f"CFTC {year}: HTTP {response.status_code}")
    return parse_cot(response.content)


def refresh_macro(force=False):
    """Update market drivers and positioning at most every few hours. Never raises."""
    from apps.core.models import SiteSettings

    site = SiteSettings.load()
    now = dj_tz.now()
    if not force and site.macro_fetched_at and now - site.macro_fetched_at < REFRESH:
        return None
    SiteSettings.objects.filter(pk=site.pk).update(macro_fetched_at=now)
    errors, added = [], 0
    for code, symbol in SERIES.items():
        try:
            full = not MacroBar.objects.filter(code=code).exists()
            added += store_series(code, fetch_yahoo_daily(symbol, "10y" if full else "1mo"))
        except MacroError as exc:
            errors.append(str(exc))
    last = CotReport.objects.order_by("-report_date").values_list("report_date", flat=True).first()
    today = now.date()
    if last is None:
        years = range(today.year - COT_YEARS, today.year + 1)
    elif (today - last).days >= 7:
        years = sorted({today.year, (today - timedelta(days=14)).year})
    else:
        years = []
    for year in years:
        try:
            added += store_cot(fetch_cot_year(year))
        except MacroError as exc:
            errors.append(str(exc))
    for message in errors:
        log.warning("Macro data not updated: %s", message)
    MacroData.clear_cache()
    return {"added": added, "errors": errors}


# ---------------------------------------------------------------------------
# Bias
# ---------------------------------------------------------------------------


def _band(x, width):
    """+1 / -1 outside a dead band, 0 inside it; NaN stays NaN."""
    return np.where(np.isnan(x), np.nan, np.where(x > width, 1.0, np.where(x < -width, -1.0, 0.0)))


def daily_drivers(bars):
    """bars: {code: {date: close}} -> DataFrame indexed by the time each day's values became usable (UTC)."""
    frame = pd.DataFrame({code: pd.Series(values, dtype=float) for code, values in bars.items()})
    if frame.empty:
        return frame
    frame.index = pd.to_datetime(frame.index)
    frame = frame.sort_index().ffill()
    for code in SERIES:
        if code not in frame:
            frame[code] = np.nan

    def vs_ema(s, width):
        ema = s.ewm(span=50, adjust=False, min_periods=50).mean()
        return _band((s / ema - 1).to_numpy(), width)

    spx_trend = vs_ema(frame["SPX"], 0.005)
    vix = frame["VIX"]
    stress = ((vix > 25) | (vix > 1.25 * vix.rolling(20).mean())).to_numpy()
    mood = np.where(np.isnan(spx_trend), np.nan, np.where(stress, -1.0, spx_trend))
    yld = _band((frame["US10Y"] - frame["US10Y"].shift(20)).to_numpy(), 0.10)  # 10 basis points
    out = pd.DataFrame({"mood": mood, "dxy": vs_ema(frame["DXY"], 0.003), "yld": yld,
                        "oil": vs_ema(frame["OIL"], 0.02), "vix": vix.to_numpy()}, index=frame.index)
    # A day's close is known after the US close; use it from the next day (00:00 UTC).
    out.index = (out.index + pd.Timedelta(days=1)).tz_localize("UTC")
    return out


def cot_positioning(rows):
    """rows of CotReport-like dicts -> {currency: DataFrame(net, index 0-100)} indexed by when it was public."""
    out = {}
    frame = pd.DataFrame(rows)
    if frame.empty:
        return out
    for currency, g in frame.groupby("currency"):
        g = g.sort_values("report_date")
        net = (g["long"] - g["short"]).astype(float).to_numpy()
        s = pd.Series(net)
        lo, hi = s.rolling(156, min_periods=52).min(), s.rolling(156, min_periods=52).max()
        index = ((s - lo) / (hi - lo).replace(0, np.nan) * 100).to_numpy()
        # Tuesday's positions are published on Friday afternoon (US time); use them from Saturday 00:00 UTC.
        when = (pd.to_datetime(g["report_date"]) + pd.Timedelta(days=4)).dt.tz_localize("UTC")
        out[currency] = pd.DataFrame({"net": net, "index": index}, index=pd.DatetimeIndex(when))
    return out


def currency_fund(c, drv):
    zero = np.zeros(len(drv))
    if c == "USD":
        return np.nan_to_num(drv["dxy"].to_numpy()) + 0.5 * np.nan_to_num(drv["yld"].to_numpy())
    if c == "XAU":
        return -np.nan_to_num(drv["dxy"].to_numpy()) - np.nan_to_num(drv["yld"].to_numpy())
    if c == "CAD":
        return np.nan_to_num(drv["oil"].to_numpy())
    return zero


def currency_sent(c, drv, cot_net):
    return RISK_WEIGHT.get(c, 0.0) * np.nan_to_num(drv["mood"].to_numpy()) + 0.5 * np.nan_to_num(np.sign(cot_net))


class MacroData:
    _cache = (0.0, None)
    TTL = 600

    def __init__(self, drivers, cot):
        self.drivers, self.cot = drivers, cot

    @classmethod
    def load(cls):
        bars = {}
        for code, d, c in MacroBar.objects.values_list("code", "date", "close"):
            bars.setdefault(code, {})[d] = c
        rows = list(CotReport.objects.values("currency", "report_date", "long", "short"))
        return cls(daily_drivers(bars) if bars else pd.DataFrame(), cot_positioning(rows))

    @classmethod
    def cached(cls):
        stamp, data = cls._cache
        if data is None or time_module.monotonic() - stamp > cls.TTL:
            data = cls.load()
            cls._cache = (time_module.monotonic(), data)
        return data

    @classmethod
    def clear_cache(cls):
        cls._cache = (0.0, None)

    @property
    def available(self):
        return not self.drivers.empty

    def frame_for(self, instrument):
        """Pair-level bias over time (index = when it became usable)."""
        if self.drivers.empty:
            return pd.DataFrame(columns=COLUMNS)
        base, quote = [c.upper() for c in instrument.currencies]
        times = self.drivers.index
        for c in (base, quote):
            if c in self.cot:
                times = times.union(self.cot[c].index)
        drv = self.drivers.reindex(times).ffill()

        def cot(c, field):
            if c not in self.cot:
                return np.full(len(times), np.nan)
            return self.cot[c][field].reindex(times).ffill().to_numpy()

        net_b, net_q = cot(base, "net"), cot(quote, "net")
        frame = pd.DataFrame({
            "m_fund": currency_fund(base, drv) - currency_fund(quote, drv),
            "m_sent": currency_sent(base, drv, net_b) - currency_sent(quote, drv, net_q),
            "m_mood": drv["mood"].to_numpy(), "m_dxy": drv["dxy"].to_numpy(), "m_yld": drv["yld"].to_numpy(),
            "m_oil": drv["oil"].to_numpy(), "m_vix": drv["vix"].to_numpy(),
            "cot_b": cot(base, "index"), "cot_q": cot(quote, "index"),
        }, index=times)
        missing = np.isnan(frame["m_mood"].to_numpy())  # no driver history yet at that time
        frame.loc[missing, ["m_fund", "m_sent"]] = np.nan
        return frame

    def attach(self, features, instrument):
        """Add the bias known at each candle's close (values older than 10 days are treated as missing)."""
        frame = self.frame_for(instrument)
        if frame.empty or features is None or features.empty:
            out = features.copy() if features is not None else features
            if out is not None:
                for c in COLUMNS:
                    out[c] = np.nan
            return out
        right = frame.rename_axis("m_time").reset_index().sort_values("m_time")
        right["m_time"] = right["m_time"].astype(features["close_time"].dtype)
        left = features.rename_axis("time").reset_index()
        merged = pd.merge_asof(left, right, left_on="close_time", right_on="m_time", direction="backward",
                               tolerance=pd.Timedelta(days=10))
        return merged.drop(columns=["m_time"]).set_index("time")

    def snapshot(self, when=None):
        """Current drivers and positioning for the Research page."""
        when = pd.Timestamp(when or dj_tz.now())
        if self.drivers.empty:
            return None
        past = self.drivers[self.drivers.index <= when]
        if past.empty:
            return None
        row = past.iloc[-1]
        cot = {}
        for currency, frame in sorted(self.cot.items()):
            p = frame[frame.index <= when]
            if not p.empty:
                cot[currency] = {"net": int(p["net"].iloc[-1]), "index": None if np.isnan(p["index"].iloc[-1])
                                 else round(float(p["index"].iloc[-1])), "as_of": p.index[-1] - pd.Timedelta(days=4)}
        return {"as_of": past.index[-1] - pd.Timedelta(days=1), "mood": row["mood"], "dxy": row["dxy"],
                "yld": row["yld"], "oil": row["oil"], "vix": row["vix"], "cot": cot}
