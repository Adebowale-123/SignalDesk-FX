from django.db import models


class AlertLog(models.Model):
    signal = models.ForeignKey("signals.Signal", null=True, blank=True, on_delete=models.SET_NULL, related_name="alerts")
    channel = models.CharField(max_length=20)  # telegram / email
    kind = models.CharField(max_length=20, default="new")  # new / closed / test
    ok = models.BooleanField(default=False)
    error = models.CharField(max_length=255, blank=True)
    sent_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-sent_at"]
