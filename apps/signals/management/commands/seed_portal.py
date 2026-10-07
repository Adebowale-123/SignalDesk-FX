from decimal import Decimal

from django.core.management.base import BaseCommand

from apps.core.models import SiteSettings
from apps.market.models import Instrument
from apps.signals.models import StrategyProfile

INSTRUMENTS = [
    # symbol, kind, pip, decimals, yahoo, twelvedata, oanda, typical spread (pips)
    ("EUR/USD", "forex", "0.0001", 5, "EURUSD=X", "EUR/USD", "EUR_USD", "1.0"),
    ("GBP/USD", "forex", "0.0001", 5, "GBPUSD=X", "GBP/USD", "GBP_USD", "1.4"),
    ("USD/JPY", "forex", "0.01", 3, "JPY=X", "USD/JPY", "USD_JPY", "1.2"),
    ("AUD/USD", "forex", "0.0001", 5, "AUDUSD=X", "AUD/USD", "AUD_USD", "1.2"),
    ("USD/CAD", "forex", "0.0001", 5, "CAD=X", "USD/CAD", "USD_CAD", "1.8"),
    ("USD/CHF", "forex", "0.0001", 5, "CHF=X", "USD/CHF", "USD_CHF", "1.6"),
    ("NZD/USD", "forex", "0.0001", 5, "NZDUSD=X", "NZD/USD", "NZD_USD", "1.8"),
    ("XAU/USD", "metal", "0.1", 2, "GC=F", "XAU/USD", "XAU_USD", "3.0"),
]

PROFILES = [
    {"name": "Intraday (15m entry, 1H + 4H trend)", "entry_timeframe": "15m", "confirm_timeframes": ["1h", "4h"],
     "sessions": ["london", "newyork"], "max_hold_bars": 48},
    {"name": "Swing (1H entry, 4H + Daily trend)", "entry_timeframe": "1h", "confirm_timeframes": ["4h", "1d"],
     "sessions": [], "max_hold_bars": 72, "is_active": False},
]


class Command(BaseCommand):
    help = "Create the default pairs and two starter strategy profiles (safe to run again)."

    def handle(self, *args, **options):
        for order, (symbol, kind, pip, decimals, yahoo, td, oanda, spread) in enumerate(INSTRUMENTS):
            Instrument.objects.update_or_create(symbol=symbol, defaults={
                "kind": kind, "pip_size": Decimal(pip), "decimals": decimals, "yahoo_symbol": yahoo,
                "spread_pips": Decimal(spread),
                "twelvedata_symbol": td, "oanda_symbol": oanda, "sort_order": order})
        for spec in PROFILES:
            spec = dict(spec)
            StrategyProfile.objects.get_or_create(name=spec.pop("name"), defaults=spec)
        SiteSettings.load()
        self.stdout.write(self.style.SUCCESS(
            f"{Instrument.objects.count()} pairs, {StrategyProfile.objects.count()} strategy profiles ready."))
