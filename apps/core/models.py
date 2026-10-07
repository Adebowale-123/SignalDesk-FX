from django.db import models


class SiteSettings(models.Model):
    """One row of portal-wide settings, edited on the Settings page."""

    class Provider(models.TextChoices):
        YAHOO = "yahoo", "Yahoo Finance (free, no key, slight delay)"
        TWELVEDATA = "twelvedata", "Twelve Data (free key, 800 requests/day)"
        OANDA = "oanda", "OANDA (free practice account token)"

    data_provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.YAHOO)
    twelvedata_api_key = models.CharField(max_length=120, blank=True)
    oanda_api_token = models.CharField(max_length=200, blank=True)
    oanda_environment = models.CharField(
        max_length=10, choices=[("practice", "Practice (demo)"), ("live", "Live")], default="practice"
    )
    engine_interval_minutes = models.PositiveSmallIntegerField(
        default=5, help_text="How often the analysis runs (minutes)."
    )
    telegram_bot_token = models.CharField(max_length=200, blank=True)
    telegram_chat_id = models.CharField(max_length=100, blank=True, help_text="Your chat or group ID.")
    alert_emails = models.TextField(blank=True, help_text="One email address per line.")
    alert_on_close = models.BooleanField(default=True, help_text="Also alert when a signal hits TP or SL.")
    calendar_fetched_at = models.DateTimeField(null=True, blank=True, editable=False)
    calendar_ok_at = models.DateTimeField(null=True, blank=True, editable=False,
                                          help_text="Last successful economic calendar download.")
    macro_fetched_at = models.DateTimeField(null=True, blank=True, editable=False)
    auto_discover = models.BooleanField(
        default=True, help_text="Let the system build and test new strategies by itself, and switch on ones that pass.")
    discover_every_days = models.PositiveSmallIntegerField(default=30, help_text="How often it searches (days).")
    max_discovered_active = models.PositiveSmallIntegerField(
        default=3, help_text="At most this many discovered strategies switched on at once.")
    last_discovered_at = models.DateTimeField(null=True, blank=True, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = verbose_name_plural = "Site settings"

    def __str__(self):
        return "Site settings"

    def save(self, *args, **kwargs):
        self.pk = 1
        super().save(*args, **kwargs)

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def email_list(self):
        return [line.strip() for line in self.alert_emails.splitlines() if "@" in line]
