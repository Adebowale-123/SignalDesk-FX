from decimal import Decimal

from django.db import models

from apps.market.models import TIMEFRAME_CHOICES, TIMEFRAMES, Instrument

SESSION_CHOICES = [("asia", "Asia (Tokyo)"), ("london", "London"), ("newyork", "New York")]


def default_confirm():
    return ["1h", "4h"]


def default_sessions():
    return ["london", "newyork"]


class StrategyProfile(models.Model):
    """A set of rules you control: which timeframes, how strict, how to place SL/TP."""

    class SlMethod(models.TextChoices):
        SWING = "swing", "Below/above the last swing point"
        ATR = "atr", "A multiple of ATR (average range)"

    name = models.CharField(max_length=60, unique=True)
    is_active = models.BooleanField(default=True)
    entry_timeframe = models.CharField(max_length=4, choices=TIMEFRAME_CHOICES, default="15m",
                                       help_text="Timeframe used to time the entry.")
    confirm_timeframes = models.JSONField(default=default_confirm, blank=True,
                                          help_text="Higher timeframes that must agree on the trend.")
    require_all_confirm = models.BooleanField(default=True,
                                              help_text="On: every confirmation timeframe must agree. Off: most must agree.")
    instruments = models.ManyToManyField(Instrument, blank=True, help_text="Leave empty to analyse all active pairs.")
    risk_reward = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("2.00"),
                                      help_text="Take profit distance as a multiple of the stop loss distance.")
    sl_method = models.CharField(max_length=10, choices=SlMethod.choices, default=SlMethod.SWING)
    atr_multiplier = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.50"),
                                         help_text="Stop distance in ATRs (ATR method, and the fallback for swing).")
    min_score = models.PositiveSmallIntegerField(default=65, help_text="Minimum setup score (0-100) to say TRADE NOW.")
    alert_min_confidence = models.PositiveSmallIntegerField(default=60,
                                                            help_text="Only alert when confidence is at least this (%).")
    adx_threshold = models.PositiveSmallIntegerField(default=20, help_text="ADX needed to call the market trending.")
    ema_fast = models.PositiveSmallIntegerField(default=20)
    ema_mid = models.PositiveSmallIntegerField(default=50)
    ema_slow = models.PositiveSmallIntegerField(default=200)
    rsi_length = models.PositiveSmallIntegerField(default=14)
    sessions = models.JSONField(default=default_sessions, blank=True,
                                help_text="Only signal during these sessions. Empty = any time the market is open.")
    max_hold_bars = models.PositiveSmallIntegerField(default=48,
                                                     help_text="Close the signal as expired after this many entry candles.")
    # News: say WAIT around scheduled economic releases for either currency of the pair.
    news_filter = models.BooleanField(default=True, help_text="Say WAIT around high-impact economic news.")
    news_minutes_before = models.PositiveSmallIntegerField(default=30, help_text="Minutes before the release.")
    news_minutes_after = models.PositiveSmallIntegerField(default=30, help_text="Minutes after the release.")
    news_include_medium = models.BooleanField(default=False, help_text="Also avoid medium-impact news.")
    # Self-tuning: regularly test variations of these settings and adopt one only if it also wins on unseen data.
    auto_tune = models.BooleanField(default=True, help_text="Let the system test and improve these settings by itself.")
    tune_every_days = models.PositiveSmallIntegerField(default=7, help_text="How often it re-tests itself (days).")
    last_tuned_at = models.DateTimeField(null=True, blank=True, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def timeframes_label(self):
        confirm = ", ".join(self.confirm_timeframes) or "none"
        return f"{self.entry_timeframe} entry · {confirm} trend"

    def clean_confirm(self):
        entry = TIMEFRAMES[self.entry_timeframe]
        return sorted({tf for tf in self.confirm_timeframes if tf in TIMEFRAMES and TIMEFRAMES[tf] > entry},
                      key=TIMEFRAMES.get)

    def analysed_instruments(self):
        chosen = self.instruments.filter(is_active=True)
        return chosen if chosen.exists() else Instrument.objects.filter(is_active=True)

    TUNED_FIELDS = ("min_score", "risk_reward", "sl_method", "atr_multiplier", "adx_threshold", "sessions")

    def tuned_params(self):
        return {"min_score": self.min_score, "risk_reward": float(self.risk_reward), "sl_method": self.sl_method,
                "atr_multiplier": float(self.atr_multiplier), "adx_threshold": self.adx_threshold,
                "sessions": list(self.sessions or [])}

    def apply_params(self, params):
        for name in self.TUNED_FIELDS:
            if name in params:
                value = params[name]
                if name in ("risk_reward", "atr_multiplier"):
                    value = Decimal(str(value)).quantize(Decimal("0.01"))
                setattr(self, name, value)

    @property
    def latest_tuning(self):
        return self.tunings.first()


class PairState(models.Model):
    """The latest decision for one pair under one profile (what the board shows)."""

    profile = models.ForeignKey(StrategyProfile, on_delete=models.CASCADE, related_name="states")
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="states")
    decision = models.CharField(max_length=10, default="wait")  # trade / wait
    direction = models.CharField(max_length=4, blank=True)  # buy / sell / ""
    score = models.PositiveSmallIntegerField(default=0)
    confidence = models.PositiveSmallIntegerField(null=True, blank=True)
    price = models.FloatField(null=True, blank=True)
    entry = models.FloatField(null=True, blank=True)
    stop_loss = models.FloatField(null=True, blank=True)
    take_profit = models.FloatField(null=True, blank=True)
    headline = models.CharField(max_length=160, blank=True)
    reasons = models.JSONField(default=list, blank=True)
    summary = models.TextField(blank=True)
    candle_time = models.DateTimeField(null=True, blank=True)
    active_signal = models.ForeignKey("Signal", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    error = models.CharField(max_length=255, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["profile", "instrument"], name="one_state_per_pair")]
        ordering = ["instrument__sort_order"]


class Signal(models.Model):
    """A TRADE NOW call and how it turned out."""

    class Status(models.TextChoices):
        OPEN = "open", "Open"
        TP = "tp", "Take profit hit"
        SL = "sl", "Stop loss hit"
        EXPIRED = "expired", "Expired"

    profile = models.ForeignKey(StrategyProfile, on_delete=models.CASCADE, related_name="signals")
    instrument = models.ForeignKey(Instrument, on_delete=models.CASCADE, related_name="signals")
    direction = models.CharField(max_length=4)  # buy / sell
    candle_time = models.DateTimeField(help_text="Close of the candle that produced the signal (UTC).")
    entry = models.FloatField()
    stop_loss = models.FloatField()
    take_profit = models.FloatField()
    risk_reward = models.FloatField()
    score = models.PositiveSmallIntegerField()
    confidence = models.PositiveSmallIntegerField(null=True, blank=True)
    sample_size = models.PositiveIntegerField(default=0)
    reasons = models.JSONField(default=list)
    summary = models.TextField(blank=True)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.OPEN)
    closed_at = models.DateTimeField(null=True, blank=True)
    exit_price = models.FloatField(null=True, blank=True)
    result_r = models.FloatField(null=True, blank=True)
    pips = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-candle_time"]
        constraints = [models.UniqueConstraint(fields=["profile", "instrument", "candle_time"], name="one_signal_per_candle")]

    def __str__(self):
        return f"{self.instrument} {self.direction.upper()} @ {self.entry}"


class BacktestRun(models.Model):
    """Results of replaying a profile over past candles. Its score buckets calibrate live confidence."""

    profile = models.ForeignKey(StrategyProfile, on_delete=models.CASCADE, related_name="backtests")
    started_at = models.DateTimeField(auto_now_add=True)
    period_start = models.DateTimeField(null=True, blank=True)
    period_end = models.DateTimeField(null=True, blank=True)
    trades = models.PositiveIntegerField(default=0)
    wins = models.PositiveIntegerField(default=0)
    losses = models.PositiveIntegerField(default=0)
    expired = models.PositiveIntegerField(default=0)
    win_rate = models.FloatField(null=True, blank=True)
    avg_r = models.FloatField(null=True, blank=True)
    profit_factor = models.FloatField(null=True, blank=True)
    total_r = models.FloatField(default=0)
    buckets = models.JSONField(default=dict, blank=True)   # "70": {"wins": n, "losses": n, "expired": n}
    per_pair = models.JSONField(default=dict, blank=True)  # "EUR/USD": {...}
    notes = models.TextField(blank=True)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.profile} backtest {self.started_at:%Y-%m-%d %H:%M}"


class TuningRun(models.Model):
    """One self-tuning pass: many setting variations tested on older data, the best checked on newer, unseen data."""

    class Outcome(models.TextChoices):
        ADOPTED = "adopted", "Better settings adopted"
        KEPT = "kept", "Current settings kept"
        FAILED = "failed", "Could not run"

    profile = models.ForeignKey(StrategyProfile, on_delete=models.CASCADE, related_name="tunings")
    started_at = models.DateTimeField(auto_now_add=True)
    outcome = models.CharField(max_length=10, choices=Outcome.choices, default=Outcome.KEPT)
    has_edge = models.BooleanField(default=False, help_text="The settings in use were profitable on unseen data.")
    variations = models.PositiveIntegerField(default=0)
    train_start = models.DateTimeField(null=True, blank=True)
    test_start = models.DateTimeField(null=True, blank=True)
    test_end = models.DateTimeField(null=True, blank=True)
    previous_params = models.JSONField(default=dict, blank=True)
    best_params = models.JSONField(default=dict, blank=True)
    current_train = models.JSONField(default=dict, blank=True)
    current_test = models.JSONField(default=dict, blank=True)
    best_train = models.JSONField(default=dict, blank=True)
    best_test = models.JSONField(default=dict, blank=True)
    message = models.TextField(blank=True)
    reverted = models.BooleanField(default=False)

    class Meta:
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.profile} tuning {self.started_at:%Y-%m-%d %H:%M}: {self.get_outcome_display()}"

    @property
    def changes(self):
        """[(label, old, new)] for the settings that changed."""
        labels = {"min_score": "Minimum score", "risk_reward": "Risk : reward", "sl_method": "Stop loss method",
                  "atr_multiplier": "ATR multiplier", "adx_threshold": "ADX threshold", "sessions": "Sessions"}
        out = []
        for key, label in labels.items():
            old, new = self.previous_params.get(key), self.best_params.get(key)
            if new is not None and old != new:
                out.append((label, _fmt_param(key, old), _fmt_param(key, new)))
        return out


def _fmt_param(key, value):
    if key == "sessions":
        names = dict(SESSION_CHOICES)
        return ", ".join(names.get(s, s).split(" (")[0] for s in value) if value else "Any time"
    if key == "sl_method":
        return "Swing point" if value == "swing" else "ATR"
    if key == "risk_reward":
        return f"1:{value:g}"
    return value if not isinstance(value, float) else f"{value:g}"
