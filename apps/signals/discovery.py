"""
Strategy discovery: the system builds new strategies from the engine's building blocks and keeps only the ones
that prove themselves on data they were not chosen on.

For each timeframe family it tries every combination of entry setups, fundamental/sentiment filter, crowded-trade
rule, minimum score, risk : reward, stop method, ADX filter and sessions. The best combination on the older 70% of
the history must then pass stricter checks on the newest 30% than weekly self-tuning uses. Passing strategies are
created (and switched on while there is room); every discovered strategy is re-tested weekly by self-tuning and
switched off automatically when it stops working.
"""

import logging

from django.db import transaction
from django.utils import timezone as dj_tz

from apps.market.models import Instrument

from .engine import SETUP_CHOICES, SETUP_ORDER
from .tuning import passes, same, scan, variations, walk_forward

log = logging.getLogger("apps.signals")

FAMILIES = [
    {"entry": "15m", "confirm": ["1h", "4h"], "max_hold": 48, "label": "15m entry, 1H + 4H trend"},
    {"entry": "30m", "confirm": ["4h"], "max_hold": 48, "label": "30m entry, 4H trend"},
    {"entry": "1h", "confirm": ["4h", "1d"], "max_hold": 72, "label": "1H entry, 4H + Daily trend"},
    {"entry": "4h", "confirm": ["1d"], "max_hold": 30, "label": "4H entry, Daily trend"},
]
SPACE = {
    "setups": [["pullback"], ["breakout"], ["rsi_cross"], ["macd_cross"], ["pullback", "breakout"],
               list(SETUP_ORDER)],
    "macro_filter": ["off", "not_against", "aligned"],
    "avoid_crowded": [False, True],
    "min_score": [55, 70, 85],
    "risk_reward": [1.5, 2.0, 3.0],
    "stop": [("swing", 1.5), ("atr", 1.5), ("atr", 2.0)],
    "adx_threshold": [15, 25],
    "sessions": [["london", "newyork"], ["asia", "london", "newyork"]],
}
# Stricter than weekly self-tuning, because thousands of combinations are searched.
MIN_TRAIN_TRADES = 60
MIN_TEST_TRADES = 30
MIN_PROFIT_FACTOR = 1.2


def combinations_per_family():
    n = 1
    for values in SPACE.values():
        n *= len(values)
    return n


def template(family):
    from .models import StrategyProfile

    return StrategyProfile(name="(search)", entry_timeframe=family["entry"], confirm_timeframes=family["confirm"],
                           max_hold_bars=family["max_hold"], setups=list(SETUP_ORDER))


def strategy_name(family, params):
    from .models import StrategyProfile

    short = {"pullback": "Pullback", "breakout": "Breakout", "rsi_cross": "RSI turn", "macd_cross": "MACD turn"}
    setups = "All setups" if len(params["setups"]) == len(SETUP_ORDER) else " + ".join(short[s] for s in params["setups"])
    base = f"Auto · {family['entry'].upper()} {setups} · {dj_tz.now():%b %Y}"
    name, n = base, 2
    while StrategyProfile.objects.filter(name=name).exists():
        name, n = f"{base} #{n}", n + 1
    return name


def discover(*, download=True):
    """Search every family; create strategies that pass. Returns the DiscoveryRun."""
    from apps.core.models import SiteSettings
    from apps.market.providers import get_provider
    from apps.news.services import NewsCalendar

    from .models import DiscoveryRun, StrategyProfile
    from .services import backtest, prepare_features

    site = SiteSettings.load()
    now = dj_tz.now()
    SiteSettings.objects.filter(pk=site.pk).update(last_discovered_at=now)
    run = DiscoveryRun.objects.create()
    provider = get_provider(site)
    news = NewsCalendar.load()
    instruments = list(Instrument.objects.filter(is_active=True))
    results, winners = [], []
    for family in FAMILIES:
        shape = template(family)
        pairs, problems = [], []
        for instrument in instruments:
            features, error = prepare_features(shape, instrument, provider, download=download)
            if error:
                problems.append(f"{instrument}: {error}")
                continue
            pairs.append(scan(features, shape, instrument, news))
        entry = {"family": family["label"], "passed": False, "problems": problems}
        if not pairs:
            entry["note"] = "No price history."
            results.append(entry)
            continue
        r = walk_forward(pairs, None, family["max_hold"], candidates=variations(SPACE),
                         min_train_trades=MIN_TRAIN_TRADES)
        del pairs
        best, train, test = r["best"], r["best_train"], r["best_test"]
        entry.update({"tested": r["variations"], "best": best, "train": train, "test": test,
                      "train_start": r["train_start"].isoformat(), "test_start": r["test_start"].isoformat(),
                      "test_end": r["test_end"].isoformat()})
        entry["passed"] = bool(best) and (train.get("profit_factor") or 0) >= MIN_PROFIT_FACTOR and passes(
            test, MIN_TEST_TRADES, MIN_PROFIT_FACTOR)
        results.append(entry)
        if entry["passed"]:
            winners.append((family, best, test))

    created, notes = [], []
    room = site.max_discovered_active - StrategyProfile.objects.filter(discovered=True, is_active=True).count()
    for family, params, test in sorted(winners, key=lambda w: -w[2]["total_r"]):
        twins = StrategyProfile.objects.filter(entry_timeframe=family["entry"], confirm_timeframes=family["confirm"])
        if any(same(p.tuned_params(), params) for p in twins):
            notes.append(f"{family['label']}: already have this strategy.")
            continue
        with transaction.atomic():
            profile = StrategyProfile(name=strategy_name(family, params), entry_timeframe=family["entry"],
                                      confirm_timeframes=family["confirm"], max_hold_bars=family["max_hold"],
                                      discovered=True, is_active=room > 0, last_tuned_at=now)
            profile.apply_params(params)
            profile.save()
        room -= 1
        created.append(profile)
        backtest(profile, download=False)  # calibrate its confidence
        if not profile.is_active:
            notes.append(f"{profile.name} was found but left off: already {site.max_discovered_active} discovered "
                         f"strategies running (raise the limit in Settings).")

    run.combinations = sum(e.get("tested", 0) for e in results)
    run.results = results
    run.finished = True
    if created:
        run.message = (f"Created {len(created)} new strateg{'y' if len(created) == 1 else 'ies'} that stayed "
                       f"profitable on unseen data: " + "; ".join(p.name for p in created) + ".")
    else:
        run.message = ("No new strategy passed the checks on unseen data this time. Nothing was added: the system "
                       "only adds strategies with evidence behind them.")
    if notes:
        run.message += " " + " ".join(notes)
    run.save()
    run.created.set(created)
    log.info("Discovery: %s", run.message)
    return run


def setup_label(keys):
    names = dict(SETUP_CHOICES)
    return ", ".join(names.get(k, k) for k in keys)
