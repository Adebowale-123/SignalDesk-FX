from django import forms

from apps.signals.forms import SecretInput
from apps.signals.models import StrategyProfile

from .models import AutoTrader


class AutoTraderForm(forms.ModelForm):
    strategies = forms.ModelMultipleChoiceField(
        queryset=StrategyProfile.objects.filter(is_active=True), required=False,
        widget=forms.CheckboxSelectMultiple, label="Strategies to trade",
        help_text="None ticked = every active strategy.")

    class Meta:
        model = AutoTrader
        fields = ["broker_type", "capital_environment", "capital_identifier", "capital_api_key",
                  "capital_password", "account_id", "strategies", "risk_percent", "max_open_trades", "daily_loss_limit",
                  "weekly_loss_limit", "max_slippage_r", "min_confidence", "close_on_expiry", "allow_live"]
        widgets = {"capital_api_key": SecretInput(), "capital_password": SecretInput()}
        labels = {"broker_type": "Broker connection", "capital_environment": "Capital.com account",
                  "capital_identifier": "Capital.com login email", "capital_api_key": "Capital.com API key",
                  "capital_password": "API key password", "account_id": "OANDA account ID", "risk_percent": "Risk per trade (%)",
                  "max_open_trades": "Maximum open trades", "daily_loss_limit": "Daily loss limit (%)",
                  "weekly_loss_limit": "Weekly loss limit (%)", "max_slippage_r": "Too late to enter after (× risk)",
                  "min_confidence": "Minimum confidence (%)", "close_on_expiry": "Close trades when the signal expires",
                  "allow_live": "Allow trading a LIVE / REAL (real money) account"}

    SECRETS = ("capital_api_key", "capital_password")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.SECRETS:
            if getattr(self.instance, name):
                self.fields[name].help_text = "Saved. Leave blank to keep it, or type a new one to replace it."

    def clean(self):
        data = super().clean()
        for name in self.SECRETS:
            if not data.get(name):
                data[name] = getattr(self.instance, name)
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
