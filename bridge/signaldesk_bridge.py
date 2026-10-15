"""
SignalDesk FX — MetaTrader 5 bridge (Windows).

Runs next to your MT5 terminal (FXTM, Exness, HFM…). Every few seconds it tells SignalDesk what MT5 knows and
places or closes trades as SignalDesk instructs. Every trade gets its stop loss and take profit at the broker.

Setup:
  1. Install MetaTrader 5 from your broker, log in to your DEMO account, and switch on "Algo Trading"
     (toolbar button) — also Tools → Options → Expert Advisors → "Allow algorithmic trading".
  2. Install Python 3 for Windows from python.org (tick "Add python.exe to PATH").
  3. Double-click this file, or run:  python signaldesk_bridge.py
     The first run installs the two packages it needs (MetaTrader5, requests).
Keep the window open: closing it stops automatic trading (open trades keep their stop loss and take profit).
"""

import json
import math
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone

# --- Filled in when you download this file from your SignalDesk Auto-trading page -------------------------
SITE_URL = "__SITE_URL__"
BRIDGE_KEY = "__BRIDGE_KEY__"
# ------------------------------------------------------------------------------------------------------------
TERMINAL_PATH = ""  # leave empty to use the MT5 terminal that is already running
MAGIC = 240611  # marks the trades this bridge opens
DEVIATION_POINTS = 30  # accepted price change while an order is filled
VERSION = "1.0"


def ensure_packages():
    try:
        import MetaTrader5  # noqa: F401
        import requests  # noqa: F401
    except ImportError:
        print("Installing MetaTrader5 and requests (first run only)…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "MetaTrader5", "requests"])


# ---------------------------------------------------------------------------
# Pure helpers (no MetaTrader needed; tested by SignalDesk)
# ---------------------------------------------------------------------------


def norm(symbol):
    return "".join(ch for ch in (symbol or "").upper() if ch.isalnum())


def resolve_symbol(wanted, available):
    """Broker symbol for e.g. "EURUSD": exact match first, then a suffixed name like "EURUSD.m" or "EURUSDm"."""
    exact = [s for s in available if norm(s) == norm(wanted)]
    if exact:
        return exact[0]
    near = sorted((s for s in available if norm(s).startswith(norm(wanted))), key=len)
    return near[0] if near else None


def lot_size(risk_money, sl_distance, tick_size, tick_value, step, minimum, maximum):
    """Lots so that hitting the stop loses about risk_money (rounded down to the broker's step)."""
    if sl_distance <= 0 or tick_size <= 0 or tick_value <= 0 or step <= 0:
        return 0.0
    loss_per_lot = sl_distance / tick_size * tick_value
    lots = math.floor(risk_money / loss_per_lot / step + 1e-9) * step
    lots = min(lots, maximum)
    decimals = max(0, -int(math.floor(math.log10(step)))) if step < 1 else 0
    lots = round(lots, decimals)
    return lots if lots >= minimum else 0.0


def levels(side, price, sl_distance, tp_distance, digits):
    if side == "buy":
        return round(price - sl_distance, digits), round(price + tp_distance, digits)
    return round(price + sl_distance, digits), round(price - tp_distance, digits)


def risk_of(lots, sl_distance, tick_size, tick_value):
    return lots * sl_distance / tick_size * tick_value if tick_size else 0.0


# ---------------------------------------------------------------------------
# MetaTrader
# ---------------------------------------------------------------------------


class Bridge:
    def __init__(self):
        import MetaTrader5 as mt5
        import requests

        self.mt5, self.requests = mt5, requests
        self.results = []
        self.symbols = None

    def log(self, text):
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {text}", flush=True)

    def connect(self):
        ok = self.mt5.initialize(TERMINAL_PATH) if TERMINAL_PATH else self.mt5.initialize()
        if not ok:
            raise RuntimeError(f"Cannot connect to MetaTrader 5: {self.mt5.last_error()}. Is MT5 open and logged in?")
        account = self.mt5.account_info()
        if account is None:
            raise RuntimeError("MetaTrader 5 is open but not logged in to an account.")
        mode = {0: "demo", 1: "contest", 2: "real"}.get(account.trade_mode, "unknown")
        self.log(f"Connected: {account.company} · {account.server} · account {account.login} ({mode.upper()})")
        terminal = self.mt5.terminal_info()
        if terminal is not None and not terminal.trade_allowed:
            self.log("WARNING: 'Algo Trading' is switched off in MT5 — trades will be refused until you switch it on.")

    # -- state for SignalDesk ---------------------------------------------------

    def snapshot(self):
        mt5 = self.mt5
        a = mt5.account_info()
        terminal = {"company": a.company, "server": a.server, "login": a.login, "bridge": VERSION,
                    "mode": {0: "demo", 1: "contest", 2: "real"}.get(a.trade_mode, "unknown"),
                    "algo_trading": bool(getattr(mt5.terminal_info(), "trade_allowed", False))}
        positions = [{"ticket": p.ticket, "symbol": p.symbol, "comment": p.comment, "volume": p.volume,
                      "open_price": p.price_open, "profit": p.profit, "ours": p.magic == MAGIC}
                     for p in (mt5.positions_get() or [])]
        now = datetime.now(timezone.utc)
        closed = {}
        opening = {}
        for d in mt5.history_deals_get(now - timedelta(days=7), now + timedelta(days=1)) or []:
            if d.magic != MAGIC:
                continue
            if d.entry == mt5.DEAL_ENTRY_IN:
                opening[d.position_id] = d
            elif d.entry in (mt5.DEAL_ENTRY_OUT, mt5.DEAL_ENTRY_OUT_BY):
                c = closed.setdefault(d.position_id, {"ticket": d.position_id, "profit": 0.0, "price": d.price,
                                                      "reason": "closed"})
                c["profit"] += d.profit + d.swap + d.commission + getattr(d, "fee", 0.0)
                c["price"] = d.price
                if d.reason == mt5.DEAL_REASON_TP:
                    c["reason"] = "tp"
                elif d.reason == mt5.DEAL_REASON_SL:
                    c["reason"] = "sl"
        for ticket, c in closed.items():  # the opening deal carries the order's comment and fill
            if ticket in opening:
                c["comment"], c["open_price"], c["volume"] = (opening[ticket].comment, opening[ticket].price,
                                                              opening[ticket].volume)
        return {"terminal": terminal, "positions": positions, "closed": list(closed.values()),
                "account": {"currency": a.currency, "balance": a.balance, "equity": a.equity},
                "results": self.results}

    # -- actions ----------------------------------------------------------------

    def find_symbol(self, wanted):
        if self.symbols is None:
            self.symbols = [s.name for s in (self.mt5.symbols_get() or [])]
        name = resolve_symbol(wanted, self.symbols)
        if name:
            self.mt5.symbol_select(name, True)
        return name

    def filling(self, info):
        mode = info.filling_mode
        if mode & 1:
            return self.mt5.ORDER_FILLING_FOK
        if mode & 2:
            return self.mt5.ORDER_FILLING_IOC
        return self.mt5.ORDER_FILLING_RETURN

    def already_done(self, order_id):
        mt5 = self.mt5
        for p in mt5.positions_get() or []:
            if p.comment == order_id and p.magic == MAGIC:
                return {"ticket": p.ticket, "price": p.price_open, "lots": p.volume, "sl": p.sl, "tp": p.tp}
        now = datetime.now(timezone.utc)
        for d in mt5.history_deals_get(now - timedelta(days=2), now + timedelta(days=1)) or []:
            if d.comment == order_id and d.magic == MAGIC and d.entry == mt5.DEAL_ENTRY_IN:
                return {"ticket": d.position_id, "price": d.price, "lots": d.volume, "sl": None, "tp": None}
        return None

    def place(self, o):
        mt5 = self.mt5
        result = {"id": o["id"]}
        done = self.already_done(o["id"])
        if done:
            return {**result, "status": "opened", **done, "message": "Already placed earlier."}
        if datetime.now(timezone.utc) > datetime.fromisoformat(o["expires_at"]):
            return {**result, "status": "skipped", "message": "Signal is too old by the time the bridge saw it."}
        name = self.find_symbol(o["symbol"])
        if not name:
            return {**result, "status": "rejected", "message": f"{o['symbol']} is not available on this MT5 account."}
        info, tick, account = mt5.symbol_info(name), mt5.symbol_info_tick(name), mt5.account_info()
        if info is None or tick is None:
            return {**result, "status": "error", "message": f"No price for {name}."}
        buy = o["side"] == "buy"
        price = tick.ask if buy else tick.bid
        sl, tp = levels(o["side"], price, o["sl_distance"], o["tp_distance"], info.digits)
        min_distance = info.trade_stops_level * info.point
        if min_distance and min(o["sl_distance"], o["tp_distance"]) < min_distance:
            return {**result, "status": "skipped", "message": "Stop too close for this broker's minimum distance."}
        tick_value = getattr(info, "trade_tick_value_loss", 0) or info.trade_tick_value
        risk_money = account.equity * o["risk_percent"] / 100
        lots = lot_size(risk_money, o["sl_distance"], info.trade_tick_size, tick_value, info.volume_step,
                        info.volume_min, info.volume_max)
        if lots <= 0:
            return {**result, "status": "skipped",
                    "message": f"Trade would be below the {info.volume_min} lot minimum at {o['risk_percent']}% risk."}
        request = {"action": mt5.TRADE_ACTION_DEAL, "symbol": name, "volume": lots,
                   "type": mt5.ORDER_TYPE_BUY if buy else mt5.ORDER_TYPE_SELL, "price": price, "sl": sl, "tp": tp,
                   "deviation": DEVIATION_POINTS, "magic": MAGIC, "comment": o["id"][:31],
                   "type_time": mt5.ORDER_TIME_GTC, "type_filling": self.filling(info)}
        r = mt5.order_send(request)
        if r is None:
            return {**result, "status": "error", "message": f"order_send failed: {mt5.last_error()}"}
        if r.retcode not in (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_PLACED):
            return {**result, "status": "rejected", "message": f"MT5 refused ({r.retcode}): {r.comment}"}
        placed = self.already_done(o["id"]) or {"ticket": r.order, "price": r.price or price}
        fill = placed.get("price") or price
        return {**result, "status": "opened", "ticket": placed["ticket"], "price": fill, "lots": lots,
                "sl": sl, "tp": tp, "risk": round(risk_of(lots, o["sl_distance"], info.trade_tick_size, tick_value), 2),
                "message": f"Opened {lots} lots on {name}."}

    def close(self, ticket):
        mt5 = self.mt5
        positions = mt5.positions_get(ticket=int(ticket)) or []
        for p in positions:
            info, tick = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
            buy = p.type == mt5.POSITION_TYPE_BUY
            r = mt5.order_send({"action": mt5.TRADE_ACTION_DEAL, "symbol": p.symbol, "volume": p.volume,
                                "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY, "position": p.ticket,
                                "price": tick.bid if buy else tick.ask, "deviation": DEVIATION_POINTS,
                                "magic": MAGIC, "comment": "signaldesk close", "type_time": mt5.ORDER_TIME_GTC,
                                "type_filling": self.filling(info)})
            self.log(f"Close {p.symbol} #{p.ticket}: {'done' if r and r.retcode == mt5.TRADE_RETCODE_DONE else r}")

    # -- loop -------------------------------------------------------------------

    def check_in(self):
        payload = self.snapshot()
        response = self.requests.post(f"{SITE_URL.rstrip('/')}/api/bridge/", data=json.dumps(payload, default=str),
                                      headers={"X-Bridge-Key": BRIDGE_KEY, "Content-Type": "application/json"},
                                      timeout=30)
        if response.status_code != 200:
            raise RuntimeError(f"SignalDesk answered {response.status_code}: {response.text[:200]}")
        self.results = []  # delivered
        return response.json()

    def run(self):
        self.connect()
        wait = 20
        while True:
            try:
                todo = self.check_in()
                wait = todo.get("poll_seconds", 20)
                if todo.get("note"):
                    self.log(f"SignalDesk: {todo['note']}")
                if todo.get("close_all"):
                    for p in self.mt5.positions_get() or []:
                        if p.magic == MAGIC:
                            self.close(p.ticket)
                for ticket in todo.get("close", []):
                    self.close(ticket)
                for o in todo.get("orders", []):
                    self.log(f"Order: {o['label']}")
                    outcome = self.place(o)
                    self.log(f"  → {outcome['status']}: {outcome.get('message', '')}")
                    self.results.append(outcome)
                if todo.get("orders"):
                    wait = 1  # report results straight away
            except Exception as exc:  # keep running through network hiccups
                self.log(f"Problem: {exc}")
                if self.mt5.account_info() is None:
                    self.log("Reconnecting to MetaTrader 5…")
                    self.mt5.shutdown()
                    try:
                        self.connect()
                    except Exception as again:
                        self.log(str(again))
                wait = 30
            time.sleep(wait)


if __name__ == "__main__":
    if SITE_URL.startswith("__") or BRIDGE_KEY == "__BRIDGE_KEY__":
        sys.exit("Download this file from your SignalDesk Auto-trading page: it fills in your site address and key.")
    ensure_packages()
    print(f"SignalDesk MT5 bridge {VERSION} — keep this window open. Press Ctrl+C to stop.\n")
    try:
        Bridge().run()
    except KeyboardInterrupt:
        print("Stopped. Open trades keep their stop loss and take profit at the broker.")
    except Exception as exc:
        print(f"\nStopped: {exc}")
        input("Press Enter to close.")
