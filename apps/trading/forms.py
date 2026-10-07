from django import forms

from apps.signals.models import StrategyProfile

from .models import AutoTrader


class AutoTraderForm(forms.ModelForm):
    strategies = forms.ModelMultipleChoiceField(
        queryset=StrategyProfile.objects.filter(is_active=True), required=False,
        widget=forms.CheckboxSelectMultiple, label="Strategies to trade",
        help_text="None ticked = every active strategy.")

    class Meta:
        model = AutoTrader
        fields = ["account_id", "strategies", "risk_percent", "max_open_trades", "daily_loss_limit",
                  "weekly_loss_limit", "max_slippage_r", "min_confidence", "close_on_expiry", "allow_live"]
        labels = {"account_id": "OANDA account ID", "risk_percent": "Risk per trade (%)",
                  "max_open_trades": "Maximum open trades", "daily_loss_limit": "Daily loss limit (%)",
                  "weekly_loss_limit": "Weekly loss limit (%)", "max_slippage_r": "Too late to enter after (× risk)",
                  "min_confidence": "Minimum confidence (%)", "close_on_expiry": "Close trades when the signal expires",
                  "allow_live": "Allow trading a LIVE (real money) account"}

    def clean(self):
        data = super().clean()
        risk = data.get("risk_percent")
        if risk is not None and not 0 < risk <= 5:
            self.add_error("risk_percent", "Between 0.01 and 5%. Most traders risk 0.5–2% per trade.")
        if data.get("max_open_trades") is not None and data["max_open_trades"] < 1:
            self.add_error("max_open_trades", "At least 1.")
        for name in ("daily_loss_limit", "weekly_loss_limit"):
            if data.get(name) is not None and data[name] <= 0:
                self.add_error(name, "Must be above 0.")
        slip = data.get("max_slippage_r")
        if slip is not None and not 0 < slip <= 1:
            self.add_error("max_slippage_r", "Between 0.01 and 1.")
        return data
