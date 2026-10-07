from django.urls import path

from . import views

app_name = "signals"

urlpatterns = [
    path("", views.board, name="board"),
    path("pair/<int:profile_id>/<slug:slug>/", views.pair_detail, name="pair"),
    path("api/candles/<int:profile_id>/<slug:slug>/", views.candles_json, name="candles"),
    path("history/", views.history, name="history"),
    path("backtests/", views.backtests, name="backtests"),
    path("backtests/<int:profile_id>/run/", views.run_backtest, name="run_backtest"),
    path("run-now/", views.run_now, name="run_now"),
    path("settings/", views.settings_page, name="settings"),
    path("settings/pairs/<int:pk>/toggle/", views.toggle_instrument, name="toggle_instrument"),
    path("settings/strategies/new/", views.profile_edit, name="profile_new"),
    path("settings/strategies/<int:pk>/", views.profile_edit, name="profile_edit"),
    path("settings/strategies/<int:pk>/delete/", views.profile_delete, name="profile_delete"),
    path("settings/test-alert/", views.test_alert, name="test_alert"),
]
