from django import forms

from apps.core.models import SiteSettings
from apps.market.models import TIMEFRAMES, Instrument

from .models import SESSION_CHOICES, StrategyProfile

TF_CHOICES = [(tf, tf.upper()) for tf in TIMEFRAMES]


class ProfileForm(forms.ModelForm):
    confirm_timeframes = forms.MultipleChoiceField(
        choices=TF_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        label="Trend confirmation timeframes",
        help_text="Higher timeframes that must agree before a trade. Must be bigger than the entry timeframe.",
    )
    sessions = forms.MultipleChoiceField(
        choices=SESSION_CHOICES, required=False, widget=forms.CheckboxSelectMultiple,
        label="Trading sessions", help_text="Only give signals during these sessions. None ticked = any time.",
    )
    instruments = forms.ModelMultipleChoiceField(
        queryset=Instrument.objects.filter(is_active=True), required=False, widget=forms.CheckboxSelectMultiple,
        label="Pairs", help_text="None ticked = every active pair.",
    )

    class Meta:
        model = StrategyProfile
        fields = ["name", "is_active", "entry_timeframe", "confirm_timeframes", "require_all_confirm", "instruments",
                  "sessions", "risk_reward", "sl_method", "atr_multiplier", "min_score", "alert_min_confidence",
                  "adx_threshold", "ema_fast", "ema_mid", "ema_slow", "rsi_length", "max_hold_bars",
                  "news_filter", "news_minutes_before", "news_minutes_after", "news_include_medium",
                  "auto_tune", "tune_every_days"]
        labels = {"is_active": "Active", "entry_timeframe": "Entry timeframe", "require_all_confirm": "All must agree",
                  "risk_reward": "Risk : reward (TP = this × SL)", "sl_method": "Stop loss placement",
                  "atr_multiplier": "ATR multiplier", "min_score": "Minimum setup score",
                  "alert_min_confidence": "Alert at confidence of at least (%)", "adx_threshold": "ADX trend threshold",
                  "ema_fast": "Fast EMA", "ema_mid": "Middle EMA", "ema_slow": "Slow EMA", "rsi_length": "RSI length",
                  "max_hold_bars": "Expire after (entry candles)", "news_filter": "Avoid high-impact news",
                  "news_minutes_before": "Minutes before news", "news_minutes_after": "Minutes after news",
                  "news_include_medium": "Also avoid medium-impact news", "auto_tune": "Self-tuning",
                  "tune_every_days": "Re-test every (days)"}

    def clean(self):
        data = super().clean()
        entry = data.get("entry_timeframe")
        confirm = data.get("confirm_timeframes") or []
        if entry:
            bad = [tf for tf in confirm if TIMEFRAMES[tf] <= TIMEFRAMES[entry]]
            if bad:
                self.add_error("confirm_timeframes",
                               f"Confirmation timeframes must be larger than the {entry} entry: remove {', '.join(bad)}.")
        if data.get("ema_fast") and data.get("ema_mid") and data.get("ema_slow"):
            if not data["ema_fast"] < data["ema_mid"] < data["ema_slow"]:
                self.add_error("ema_mid", "EMAs must go fast < middle < slow.")
        if data.get("tune_every_days") is not None and data["tune_every_days"] < 1:
            self.add_error("tune_every_days", "At least 1 day.")
        rr = data.get("risk_reward")
        if rr is not None and rr <= 0:
            self.add_error("risk_reward", "Must be above 0.")
        return data


class SecretInput(forms.PasswordInput):
    """Shows that a secret is saved without revealing it; leaving it blank keeps the saved value."""

    def __init__(self):
        super().__init__(render_value=False, attrs={"autocomplete": "off"})


class SiteSettingsForm(forms.ModelForm):
    class Meta:
        model = SiteSettings
        fields = ["data_provider", "twelvedata_api_key", "oanda_api_token", "oanda_environment",
                  "engine_interval_minutes", "telegram_bot_token", "telegram_chat_id", "alert_emails",
                  "alert_on_close"]
        widgets = {"twelvedata_api_key": SecretInput(), "oanda_api_token": SecretInput(),
                   "telegram_bot_token": SecretInput(), "alert_emails": forms.Textarea(attrs={"rows": 3})}
        labels = {"data_provider": "Price data source", "twelvedata_api_key": "Twelve Data API key",
                  "oanda_api_token": "OANDA API token", "oanda_environment": "OANDA account type",
                  "engine_interval_minutes": "Run analysis every (minutes)", "telegram_bot_token": "Telegram bot token",
                  "telegram_chat_id": "Telegram chat ID", "alert_emails": "Alert emails",
                  "alert_on_close": "Also alert when a signal hits TP or SL"}

    SECRETS = ("twelvedata_api_key", "oanda_api_token", "telegram_bot_token")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.SECRETS:
            if getattr(self.instance, name):
                self.fields[name].help_text = "Saved. Leave blank to keep it, or type a new one to replace it."
                self.fields[name].required = False

    def clean(self):
        data = super().clean()
        for name in self.SECRETS:
            if not data.get(name):
                data[name] = getattr(self.instance, name)
        if data.get("data_provider") == "twelvedata" and not data.get("twelvedata_api_key"):
            self.add_error("twelvedata_api_key", "Twelve Data needs an API key.")
        if data.get("data_provider") == "oanda" and not data.get("oanda_api_token"):
            self.add_error("oanda_api_token", "OANDA needs an API token.")
        if data.get("engine_interval_minutes") is not None and data["engine_interval_minutes"] < 1:
            self.add_error("engine_interval_minutes", "At least 1 minute.")
        return data
