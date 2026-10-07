"""Price data providers. Each returns a list of closed candles as dicts (UTC open time)."""

import logging
import time as time_module
from datetime import datetime, timedelta, timezone

import requests

from .models import TIMEFRAMES

log = logging.getLogger("apps.market")


class ProviderError(Exception):
    pass


def _closed_only(candles, timeframe, now=None):
    """Drop the still-forming candle: analysis only uses candles that have closed."""
    now = now or datetime.now(timezone.utc)
    length = timedelta(minutes=TIMEFRAMES[timeframe])
    return [c for c in candles if c["time"] + length <= now]


class BaseProvider:
    code = ""
    native = set()  # timeframes fetched directly; others are built by resampling
    min_seconds_between_calls = 0.0
    _last_call = 0.0

    def symbol_for(self, instrument):
        raise NotImplementedError

    def fetch(self, instrument, timeframe, *, history=False):
        """Return closed candles. history=True asks for as much past data as the provider allows."""
        raise NotImplementedError

    def _throttle(self):
        wait = self.min_seconds_between_calls - (time_module.monotonic() - BaseProvider._last_call)
        if wait > 0:
            time_module.sleep(wait)
        BaseProvider._last_call = time_module.monotonic()


class YahooProvider(BaseProvider):
    """Free, no key. Intraday history: 60 days (5m-30m), 730 days (1h). Gold uses COMEX futures (GC=F)."""

    code = "yahoo"
    native = {"5m", "15m", "30m", "1h", "1d"}
    INTERVAL = {"5m": "5m", "15m": "15m", "30m": "30m", "1h": "60m", "1d": "1d"}
    HISTORY = {"5m": "59d", "15m": "59d", "30m": "59d", "1h": "729d", "1d": "10y"}
    RECENT = {"5m": "5d", "15m": "5d", "30m": "5d", "1h": "30d", "1d": "1y"}
    URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

    def symbol_for(self, instrument):
        if not instrument.yahoo_symbol:
            raise ProviderError(f"{instrument} has no Yahoo symbol set.")
        return instrument.yahoo_symbol

    def fetch(self, instrument, timeframe, *, history=False, _retry=True):
        params = {"interval": self.INTERVAL[timeframe], "range": (self.HISTORY if history else self.RECENT)[timeframe]}
        try:
            return self._fetch(instrument, timeframe, params)
        except ProviderError:
            if not _retry:
                raise
            time_module.sleep(2)  # Yahoo occasionally refuses a request in a burst; one retry is enough
            return self.fetch(instrument, timeframe, history=history, _retry=False)

    def _fetch(self, instrument, timeframe, params):
        try:
            response = requests.get(self.URL.format(symbol=self.symbol_for(instrument)), params=params,
                                    headers={"User-Agent": "Mozilla/5.0 SignalDesk"}, timeout=30)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Yahoo unreachable: {exc.__class__.__name__}") from exc
        result = (data.get("chart") or {}).get("result")
        if not result:
            error = (data.get("chart") or {}).get("error") or {}
            raise ProviderError(f"Yahoo: {error.get('description', 'no data')}")
        result = result[0]
        quote = (result.get("indicators") or {}).get("quote", [{}])[0]
        candles = []
        for i, ts in enumerate(result.get("timestamp") or []):
            o, h, low, c = (quote.get(k, [None])[i] for k in ("open", "high", "low", "close"))
            if None in (o, h, low, c):
                continue
            candles.append({"time": datetime.fromtimestamp(ts, timezone.utc), "open": o, "high": h, "low": low,
                            "close": c, "volume": (quote.get("volume") or [0] * (i + 1))[i] or 0})
        if timeframe == "1d":
            for c in candles:  # daily bars: normalise to midnight UTC
                c["time"] = c["time"].replace(hour=0, minute=0, second=0)
        return _closed_only(candles, timeframe)


class TwelveDataProvider(BaseProvider):
    """Free key: 800 requests/day, 8 per minute. Has spot gold (XAU/USD)."""

    code = "twelvedata"
    native = {"5m", "15m", "30m", "1h", "4h", "1d"}
    INTERVAL = {"5m": "5min", "15m": "15min", "30m": "30min", "1h": "1h", "4h": "4h", "1d": "1day"}
    URL = "https://api.twelvedata.com/time_series"
    min_seconds_between_calls = 8.0

    def __init__(self, api_key):
        if not api_key:
            raise ProviderError("Twelve Data needs an API key (Settings → Data source).")
        self.api_key = api_key

    def symbol_for(self, instrument):
        return instrument.twelvedata_symbol or instrument.symbol

    def fetch(self, instrument, timeframe, *, history=False):
        self._throttle()
        params = {"symbol": self.symbol_for(instrument), "interval": self.INTERVAL[timeframe],
                  "outputsize": 5000 if history else 300, "timezone": "UTC", "apikey": self.api_key}
        try:
            data = requests.get(self.URL, params=params, timeout=30).json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"Twelve Data unreachable: {exc.__class__.__name__}") from exc
        if data.get("status") == "error":
            raise ProviderError(f"Twelve Data: {data.get('message', 'error')}")
        candles = []
        for v in reversed(data.get("values") or []):
            stamp = v["datetime"] if len(v["datetime"]) > 10 else v["datetime"] + " 00:00:00"
            candles.append({"time": datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc),
                            "open": float(v["open"]), "high": float(v["high"]), "low": float(v["low"]),
                            "close": float(v["close"]), "volume": float(v.get("volume") or 0)})
        return _closed_only(candles, timeframe)


class OandaProvider(BaseProvider):
    """Free practice account: real-time mid prices for forex and gold."""

    code = "oanda"
    native = {"5m", "15m", "30m", "1h", "4h", "1d"}
    GRANULARITY = {"5m": "M5", "15m": "M15", "30m": "M30", "1h": "H1", "4h": "H4", "1d": "D"}
    HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}

    def __init__(self, token, environment="practice"):
        if not token:
            raise ProviderError("OANDA needs an API token (Settings → Data source).")
        self.token = token
        self.host = self.HOSTS.get(environment, self.HOSTS["practice"])

    def symbol_for(self, instrument):
        return instrument.oanda_symbol or instrument.symbol.replace("/", "_")

    def fetch(self, instrument, timeframe, *, history=False):
        params = {"granularity": self.GRANULARITY[timeframe], "count": 5000 if history else 300, "price": "M",
                  "alignmentTimezone": "UTC", "dailyAlignment": 0}
        url = f"{self.host}/v3/instruments/{self.symbol_for(instrument)}/candles"
        try:
            response = requests.get(url, params=params, headers={"Authorization": f"Bearer {self.token}"}, timeout=30)
            data = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProviderError(f"OANDA unreachable: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise ProviderError(f"OANDA: {data.get('errorMessage', response.status_code)}")
        candles = []
        for c in data.get("candles", []):
            if not c.get("complete"):
                continue
            mid = c["mid"]
            candles.append({"time": datetime.fromisoformat(c["time"].replace("Z", "+00:00")[:26] + "+00:00"
                                                          if "." in c["time"] else c["time"].replace("Z", "+00:00")),
                            "open": float(mid["o"]), "high": float(mid["h"]), "low": float(mid["l"]),
                            "close": float(mid["c"]), "volume": float(c.get("volume", 0))})
        return candles


def get_provider(site_settings):
    code = site_settings.data_provider
    if code == "twelvedata":
        return TwelveDataProvider(site_settings.twelvedata_api_key)
    if code == "oanda":
        return OandaProvider(site_settings.oanda_api_token, site_settings.oanda_environment)
    return YahooProvider()
