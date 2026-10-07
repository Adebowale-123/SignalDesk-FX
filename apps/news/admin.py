from django.contrib import admin

from .models import EconomicEvent


@admin.register(EconomicEvent)
class EconomicEventAdmin(admin.ModelAdmin):
    list_display = ("time", "currency", "impact", "title", "forecast", "previous")
    list_filter = ("impact", "currency")
    search_fields = ("title",)
