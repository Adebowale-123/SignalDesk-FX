from django.contrib import admin

from .models import Candle, Instrument


@admin.register(Instrument)
class InstrumentAdmin(admin.ModelAdmin):
    list_display = ("symbol", "kind", "pip_size", "decimals", "spread_pips", "yahoo_symbol", "twelvedata_symbol", "oanda_symbol",
                    "is_active", "sort_order")
    list_editable = ("is_active", "sort_order")


@admin.register(Candle)
class CandleAdmin(admin.ModelAdmin):
    list_display = ("instrument", "timeframe", "time", "open", "high", "low", "close")
    list_filter = ("instrument", "timeframe")
