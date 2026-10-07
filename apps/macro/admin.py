from django.contrib import admin

from .models import CotReport, MacroBar


@admin.register(MacroBar)
class MacroBarAdmin(admin.ModelAdmin):
    list_display = ("code", "date", "close")
    list_filter = ("code",)


@admin.register(CotReport)
class CotReportAdmin(admin.ModelAdmin):
    list_display = ("currency", "report_date", "long", "short", "open_interest")
    list_filter = ("currency",)
