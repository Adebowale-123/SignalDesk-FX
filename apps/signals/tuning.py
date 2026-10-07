"""
Fast replay of a strategy over history, used by backtests and by self-tuning.

`scan()` runs the real `decide()` once per candle with the tunable filters switched off, and keeps every candle
that would be a trade. A variation of the settings (minimum score, ADX, sessions, stop method, risk-reward) is then
just a filter on those candidates plus its own stop and target, so `simulate()` can test a thousand variations
in seconds and still match the live engine exactly.

Self-tuning (walk-forward): pick the best variation on the older 70% of the data, then adopt it only if it is
also profitable on the newest 30%, which played no part in choosing it. That guards against settings that only
fit the past by luck.
"""

import copy
import itertools
import logging
from collections import defaultdict
from dataclasses import dataclass

import numpy as np
from django.utils import timezone as dj_tz

from .engine import active_sessions, decide

log = logging.getLogger("apps.signals")

SESSION_BITS = {"asia": 1, "london": 2, "newyork": 4}

SEARCH_SPACE = {
    "min_score": [55, 65, 75, 85],
    "risk_reward": [1.5, 2.0, 2.5, 3.0],
    "stop": [("swing", 1.5), ("atr", 1.0), ("atr", 1.5), ("atr", 2.0)],
    "adx_threshold": [15, 20, 25, 30],
    "sessions": [["london", "newyork"], ["london"], ["newyork"], ["asia", "london", "newyork"]],
}
TRAIN_SHARE = 0.7
MIN_TRAIN_TRADES = 30
MIN_TEST_TRADES = 20
MIN_TEST_PROFIT_FACTOR = 1.1


def session_mask(sessions):
    return sum(SESSION_BITS.get(s, 0) for s in sessions or [])


@dataclass
class PairData:
    """One pair's candles and the candles where the strategy could trade."""

    symbol: str
    times: object
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    spread: float
    pip: float
    idx: np.ndarray  # candidate candle positions
    direction: np.ndarray  # +1 buy, -1 sell
    score: np.ndarray
    adx: np.ndarray
    smask: np.ndarray
    entry: np.ndarray
    atr: np.ndarray
    swing: np.ndarray  # swing low for buys, swing high for sells (nan if none)


def scan(features, profile, instrument, news=None):
    """Run the engine over every candle with the tunable filters off and keep the trade candidates."""
    loose = copy.copy(profile)
    loose.min_score, loose.adx_threshold, loose.sessions = 0, 0, []
    rows = {k: [] for k in ("idx", "direction", "score", "adx", "smask", "entry", "atr", "swing")}
    columns = list(features.columns)
    values = features.to_numpy(dtype=object)
    times = features.index
    for i in range(len(features) - 1):
        row = dict(zip(columns, values[i]))
        row["time"] = times[i]
        d = decide(row, loose, instrument, news)
        if not d.is_trade:
            continue
        bull = d.direction == "buy"
        rows["idx"].append(i)
        rows["direction"].append(1 if bull else -1)
        rows["score"].append(d.score)
        rows["adx"].append(float(row["adx"]))
        rows["smask"].append(session_mask(active_sessions(row["close_time"])))
        rows["entry"].append(float(row["close"]))
        rows["atr"].append(float(row["atr"]))
        swing = row.get("swing_low") if bull else row.get("swing_high")
        rows["swing"].append(np.nan if swing is None else float(swing))
    arrays = {k: np.asarray(v, dtype=float) for k, v in rows.items()}
    for k in ("idx", "direction", "score", "smask"):
        arrays[k] = arrays[k].astype(int)
    return PairData(symbol=instrument.symbol, times=times, high=features["high"].to_numpy(dtype=float),
                    low=features["low"].to_numpy(dtype=float), close=features["close"].to_numpy(dtype=float),
                    spread=float(instrument.spread_pips) * float(instrument.pip_size), pip=float(instrument.pip_size),
                    **arrays)


def _risk(data, params):
    """Stop distance for every candidate, exactly as the engine places it."""
    atr = data.atr
    fallback = max(1.0, float(params["atr_multiplier"])) * atr
    if params["sl_method"] != "swing":
        return fallback
    stop = np.where(data.direction == 1, data.swing - 0.2 * atr, data.swing + 0.2 * atr)
    candidate = np.abs(data.entry - stop)
    use = ~np.isnan(candidate) & (candidate >= atr) & (candidate <= 3 * atr)
    return np.where(use, candidate, fallback)


def _outcome(data, i, direction, stop, target, max_hold):
    """Walk forward from candle i: stop loss counts first when both are hit in one candle."""
    start, end = i + 1, min(i + 1 + max_hold, len(data.close))
    span = end - start
    highs, lows = data.high[start:end], data.low[start:end]
    if direction == 1:
        sl_hit, tp_hit = lows <= stop, highs >= target
    else:
        sl_hit, tp_hit = highs >= stop, lows <= target
    first_sl = int(sl_hit.argmax()) if sl_hit.any() else span
    first_tp = int(tp_hit.argmax()) if tp_hit.any() else span
    if first_sl < span and first_sl <= first_tp:
        return "losses", stop, first_sl + 1
    if first_tp < span:
        return "wins", target, first_tp + 1
    if span == max_hold:
        return "expired", float(data.close[end - 1]), span
    return None, None, span  # still running when the data ends


def simulate(data, params, max_hold, start=0, stop=None):
    """Trades for one pair under `params`, one at a time, entering only on candles in [start, stop)."""
    stop = len(data.close) if stop is None else stop
    wanted = session_mask(params["sessions"])
    ok = (data.score >= params["min_score"]) & (data.adx >= params["adx_threshold"]) & \
         (data.idx >= start) & (data.idx < stop)
    if wanted:
        ok &= (data.smask & wanted) != 0
    picks = np.nonzero(ok)[0]
    if not len(picks):
        return []
    risk = _risk(data, params)
    rr = float(params["risk_reward"])
    trades, free_from = [], start
    for k in picks:
        i = data.idx[k]
        if i < free_from:
            continue
        d, entry, r_dist = data.direction[k], data.entry[k], risk[k]
        sl, tp = entry - d * r_dist, entry + d * rr * r_dist
        outcome, price, used = _outcome(data, i, d, sl, tp, max_hold)
        if outcome is None:
            break
        move = (price - entry) * d
        r = move / r_dist - (data.spread / r_dist if r_dist else 0)
        trades.append({"outcome": outcome, "r": float(r), "pips": float((move - data.spread) / data.pip),
                       "score": int(data.score[k])})
        free_from = i + used + 1
    return trades


def summarise(trades):
    wins = sum(t["outcome"] == "wins" for t in trades)
    losses = sum(t["outcome"] == "losses" for t in trades)
    rs = [t["r"] for t in trades]
    gains, pains = sum(r for r in rs if r > 0), -sum(r for r in rs if r < 0)
    return {"trades": len(trades), "wins": wins, "losses": losses, "expired": len(trades) - wins - losses,
            "win_rate": round(100 * wins / (wins + losses), 1) if wins + losses else None,
            "avg_r": round(float(np.mean(rs)), 3) if rs else None,
            "profit_factor": round(gains / pains, 2) if pains else (None if not gains else 99.0),
            "total_r": round(float(sum(rs)), 2)}


def buckets_of(trades):
    buckets = defaultdict(lambda: {"wins": 0, "losses": 0, "expired": 0})
    for t in trades:
        buckets[t["score"] // 10 * 10][t["outcome"]] += 1
    return {str(k): v for k, v in sorted(buckets.items())}


# ---------------------------------------------------------------------------
# Self-tuning
# ---------------------------------------------------------------------------


def variations():
    for min_score, rr, (sl_method, mult), adx, sessions in itertools.product(*SEARCH_SPACE.values()):
        yield {"min_score": min_score, "risk_reward": rr, "sl_method": sl_method, "atr_multiplier": mult,
               "adx_threshold": adx, "sessions": sessions}


def passes(stats):
    return (stats["trades"] >= MIN_TEST_TRADES and (stats["avg_r"] or 0) > 0
            and (stats["profit_factor"] or 0) >= MIN_TEST_PROFIT_FACTOR)


def same(a, b):
    return all(a.get(k) == b.get(k) if k != "sessions" else sorted(a.get(k) or []) == sorted(b.get(k) or [])
               for k in ("min_score", "risk_reward", "sl_method", "atr_multiplier", "adx_threshold", "sessions"))


def walk_forward(pairs, current, max_hold):
    """Choose on the older part, verify on the newer part. Returns a dict describing the result."""
    starts = [p.times[0] for p in pairs]
    ends = [p.times[-1] for p in pairs]
    first, last = min(starts), max(ends)
    cutoff = first + (last - first) * TRAIN_SHARE
    cuts = [int(np.searchsorted(p.times, cutoff)) for p in pairs]

    def evaluate(params, part):
        trades = []
        for p, cut in zip(pairs, cuts):
            trades += simulate(p, params, max_hold, *((0, cut) if part == "train" else (cut, None)))
        return summarise(trades)

    best, best_train, count = None, None, 0
    for params in variations():
        count += 1
        stats = evaluate(params, "train")
        if stats["trades"] < MIN_TRAIN_TRADES:
            continue
        if best_train is None or stats["total_r"] > best_train["total_r"]:
            best, best_train = params, stats
    result = {"variations": count, "train_start": first, "test_start": cutoff, "test_end": last,
              "current_train": evaluate(current, "train"), "current_test": evaluate(current, "test"),
              "best": best, "best_train": best_train or {}, "best_test": {}}
    if best is not None:
        result["best_test"] = evaluate(best, "test")
    return result


def tune_profile(profile_id, *, download=True):
    """Test variations of the profile's settings and adopt a better one only if it holds up on unseen data."""
    from apps.core.models import SiteSettings
    from apps.market.providers import get_provider
    from apps.news.services import NewsCalendar

    from .models import StrategyProfile, TuningRun
    from .services import backtest, prepare_features

    profile = StrategyProfile.objects.get(pk=profile_id)
    profile.last_tuned_at = dj_tz.now()  # recorded first, so a failure is not retried on every cycle
    StrategyProfile.objects.filter(pk=profile.pk).update(last_tuned_at=profile.last_tuned_at)
    provider = get_provider(SiteSettings.load())
    news = NewsCalendar.load()
    pairs, problems = [], []
    for instrument in profile.analysed_instruments():
        features, error = prepare_features(profile, instrument, provider, download=download)
        if error:
            problems.append(f"{instrument}: {error}")
            continue
        pairs.append(scan(features, profile, instrument, news))
    current = profile.tuned_params()
    if not pairs:
        return TuningRun.objects.create(profile=profile, outcome=TuningRun.Outcome.FAILED, previous_params=current,
                                        message="No price history available. " + "; ".join(problems))

    r = walk_forward(pairs, current, profile.max_hold_bars)
    best, best_test, current_test = r["best"], r["best_test"], r["current_test"]
    adopt = (best is not None and not same(best, current) and passes(best_test)
             and best_test["total_r"] > current_test["total_r"])
    period = (f"Chose on {r['train_start']:%d %b %Y}–{r['test_start']:%d %b %Y}, "
              f"checked on unseen data {r['test_start']:%d %b}–{r['test_end']:%d %b %Y}.")
    if adopt:
        profile.apply_params(best)
        profile.save(update_fields=[*profile.TUNED_FIELDS, "last_tuned_at"])  # leave other edits made meanwhile
        outcome, has_edge = TuningRun.Outcome.ADOPTED, True
        message = (f"Found better settings. On the unseen period they made {best_test['total_r']:+.1f}R over "
                   f"{best_test['trades']} trades (profit factor {best_test['profit_factor']}), against "
                   f"{current_test['total_r']:+.1f}R for the old settings. {period}")
    else:
        outcome, has_edge = TuningRun.Outcome.KEPT, passes(current_test)
        if has_edge:
            message = (f"Current settings still hold up: {current_test['total_r']:+.1f}R over "
                       f"{current_test['trades']} unseen trades (profit factor {current_test['profit_factor']}). "
                       f"No variation did clearly better. {period}")
        elif best is None:
            message = f"Not enough trades in the history to judge any variation. {period}"
        else:
            message = (f"No variation was profitable on the unseen period (best candidate: "
                       f"{best_test.get('total_r', 0):+.1f}R over {best_test.get('trades', 0)} trades). Settings kept, "
                       f"but this strategy has no proven edge right now: treat its signals with caution. {period}")
    proof = best_test if adopt else current_test
    if has_edge and (proof["trades"] < 100 or (proof["profit_factor"] or 0) < 1.3):
        message += (f" The evidence is still weak ({proof['trades']} unseen trades, profit factor "
                    f"{proof['profit_factor']}), so keep testing on a demo account before trusting it.")
    if problems:
        message += " Skipped: " + "; ".join(problems) + "."
    run = TuningRun.objects.create(
        profile=profile, outcome=outcome, has_edge=has_edge, variations=r["variations"],
        train_start=r["train_start"], test_start=r["test_start"], test_end=r["test_end"],
        previous_params=current, best_params=best or {}, current_train=r["current_train"],
        current_test=current_test, best_train=r["best_train"], best_test=best_test, message=message)
    backtest(profile, download=False)  # recalibrate confidence for the settings now in use
    log.info("Self-tuning %s: %s", profile, outcome)
    return run
