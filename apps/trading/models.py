from decimal import Decimal

from django.db import models


class AutoTrader(models.Model):
    """Settings and state of automatic trading through OANDA (one row)."""

    enabled = models.BooleanField(default=False, help_text="Master switch. Off = no new trades are opened.")
    account_id = models.CharField(max_length=40, blank=True, help_text='OANDA account ID, e.g. "101-004-1234567-001".')
    allow_live = models.BooleanField(
        default=False, help_text="Required before trading a LIVE (real money) OANDA account.")
    strategies = models.ManyToManyField("signals.StrategyProfile", blank=True,
                                        help_text="Strategies whose signals are traded. None ticked = all active.")
    risk_percent = models.DecimalField(max_digits=4, decimal_places=2, default=Decimal("1.00"),
                                       help_text="Account balance risked per trade (%), if the stop loss is hit.")
    max_open_trades = models.PositiveSmallIntegerField(default=3, help_text="Never more open trades than this.")
    daily_loss_limit = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("3.0"),
                                           help_text="Stop opening trades for the day after losing this % of the account.")
    weekly_loss_limit = models.DecimalField(max_digits=4, decimal_places=1, default=Decimal("6.0"),
                                            help_text="Stop opening trades for the week after losing this %.")
    max_slippage_r = models.DecimalField(
        max_digits=3, decimal_places=2, default=Decimal("0.25"),
        help_text="Skip a signal if price has already moved against it by more than this fraction of the stop "
                  "distance (0.25 = a quarter of the risk).")
    min_confidence = models.PositiveSmallIntegerField(
        default=0, help_text="Only trade signals with at least this confidence (%). 0 = trade every signal.")
    close_on_expiry = models.BooleanField(
        default=True, help_text="Close a trade at market when its signal expires, as the backtest does.")

    # State
    day_start = models.DateField(null=True, blank=True, editable=False)
    day_start_nav = models.FloatField(null=True, blank=True, editable=False)
    week_start = models.DateField(null=True, blank=True, editable=False)
    week_start_nav = models.FloatField(null=True, blank=True, editable=False)
    halted_until = models.DateTimeField(null=True, blank=True, editable=False)
    halt_reason = models.CharField(max_length=200, blank=True, editable=False)
    last_sync_at = models.DateTimeField(null=True, blank=True, editable=False)
    last_error = models.CharField(max_length=255, blank=True, editable=False)
    account_currency = models.CharField(max_length=4, blank=True, editable=False)
    balance = models.FloatField(null=True, blank=True, editable=False)
    nav = models.FloatField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = verbose_name_plural = "Auto-trader"

    def __str__(self):
        return "Auto-trader"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def traded_profiles(self):
        from apps.signals.models import StrategyProfile

        chosen = self.strategies.filter(is_active=True)
        return chosen if chosen.exists() else StrategyProfile.objects.filter(is_active=True)


class BrokerTrade(models.Model):
    """What the auto-trader did with one signal: the order it placed, or why it skipped it."""

    class Status(models.TextChoices):
        PENDING = "pending", "Placing"
        OPEN = "open", "Open"
        CLOSED = "closed", "Closed"
        SKIPPED = "skipped", "Skipped"
        REJECTED = "rejected", "Rejected by broker"
        ERROR = "error", "Error"

    signal = models.OneToOneField("signals.Signal", on_delete=models.CASCADE, related_name="broker_trade")
    instrument = models.ForeignKey("market.Instrument", on_delete=models.CASCADE, related_name="broker_trades")
    environment = models.CharField(max_length=10, default="practice")
    direction = models.CharField(max_length=4)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    units = models.IntegerField(default=0)
    requested_price = models.FloatField(null=True, blank=True)
    fill_price = models.FloatField(null=True, blank=True)
    stop_loss = models.FloatField(null=True, blank=True)
    take_profit = models.FloatField(null=True, blank=True)
    risk_amount = models.FloatField(null=True, blank=True, help_text="Planned loss at the stop, account currency.")
    broker_trade_id = models.CharField(max_length=40, blank=True)
    opened_at = models.DateTimeField(null=True, blank=True)
    closed_at = models.DateTimeField(null=True, blank=True)
    close_price = models.FloatField(null=True, blank=True)
    close_reason = models.CharField(max_length=20, blank=True)  # tp / sl / expired / closed
    realized_pl = models.FloatField(null=True, blank=True)
    message = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.instrument} {self.direction} {self.get_status_display()}"
