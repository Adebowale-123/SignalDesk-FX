from django.db import models


class EconomicEvent(models.Model):
    """One scheduled economic release (times in UTC), from the ForexFactory weekly calendar."""

    class Impact(models.TextChoices):
        HIGH = "high", "High"
        MEDIUM = "medium", "Medium"
        LOW = "low", "Low"
        HOLIDAY = "holiday", "Holiday"

    title = models.CharField(max_length=160)
    currency = models.CharField(max_length=4, help_text='"USD", "EUR"… or "ALL".')
    time = models.DateTimeField()
    impact = models.CharField(max_length=10, choices=Impact.choices)
    forecast = models.CharField(max_length=30, blank=True)
    previous = models.CharField(max_length=30, blank=True)
    fetched_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["time"]
        constraints = [models.UniqueConstraint(fields=["title", "currency", "time"], name="unique_event")]
        indexes = [models.Index(fields=["time"])]

    def __str__(self):
        return f"{self.time:%a %d %b %H:%M} {self.currency} {self.title}"
