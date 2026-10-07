from datetime import datetime, timedelta, timezone

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Avg, Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.alerts.services import send_test
from apps.core.models import SiteSettings
from apps.market.models import Candle, Instrument
from apps.market.providers import ProviderError, get_provider

from .engine import SESSION_NAMES, SESSIONS_UTC, active_sessions, market_open
from .forms import ProfileForm, SiteSettingsForm
from .models import BacktestRun, PairState, Signal, StrategyProfile
from .services import backtest, load_features, run_cycle


def _profile(request):
    profiles = StrategyProfile.objects.filter(is_active=True)
    wanted = request.GET.get("profile")
    chosen = profiles.filter(pk=wanted).first() if wanted and wanted.isdigit() else None
    return chosen or profiles.first(), profiles


def _session_status():
    now = datetime.now(timezone.utc)
    current = active_sessions(now)
    return [{"name": SESSION_NAMES[k], "open": k in current, "hours": f"{a:02d}:00–{b:02d}:00 UTC"}
            for k, (a, b) in SESSIONS_UTC.items()], market_open(now)


def board(request):
    profile, profiles = _profile(request)
    rows, last_run = [], None
    if profile:
        states = {s.instrument_id: s for s in PairState.objects.filter(profile=profile)
                  .select_related("instrument", "active_signal")}
        for instrument in profile.analysed_instruments():
            state = states.get(instrument.pk)
            rows.append({"instrument": instrument, "state": state})
            if state and (last_run is None or state.updated_at > last_run):
                last_run = state.updated_at
    sessions, is_open = _session_status()
    return render(request, "signals/board.html", {
        "profile": profile, "profiles": profiles, "rows": rows, "last_run": last_run,
        "sessions": sessions, "market_open": is_open, "site": SiteSettings.load(),
        "calibrated": bool(profile and profile.backtests.exists()),
    })


def pair_detail(request, profile_id, slug):
    profile = get_object_or_404(StrategyProfile, pk=profile_id)
    instrument = next((i for i in Instrument.objects.all() if i.slug == slug), None)
    if instrument is None:
        raise Http404
    state = PairState.objects.filter(profile=profile, instrument=instrument).select_related("active_signal").first()
    signals = Signal.objects.filter(profile=profile, instrument=instrument)[:10]
    return render(request, "signals/pair_detail.html", {
        "profile": profile, "instrument": instrument, "state": state, "signals": signals,
        "active": state.active_signal if state and state.active_signal_id and
        state.active_signal.status == Signal.Status.OPEN else None,
        "run": profile.backtests.first(),
    })


def candles_json(request, profile_id, slug):
    profile = get_object_or_404(StrategyProfile, pk=profile_id)
    instrument = next((i for i in Instrument.objects.all() if i.slug == slug), None)
    if instrument is None:
        raise Http404
    try:
        features = load_features(profile, instrument, get_provider(SiteSettings.load()), limit=400)
    except ProviderError:
        features = None
    if features is None or features.empty:
        return JsonResponse({"candles": [], "ema": []})
    tail = features.tail(200)
    to_ts = lambda t: int(t.timestamp())  # noqa: E731
    return JsonResponse({
        "candles": [{"time": to_ts(t), "open": r.open, "high": r.high, "low": r.low, "close": r.close}
                    for t, r in tail.iterrows()],
        "ema_fast": [{"time": to_ts(t), "value": v} for t, v in tail["ema_f"].dropna().items()],
        "ema_mid": [{"time": to_ts(t), "value": v} for t, v in tail["ema_m"].dropna().items()],
        "decimals": instrument.decimals,
    })


def history(request):
    profile, profiles = _profile(request)
    signals = Signal.objects.select_related("instrument", "profile")
    if profile:
        signals = signals.filter(profile=profile)
    closed = signals.exclude(status=Signal.Status.OPEN)
    stats = closed.aggregate(
        total=Count("id"), wins=Count("id", filter=Q(status=Signal.Status.TP)),
        losses=Count("id", filter=Q(status=Signal.Status.SL)), avg_r=Avg("result_r"), total_r=Sum("result_r"))
    decided = (stats["wins"] or 0) + (stats["losses"] or 0)
    stats["win_rate"] = round(100 * stats["wins"] / decided, 1) if decided else None
    by_pair = (closed.values("instrument__symbol").annotate(
        n=Count("id"), wins=Count("id", filter=Q(status=Signal.Status.TP)),
        r=Sum("result_r"), pips=Sum("pips")).order_by("instrument__symbol"))
    return render(request, "signals/history.html", {
        "profile": profile, "profiles": profiles, "signals": signals[:200], "stats": stats, "by_pair": by_pair,
        "open_count": signals.filter(status=Signal.Status.OPEN).count(),
    })


def backtests(request):
    profile, profiles = _profile(request)
    runs = BacktestRun.objects.filter(profile=profile)[:10] if profile else []
    breakeven = round(100 / (1 + float(profile.risk_reward))) if profile else None
    return render(request, "signals/backtests.html", {"profile": profile, "profiles": profiles, "runs": runs,
                                                      "latest": runs[0] if runs else None, "breakeven": breakeven})


@staff_member_required
@require_POST
def run_backtest(request, profile_id):
    profile = get_object_or_404(StrategyProfile, pk=profile_id)
    try:
        run = backtest(profile)
    except ProviderError as exc:
        messages.error(request, f"Backtest could not download prices: {exc}")
        return redirect(f"/backtests/?profile={profile.pk}")
    messages.success(request, f"Backtest finished: {run.trades} trades, win rate {run.win_rate}%. "
                              "Confidence on the board now uses these results.")
    return redirect(f"/backtests/?profile={profile.pk}")


@staff_member_required
@require_POST
def run_now(request):
    summary = run_cycle()
    if summary.get("error"):
        messages.error(request, f"Analysis could not run: {summary['error']}")
    else:
        messages.success(request, f"Analysis updated: {summary.get('pairs', 0)} pairs, "
                                  f"{summary.get('new_signals', 0)} new signal(s).")
    return redirect(request.POST.get("next") or "signals:board")


# --- Settings --------------------------------------------------------------


@staff_member_required
def settings_page(request):
    site = SiteSettings.load()
    old_provider = site.data_provider
    form = SiteSettingsForm(request.POST or None, instance=site)
    if request.method == "POST" and form.is_valid():
        site = form.save()
        if site.data_provider != old_provider:
            Candle.objects.all().delete()  # prices from different sources must not mix
            messages.info(request, "Data source changed: stored prices were cleared and will be downloaded again.")
        messages.success(request, "Settings saved.")
        return redirect("signals:settings")
    return render(request, "signals/settings.html", {
        "form": form, "profiles": StrategyProfile.objects.all(), "instruments": Instrument.objects.all(),
    })


@staff_member_required
@require_POST
def toggle_instrument(request, pk):
    instrument = get_object_or_404(Instrument, pk=pk)
    instrument.is_active = not instrument.is_active
    instrument.save(update_fields=["is_active"])
    messages.success(request, f"{instrument} {'switched on' if instrument.is_active else 'switched off'}.")
    return redirect("signals:settings")


@staff_member_required
def profile_edit(request, pk=None):
    profile = get_object_or_404(StrategyProfile, pk=pk) if pk else None
    form = ProfileForm(request.POST or None, instance=profile)
    if request.method == "POST" and form.is_valid():
        profile = form.save()
        messages.success(request, f"Saved “{profile}”. Run a backtest to calibrate its confidence.")
        return redirect("signals:settings")
    return render(request, "signals/profile_form.html", {"form": form, "profile": profile})


@staff_member_required
@require_POST
def profile_delete(request, pk):
    profile = get_object_or_404(StrategyProfile, pk=pk)
    profile.delete()
    messages.success(request, f"Deleted “{profile}”.")
    return redirect("signals:settings")


@staff_member_required
@require_POST
def test_alert(request):
    sent = send_test()
    if sent:
        messages.success(request, f"Test alert sent via {', '.join(sent)}.")
    else:
        messages.error(request, "Nothing sent. Add a Telegram bot token and chat ID, or an email address, then save.")
    return redirect("signals:settings")
