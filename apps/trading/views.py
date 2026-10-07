from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Count, Q, Sum
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from apps.core.models import SiteSettings

from .forms import AutoTraderForm
from .models import AutoTrader, BrokerTrade
from .oanda import BrokerError
from .services import close_all, get_broker


def _readiness(trader, site):
    """What still needs doing before auto-trading can run, in order."""
    steps = []
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
        "steps": _readiness(trader, site), "live": site.oanda_environment == "live",
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
        messages.success(request, f"Auto-trading is ON ({'LIVE' if site.oanda_environment == 'live' else 'practice'}"
                                  " account). New TRADE NOW signals will be traded automatically.")
    else:
        messages.success(request, "Auto-trading is OFF. No new trades will be opened; open trades keep their "
                                  "stop loss and take profit at OANDA.")
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
