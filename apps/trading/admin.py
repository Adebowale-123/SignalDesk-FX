from django.contrib import admin

from .models import AutoTrader, BrokerTrade


@admin.register(AutoTrader)
class AutoTraderAdmin(admin.ModelAdmin):
    list_display = ("__str__", "enabled", "account_id", "risk_percent", "max_open_trades", "halted_until")


@admin.register(BrokerTrade)
class BrokerTradeAdmin(admin.ModelAdmin):
    list_display = ("created_at", "instrument", "direction", "status", "units", "fill_price", "realized_pl",
                    "environment")
    list_filter = ("status", "environment", "instrument")
