"""Minimal OANDA v20 REST client for placing and tracking trades."""

import requests

HOSTS = {"practice": "https://api-fxpractice.oanda.com", "live": "https://api-fxtrade.oanda.com"}


class BrokerError(Exception):
    pass


class OandaBroker:
    def __init__(self, token, account_id="", environment="practice"):
        if not token:
            raise BrokerError("No OANDA API token (Settings → Data source).")
        self.token, self.account_id = token, account_id
        self.environment = environment if environment in HOSTS else "practice"
        self.host = HOSTS[self.environment]
        self._instruments = None

    # -- HTTP ---------------------------------------------------------------

    def _request(self, method, path, **kwargs):
        try:
            response = requests.request(method, f"{self.host}{path}", timeout=20, headers={
                "Authorization": f"Bearer {self.token}", "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339"}, **kwargs)
        except requests.RequestException as exc:
            raise BrokerError(f"OANDA unreachable ({exc.__class__.__name__})") from exc
        try:
            data = response.json()
        except ValueError:
            data = {}
        if response.status_code >= 400:
            reject = (data.get("orderRejectTransaction") or {}).get("rejectReason")
            raise BrokerError(f"OANDA {response.status_code}: {data.get('errorMessage') or reject or 'error'}")
        return data

    def _account_path(self, suffix=""):
        if not self.account_id:
            raise BrokerError("No OANDA account ID set.")
        return f"/v3/accounts/{self.account_id}{suffix}"

    # -- Account --------------------------------------------------------------

    def accounts(self):
        return [a["id"] for a in self._request("GET", "/v3/accounts").get("accounts", [])]

    def summary(self):
        a = self._request("GET", self._account_path("/summary"))["account"]
        return {"currency": a["currency"], "balance": float(a["balance"]), "nav": float(a["NAV"]),
                "open_trades": int(a.get("openTradeCount", 0)), "margin_available": float(a.get("marginAvailable", 0)),
                "unrealized_pl": float(a.get("unrealizedPL", 0))}

    def instrument_info(self, name):
        if self._instruments is None:
            data = self._request("GET", self._account_path("/instruments"))
            self._instruments = {i["name"]: {"precision": int(i.get("displayPrecision", 5)),
                                             "units_precision": int(i.get("tradeUnitsPrecision", 0)),
                                             "min_units": float(i.get("minimumTradeSize", 1))}
                                 for i in data.get("instruments", [])}
        if name not in self._instruments:
            raise BrokerError(f"{name} is not tradable on this OANDA account.")
        return self._instruments[name]

    def open_trades(self):
        trades = self._request("GET", self._account_path("/openTrades")).get("trades", [])
        return [{"id": t["id"], "instrument": t["instrument"], "units": float(t["currentUnits"])} for t in trades]

    # -- Prices -------------------------------------------------------------

    def price(self, name):
        """Current bid/ask and how to turn a loss in the quote currency into the account currency."""
        data = self._request("GET", self._account_path("/pricing"),
                             params={"instruments": name, "includeHomeConversions": "true"})
        prices = data.get("prices") or []
        if not prices:
            raise BrokerError(f"No price for {name}.")
        p = prices[0]
        quote = name.split("_")[1]
        loss_factor = None
        for conv in data.get("homeConversions") or []:
            if conv.get("currency") == quote:
                loss_factor = float(conv.get("accountLoss") or conv.get("positionValue"))
        if loss_factor is None:
            factors = p.get("quoteHomeConversionFactors") or {}
            loss_factor = float(factors.get("negativeUnits") or factors.get("positiveUnits") or 0) or None
        if loss_factor is None:
            raise BrokerError(f"Cannot convert {quote} to the account currency.")
        return {"bid": float(p["bids"][0]["price"]), "ask": float(p["asks"][0]["price"]),
                "tradeable": p.get("tradeable", True), "loss_factor": loss_factor}

    # -- Orders and trades ---------------------------------------------------

    def market_order(self, name, units, stop_loss, take_profit, precision, client_id, comment=""):
        """Market order with stop loss and take profit attached at the broker. Returns the fill or raises."""
        fmt = f"{{:.{precision}f}}"
        order = {"order": {
            "type": "MARKET", "instrument": name, "units": str(units), "timeInForce": "FOK",
            "positionFill": "DEFAULT",
            "stopLossOnFill": {"price": fmt.format(stop_loss), "timeInForce": "GTC"},
            "takeProfitOnFill": {"price": fmt.format(take_profit), "timeInForce": "GTC"},
            "clientExtensions": {"id": client_id, "tag": "signaldesk", "comment": comment[:120]},
            "tradeClientExtensions": {"id": client_id, "tag": "signaldesk", "comment": comment[:120]},
        }}
        data = self._request("POST", self._account_path("/orders"), json=order)
        fill = data.get("orderFillTransaction") or {}
        opened = fill.get("tradeOpened")
        if not opened:
            reason = (data.get("orderCancelTransaction") or {}).get("reason") or "order not filled"
            raise BrokerError(f"Order cancelled: {reason}")
        return {"trade_id": opened["tradeID"], "units": float(opened["units"]),
                "price": float(opened.get("price") or fill.get("price"))}

    def trade(self, trade_id):
        t = self._request("GET", self._account_path(f"/trades/{trade_id}"))["trade"]
        reason = "closed"
        if (t.get("takeProfitOrder") or {}).get("state") == "FILLED":
            reason = "tp"
        elif (t.get("stopLossOrder") or {}).get("state") == "FILLED":
            reason = "sl"
        return {"id": t["id"], "state": t["state"], "realized_pl": float(t.get("realizedPL") or 0),
                "price": float(t.get("price") or 0), "units": float(t.get("initialUnits") or 0),
                "unrealized_pl": float(t.get("unrealizedPL") or 0),
                "close_price": float(t["averageClosePrice"]) if t.get("averageClosePrice") else None,
                "close_time": t.get("closeTime"), "reason": reason}

    def close_trade(self, trade_id):
        data = self._request("PUT", self._account_path(f"/trades/{trade_id}/close"), json={"units": "ALL"})
        fill = data.get("orderFillTransaction") or {}
        return {"price": float(fill["price"]) if fill.get("price") else None,
                "realized_pl": float(fill.get("pl") or 0)}


def client_id(signal):
    """The same for the same signal on any copy of the portal, so OANDA refuses a second order for it."""
    strategy = "".join(ch for ch in signal.profile.name.lower() if ch.isalnum())[:24]
    return f"sd-{signal.instrument.slug}-{strategy}-{int(signal.candle_time.timestamp())}"

