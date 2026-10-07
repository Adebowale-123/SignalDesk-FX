from django.contrib import admin

from .models import BacktestRun, PairState, Signal, StrategyProfile


@admin.register(StrategyProfile)
class StrategyProfileAdmin(admin.ModelAdmin):
    list_display = ("name", "entry_timeframe", "confirm_timeframes", "risk_reward", "min_score", "is_active")


@admin.register(Signal)
class SignalAdmin(admin.ModelAdmin):
    list_display = ("candle_time", "instrument", "profile", "direction", "entry", "stop_loss", "take_profit",
                    "confidence", "status", "pips")
    list_filter = ("status", "direction", "profile", "instrument")


@admin.register(PairState)
class PairStateAdmin(admin.ModelAdmin):
    list_display = ("profile", "instrument", "decision", "direction", "score", "confidence", "updated_at")


@admin.register(BacktestRun)
class BacktestRunAdmin(admin.ModelAdmin):
    list_display = ("started_at", "profile", "trades", "win_rate", "avg_r", "profit_factor")
