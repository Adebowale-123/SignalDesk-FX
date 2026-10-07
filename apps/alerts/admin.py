from django.contrib import admin

from .models import AlertLog


@admin.register(AlertLog)
class AlertLogAdmin(admin.ModelAdmin):
    list_display = ("sent_at", "channel", "kind", "ok", "error", "signal")
    list_filter = ("channel", "ok", "kind")
