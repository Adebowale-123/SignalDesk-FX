import hmac
import json
import secrets
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings

from .forms import AutoTraderForm
from .models import AutoTrader, BrokerTrade
from .oanda import BrokerError
from .services import close_all, get_broker


def _readiness(trader, site):
    """What still needs doing before auto-trading can run, in order."""
    steps = []
    if trader.broker_type == AutoTrader.Broker.MT5:
        if not trader.bridge_online:
            steps.append("Start the bridge on your Windows computer with MetaTrader 5 open and logged in "
                         "(see “MetaTrader 5 bridge” below). This page shows Online when it is connected.")
        elif not trader.bridge_info.get("algo_trading", True):
            steps.append("Switch on “Algo Trading” in MetaTrader 5 (toolbar button).")
        if trader.bridge_account_is_real and not trader.allow_live:
            steps.append("The MT5 account is a REAL account: tick “Allow trading a LIVE / REAL account” to permit "
                         "real money, or log MT5 in to a demo account.")
        return steps
    if not site.oanda_api_token:
        steps.append("Add your OANDA API token in Settings → Data source.")
    if site.data_provider != "oanda":
        steps.append("Set the price source to OANDA in Settings → Data source (same token).")
    if not trader.account_id:
        steps.append("Enter your OANDA account ID below (or press Test connection to find it).")
    if site.oanda_environment == "live" and not trader.allow_live:
        steps.append("Your token is for a LIVE account: tick “Allow trading a LIVE account” to permit real money.")
    return steps


@staff_member_required
def dashboard(request):
    trader = AutoTrader.load()
    site = SiteSettings.load()
    form = AutoTraderForm(request.POST or None, instance=trader)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Auto-trading settings saved.")
        return redirect("trading:dashboard")
    trades = BrokerTrade.objects.select_related("instrument", "signal__profile")[:100]
    closed = BrokerTrade.objects.filter(status=BrokerTrade.Status.CLOSED)
    stats = closed.aggregate(n=Count("id"), wins=Count("id", filter=Q(realized_pl__gt=0)), pl=Sum("realized_pl"))
    return render(request, "trading/dashboard.html", {
        "trader": trader, "site": site, "form": form, "trades": trades, "stats": stats,
        "open_trades": BrokerTrade.objects.filter(status=BrokerTrade.Status.OPEN).select_related("instrument"),
        "steps": _readiness(trader, site), "mt5": trader.broker_type == AutoTrader.Broker.MT5,
        "live": trader.bridge_account_is_real if trader.broker_type == AutoTrader.Broker.MT5
        else site.oanda_environment == "live",
    })


@staff_member_required
@require_POST
def switch(request):
    trader = AutoTrader.load()
    site = SiteSettings.load()
    turning_on = not trader.enabled
    if turning_on and _readiness(trader, site):
        messages.error(request, "Finish the setup steps first.")
        return redirect("trading:dashboard")
    trader.enabled = turning_on
    trader.save(update_fields=["enabled"])
    if turning_on:
        real = trader.bridge_account_is_real if trader.broker_type == AutoTrader.Broker.MT5 else \
            site.oanda_environment == "live"
        messages.success(request, f"Auto-trading is ON ({'REAL MONEY' if real else 'demo'} account). New TRADE NOW "
                                  "signals will be traded automatically.")
    else:
        messages.success(request, "Auto-trading is OFF. No new trades will be opened; open trades keep their "
                                  "stop loss and take profit at the broker.")
    return redirect("trading:dashboard")


@staff_member_required
@require_POST
def test_connection(request):
    trader = AutoTrader.load()
    site = SiteSettings.load()
    try:
        broker = get_broker(trader, site)
        if not trader.account_id:
            accounts = broker.accounts()
            if not accounts:
                raise BrokerError("This token has no accounts.")
            trader.account_id = accounts[0]
            trader.save(update_fields=["account_id"])
            broker.account_id = accounts[0]
            messages.info(request, f"Found account {accounts[0]} and saved it.")
        s = broker.summary()
        broker.instrument_info("EUR_USD")
    except BrokerError as exc:
        messages.error(request, f"Connection failed: {exc}")
        return redirect("trading:dashboard")
    trader.account_currency, trader.balance, trader.nav = s["currency"], s["balance"], s["nav"]
    trader.save(update_fields=["account_currency", "balance", "nav"])
    messages.success(request, f"Connected to OANDA {site.oanda_environment} account {trader.account_id}: balance "
                              f"{s['balance']:,.2f} {s['currency']}, {s['open_trades']} open trade(s).")
    return redirect("trading:dashboard")


@staff_member_required
@require_POST
def close_all_view(request):
    trader = AutoTrader.load()
    trader.enabled = False
    trader.save(update_fields=["enabled"])
    try:
        closed, errors = close_all()
    except BrokerError as exc:
        messages.error(request, f"Could not reach OANDA: {exc}. Auto-trading is OFF.")
        return redirect("trading:dashboard")
    if trader.broker_type == AutoTrader.Broker.MT5:
        messages.success(request, f"Auto-trading switched OFF. The bridge is closing {closed} open trade(s) on its "
                                  "next check-in (within about 20 seconds, if it is running).")
        return redirect("trading:dashboard")
    messages.success(request, f"Auto-trading switched OFF and {closed} open trade(s) closed.")
    for e in errors:
        messages.error(request, e)
    return redirect("trading:dashboard")


@staff_member_required
@require_POST
def resume(request):
    trader = AutoTrader.load()
    trader.halted_until, trader.halt_reason = None, ""
    trader.day_start = trader.week_start = None  # measure losses from now
    trader.save()
    messages.success(request, "Loss-limit pause cleared. Losses are measured again from now.")
    return redirect("trading:dashboard")


# --- MetaTrader 5 bridge ------------------------------------------------------


@csrf_exempt
@require_POST
def bridge_api(request):
    """Check-in from the Windows bridge (authenticated with the bridge key, not a login)."""
    from .bridge import exchange

    key = AutoTrader.load().bridge_key
    if not key or not hmac.compare_digest(request.headers.get("X-Bridge-Key", ""), key):
        return JsonResponse({"ok": False, "error": "bad bridge key"}, status=403)
    try:
        payload = json.loads(request.body or b"{}")
    except ValueError:
        return JsonResponse({"ok": False, "error": "bad JSON"}, status=400)
    return JsonResponse(exchange(payload))


@staff_member_required
def download_bridge(request):
    """The bridge program with this site's address and key filled in."""
    source = (Path(settings.BASE_DIR) / "bridge" / "signaldesk_bridge.py").read_text(encoding="utf-8")
    site_url = request.build_absolute_uri("/").rstrip("/")
    text = source.replace('SITE_URL = "__SITE_URL__"', f'SITE_URL = "{site_url}"', 1) \
        .replace('BRIDGE_KEY = "__BRIDGE_KEY__"', f'BRIDGE_KEY = "{AutoTrader.load().bridge_key}"', 1)
    response = HttpResponse(text, content_type="text/x-python; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="signaldesk_bridge.py"'
    return response


@staff_member_required
@require_POST
def new_bridge_key(request):
    trader = AutoTrader.load()
    trader.bridge_key = secrets.token_urlsafe(32)
    trader.save(update_fields=["bridge_key"])
    messages.success(request, "New bridge key made. The old bridge file stops working: download it again.")
    return redirect("trading:dashboard")
