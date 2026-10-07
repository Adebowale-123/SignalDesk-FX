from datetime import datetime, timedelta, timezone

from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Avg, Count, Q, Sum
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from apps.alerts.services import send_test
from apps.core import background
from apps.core.models import SiteSettings
from apps.macro.services import MacroData
from apps.market.models import Candle, Instrument
from apps.market.providers import ProviderError, get_provider
from apps.news.services import NewsCalendar, currencies_for

from .discovery import FAMILIES, combinations_per_family, discover, setup_label
from .engine import SESSION_NAMES, SESSIONS_UTC, active_sessions, market_open
from .forms import ProfileForm, SiteSettingsForm
from .models import BacktestRun, DiscoveryRun, PairState, Signal, StrategyProfile, TuningRun
from .services import backtest, load_features, run_cycle
from .tuning import tune_profile


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
        "tuning": profile.latest_tuning if profile else None,
        "news": _upcoming_news(profile, [r["instrument"] for r in rows]),
    })


def _upcoming_news(profile, instruments, hours=24):
    """Relevant scheduled news in the next day for the pairs on the board."""
    if not profile:
        return []
    currencies = sorted({c for i in instruments for c in currencies_for(i)})
    now = datetime.now(timezone.utc)
    events = NewsCalendar.load().upcoming(now - timedelta(minutes=profile.news_minutes_after), currencies,
                                          hours=hours, include_medium=profile.news_include_medium)
    window = timedelta(minutes=profile.news_minutes_before)
    for e in events:
        e["blocking"] = profile.news_filter and e["time"] - window <= now <= e["time"] + timedelta(
            minutes=profile.news_minutes_after)
    return events


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
    tunings = list(profile.tunings.all()[:6]) if profile else []
    return render(request, "signals/backtests.html", {
        "profile": profile, "profiles": profiles, "runs": runs, "latest": runs[0] if runs else None,
        "breakeven": breakeven, "running": bool(profile and background.is_running(f"backtest:{profile.pk}")),
        "tuning_running": bool(profile and background.is_running(f"tune:{profile.pk}")),
        "tuning": tunings[0] if tunings else None, "older_tunings": tunings[1:],
        "next_tune": (profile.last_tuned_at + timedelta(days=profile.tune_every_days))
        if profile and profile.auto_tune and profile.last_tuned_at else None,
    })


@staff_member_required
@require_POST
def tune_now(request, profile_id):
    profile = get_object_or_404(StrategyProfile, pk=profile_id)
    if background.is_running(f"backtest:{profile.pk}"):
        messages.info(request, "A backtest is running for this strategy. Try again when it has finished.")
    elif background.start(f"tune:{profile.pk}", tune_profile, profile.pk):
        messages.success(request, "Self-tuning started. It tests about 1,000 variations and takes a few minutes; "
                                  "this page refreshes until it's done.")
    else:
        messages.info(request, "Self-tuning is already running for this strategy.")
    return redirect(f"/backtests/?profile={profile.pk}")


@staff_member_required
@require_POST
def undo_tuning(request, pk):
    run = get_object_or_404(TuningRun, pk=pk, outcome=TuningRun.Outcome.ADOPTED, reverted=False)
    profile = run.profile
    profile.apply_params(run.previous_params)
    profile.save()
    run.reverted = True
    run.save(update_fields=["reverted"])
    background.start(f"backtest:{profile.pk}", backtest, profile, download=False)
    messages.success(request, "Previous settings restored. Confidence is being recalibrated in the background.")
    return redirect(f"/backtests/?profile={profile.pk}")


@staff_member_required
@require_POST
def run_backtest(request, profile_id):
    profile = get_object_or_404(StrategyProfile, pk=profile_id)
    if background.is_running(f"tune:{profile.pk}"):
        messages.info(request, "Self-tuning is running for this strategy and will run a backtest when it finishes.")
    elif background.start(f"backtest:{profile.pk}", backtest, profile):
        messages.success(request, "Backtest started. It takes about 1–5 minutes; this page refreshes until it's done.")
    else:
        messages.info(request, "A backtest for this strategy is already running.")
    return redirect(f"/backtests/?profile={profile.pk}")


@staff_member_required
@require_POST
def run_now(request):
    if background.is_running("engine"):
        messages.info(request, "An analysis is already running. Refresh in a moment.")
        return redirect(request.POST.get("next") or "signals:board")
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


# --- Research: fundamentals, sentiment and strategy discovery ------------------


def _direction(value, up, down, flat):
    if value is None or value != value:
        return {"text": "No data", "tone": ""}
    return {"text": up, "tone": "pos"} if value > 0 else {"text": down, "tone": "neg"} if value < 0 else \
        {"text": flat, "tone": ""}


def describe(params):
    if not params:
        return ""
    parts = [setup_label(params.get("setups") or []),
             f"min score {params['min_score']}", f"1:{params['risk_reward']:g}",
             "swing stop" if params["sl_method"] == "swing" else f"{params['atr_multiplier']:g}× ATR stop",
             f"ADX ≥ {params['adx_threshold']}",
             " + ".join(s.title().replace("Newyork", "New York") for s in params["sessions"]) or "any session"]
    macro = params.get("macro_filter", "off")
    if macro != "off":
        parts.append({"not_against": "skips trades fundamentals/sentiment oppose",
                      "aligned": "only when fundamentals/sentiment agree"}[macro])
    if params.get("avoid_crowded"):
        parts.append("avoids crowded trades")
    return "; ".join(parts)


def research(request):
    snap = MacroData.cached().snapshot()
    drivers, positioning = [], []
    if snap:
        drivers = [
            ("Risk mood", _direction(snap["mood"], "Risk-on", "Risk-off", "Mixed"),
             "S&P 500 trend and VIX stress. Risk-on lifts AUD, NZD, CAD; risk-off lifts JPY, CHF, gold, USD."),
            ("VIX (fear index)", {"text": f"{snap['vix']:.1f}", "tone": "neg" if snap["vix"] > 25 else ""},
             "Above 25, or up sharply from its average, counts as stress."),
            ("US dollar index", _direction(snap["dxy"], "Rising", "Falling", "Flat"),
             "Versus its 50-day average. A rising dollar favours USD over the other currencies and weighs on gold."),
            ("US 10-year yield", _direction(snap["yld"], "Rising", "Falling", "Steady"),
             "20-day change. Rising yields support USD and hurt gold."),
            ("Crude oil", _direction(snap["oil"], "Rising", "Falling", "Flat"), "Supports CAD when rising."),
        ]
        for currency, c in snap["cot"].items():
            idx = c["index"]
            positioning.append({"currency": "Gold" if currency == "XAU" else currency, "net": c["net"], "index": idx,
                                "as_of": c["as_of"],
                                "label": "Crowded long" if idx is not None and idx > 90 else
                                "Crowded short" if idx is not None and idx < 10 else
                                "Net long" if c["net"] > 0 else "Net short"})
    runs = list(DiscoveryRun.objects.prefetch_related("created")[:6])
    run = runs[0] if runs else None
    rows = []
    if run:
        for e in run.results:
            rows.append({**e, "summary": describe(e.get("best"))})
    site = SiteSettings.load()
    return render(request, "signals/research.html", {
        "snap": snap, "drivers": drivers, "positioning": positioning, "run": run, "rows": rows,
        "older": runs[1:], "running": background.is_running("discover"), "site": site,
        "discovered": StrategyProfile.objects.filter(discovered=True).order_by("-is_active", "-created_at"),
        "per_family": combinations_per_family(), "families": FAMILIES,
        "next_run": site.last_discovered_at + timedelta(days=site.discover_every_days)
        if site.auto_discover and site.last_discovered_at else None,
    })


@staff_member_required
@require_POST
def discover_now(request):
    if background.start("discover", discover):
        messages.success(request, "Strategy discovery started. It tests thousands of combinations and can take a "
                                  "while; this page refreshes until it's done.")
    else:
        messages.info(request, "Strategy discovery is already running.")
    return redirect("signals:research")
