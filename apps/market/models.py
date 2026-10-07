from django.db import models

# Supported timeframes and their length in minutes.
TIMEFRAMES = {"5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1d": 1440}
TIMEFRAME_CHOICES = [(tf, tf) for tf in TIMEFRAMES]


class Instrument(models.Model):
    class Kind(models.TextChoices):
        FOREX = "forex", "Forex"
        METAL = "metal", "Metal"

    symbol = models.CharField(max_length=20, unique=True, help_text='Display name, e.g. "EUR/USD".')
    kind = models.CharField(max_length=10, choices=Kind.choices, default=Kind.FOREX)
    pip_size = models.DecimalField(max_digits=10, decimal_places=6, help_text="0.0001 for most pairs, 0.01 for JPY pairs, 0.1 for gold.")
    decimals = models.PositiveSmallIntegerField(default=5, help_text="Decimals shown for prices.")
    spread_pips = models.DecimalField(max_digits=6, decimal_places=2, default=1,
                                      help_text="Typical spread in pips. Backtests charge it on every trade.")
    yahoo_symbol = models.CharField(max_length=30, blank=True, help_text='e.g. "EURUSD=X", gold "GC=F".')
    twelvedata_symbol = models.CharField(max_length=30, blank=True, help_text='e.g. "EUR/USD", "XAU/USD".')
    oanda_symbol = models.CharField(max_length=30, blank=True, help_text='e.g. "EUR_USD", "XAU_USD".')
    is_active = models.BooleanField(default=True)
    sort_order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        ordering = ["sort_order", "symbol"]

    def __str__(self):
        return self.symbol

    @property
    def slug(self):
        return self.symbol.replace("/", "").lower()

    @property
    def currencies(self):
        parts = self.symbol.split("/")
        return parts if len(parts) == 2 else [self.symbol, ""]

    def fmt(self, price):
        return f"{price:.{self.decimals}f}" if price is not None else "—"


class Candle(models.Model):
    """One closed OHLC candle (times in UTC, at the candle's open)."""

    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="candles")
    timeframe = models.CharField(max_length=4, choices=TIMEFRAME_CHOICES)
    time = models.DateTimeField()
    open = models.FloatField()
    high = models.FloatField()
    low = models.FloatField()
    close = models.FloatField()
    volume = models.FloatField(default=0)

    class Meta:
        ordering = ["time"]
        constraints = [models.UniqueConstraint(fields=["instrument", "timeframe", "time"], name="unique_candle")]
        indexes = [models.Index(fields=["instrument", "timeframe", "-time"])]

    def __str__(self):
        return f"{self.instrument} {self.timeframe} {self.time:%Y-%m-%d %H:%M}"
