"""Minimal Capital.com REST API client (https://open-api.capital.com) for placing and tracking trades."""

import requests

HOSTS = {"demo": "https://demo-api-capital.backend-capital.com",
         "live": "https://api-capital.backend-capital.com"}
# Capital.com's names ("epics") for our instruments where they differ from "EURUSD"-style symbols.
EPIC_OVERRIDES = {"XAU/USD": "GOLD"}


class BrokerError(Exception):
    pass


def epic_for(instrument):
    return EPIC_OVERRIDES.get(instrument.symbol, instrument.symbol.replace("/", "").upper())


def _value(rule):
    """Dealing rules come as {"unit": ..., "value": ...} (or a bare number)."""
    if isinstance(rule, dict):
        return rule.get("unit"), float(rule.get("value") or 0)
    return None, float(rule or 0)


class CapitalBroker:
    def __init__(self, api_key, identifier, password, environment="demo"):
        if not (api_key and identifier and password):
            raise BrokerError("Capital.com API key, login email and API password are needed (Auto-trading page).")
        self.api_key, self.identifier, self.password = api_key, identifier, password
        self.environment = environment if environment in HOSTS else "demo"
        self.host = HOSTS[self.environment]
        self.tokens = None

    # -- HTTP ---------------------------------------------------------------

    def _login(self):
        try:
            response = requests.post(f"{self.host}/api/v1/session", timeout=20,
                                     headers={"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"},
                                     json={"identifier": self.identifier, "password": self.password,
                                           "encryptedPassword": False})
        except requests.RequestException as exc:
            raise BrokerError(f"Capital.com unreachable ({exc.__class__.__name__})") from exc
        if response.status_code != 200:
            raise BrokerError(f"Capital.com login failed ({response.status_code}): {self._error(response)}")
        cst, token = response.headers.get("CST"), response.headers.get("X-SECURITY-TOKEN")
        if not cst or not token:
            raise BrokerError("Capital.com login did not return session tokens.")
        self.tokens = {"CST": cst, "X-SECURITY-TOKEN": token}

    @staticmethod
    def _error(response):
        try:
            return response.json().get("errorCode") or response.text[:120]
        except ValueError:
            return response.text[:120]

    def _request(self, method, path, retry=True, **kwargs):
        if self.tokens is None:
            self._login()
        try:
            response = requests.request(method, f"{self.host}{path}", timeout=20,
                                        headers={**self.tokens, "Content-Type": "application/json"}, **kwargs)
        except requests.RequestException as exc:
            raise BrokerError(f"Capital.com unreachable ({exc.__class__.__name__})") from exc
        if response.status_code == 401 and retry:  # session expired: log in again once
            self.tokens = None
            return self._request(method, path, retry=False, **kwargs)
        if response.status_code >= 400:
            raise BrokerError(f"Capital.com {response.status_code}: {self._error(response)}")
        try:
            return response.json()
        except ValueError:
            return {}

    # -- Account ----------------------------------------------------------------

    def summary(self):
        accounts = self._request("GET", "/api/v1/accounts").get("accounts") or []
        if not accounts:
            raise BrokerError("No Capital.com account found for this login.")
        a = next((x for x in accounts if x.get("preferred")), accounts[0])
        b = a.get("balance") or {}
        balance = float(b.get("balance") or 0)
        pnl = float(b.get("profitLoss") or 0)
        return {"currency": a.get("currency", ""), "balance": balance, "nav": balance + pnl,
                "account_id": a.get("accountId", ""), "account_type": a.get("accountType", "")}

    def positions(self):
        out = []
        for item in self._request("GET", "/api/v1/positions").get("positions") or []:
            p, m = item.get("position") or {}, item.get("market") or {}
            out.append({"deal_id": p.get("dealId"), "epic": m.get("epic"), "direction": p.get("direction"),
                        "size": float(p.get("size") or 0), "level": float(p.get("level") or 0),
                        "upl": float(p.get("upl") or 0), "created": p.get("createdDateUTC"),
                        "stop": p.get("stopLevel"), "profit": p.get("profitLevel")})
        return out

    # -- Markets ------------------------------------------------------------------

    def market(self, epic):
        """Live price and dealing rules for one instrument."""
        data = self._request("GET", f"/api/v1/markets/{epic}")
        instrument, snap, rules = data.get("instrument") or {}, data.get("snapshot") or {}, data.get("dealingRules") or {}
        bid, offer = snap.get("bid"), snap.get("offer")
        if bid is None or offer is None:
            raise BrokerError(f"No price for {epic}.")
        _, min_size = _value(rules.get("minDealSize"))
        stop_unit, stop_value = _value(rules.get("minStopOrProfitDistance"))
        _, size_step = _value(rules.get("minSizeIncrement") or rules.get("minDealSize"))
        decimals = snap.get("decimalPlacesFactor")
        mid = (float(bid) + float(offer)) / 2
        min_stop = mid * stop_value / 100 if stop_unit == "PERCENTAGE" else stop_value
        return {"bid": float(bid), "offer": float(offer), "status": snap.get("marketStatus", "TRADEABLE"),
                "decimals": int(decimals) if decimals is not None else 5,
                "currency": instrument.get("currency") or snap.get("currency") or "",
                "lot_size": float(instrument.get("lotSize") or snap.get("lotSize") or 1),
                "min_size": min_size or 1.0, "size_step": size_step or min_size or 1.0, "min_stop": min_stop}

    # -- Orders and positions ---------------------------------------------------------

    def open_position(self, epic, direction, size, stop_level, profit_level):
        data = self._request("POST", "/api/v1/positions", json={
            "epic": epic, "direction": direction.upper(), "size": size, "guaranteedStop": False,
            "stopLevel": stop_level, "profitLevel": profit_level})
        reference = data.get("dealReference")
        if not reference:
            raise BrokerError("Capital.com did not return a deal reference.")
        return self.confirm(reference)

    def confirm(self, reference):
        c = self._request("GET", f"/api/v1/confirms/{reference}")
        if c.get("dealStatus") != "ACCEPTED":
            raise BrokerError(f"Order rejected: {c.get('reason') or c.get('status') or 'unknown reason'}")
        opened = [d.get("dealId") for d in c.get("affectedDeals") or [] if d.get("status") in ("OPENED", None)]
        return {"deal_id": opened[0] if opened else c.get("dealId"), "level": float(c.get("level") or 0),
                "size": float(c.get("size") or 0)}

    def close_position(self, deal_id):
        data = self._request("DELETE", f"/api/v1/positions/{deal_id}")
        reference = data.get("dealReference")
        level = None
        if reference:
            try:
                level = float(self._request("GET", f"/api/v1/confirms/{reference}").get("level") or 0) or None
            except BrokerError:
                pass
        return {"level": level}

    def close_reason(self, deal_id, since):
        """'tp' / 'sl' / 'closed' and the closing level, from the account's activity history."""
        params = {"from": since.strftime("%Y-%m-%dT%H:%M:%S"), "detailed": "true", "dealId": deal_id}
        items = self._request("GET", "/api/v1/history/activity", params=params).get("activities") or []
        reason, level = "closed", None
        for a in items:
            source = a.get("source")
            if source == "TP":
                reason = "tp"
            elif source == "SL":
                reason = "sl"
            details = a.get("details") or {}
            if details.get("level") is not None and source in ("TP", "SL", "USER", "SYSTEM", "CLOSE_OUT"):
                level = float(details["level"])
        return reason, level
