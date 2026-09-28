from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.getenv("PORT", os.getenv("EXECUTOR_PORT", "8080")))
API_KEY = os.getenv("BINANCE_API_KEY", "").strip()
API_SECRET = os.getenv("BINANCE_API_SECRET", "").strip()
VERIFY_URL = os.getenv(
    "CLOUDFLARE_BRIDGE_VERIFY_URL",
    "https://tst-spot-signal.nurhanne-tarek13.workers.dev/make-bridge-verify",
).strip()
MAX_QUOTE = min(5.5, float(os.getenv("EXECUTOR_MAX_QUOTE_USDT", "5.5")))
STATE_PATH = Path(os.getenv("RAILWAY_V2_EXECUTOR_STATE", "/data/railway_v2_executor_state.json"))
PUBLIC_BASES = [
    "https://api.binance.com",
    "https://api-gcp.binance.com",
    "https://api1.binance.com",
    "https://api2.binance.com",
    "https://api3.binance.com",
    "https://api4.binance.com",
]
_lock = threading.RLock()


class BinanceError(RuntimeError):
    def __init__(self, http_status: int, code=None, msg: str = ""):
        self.http_status = int(http_status or 0)
        self.code = code
        self.msg = str(msg or "")
        super().__init__(f"BINANCE_ERROR http={self.http_status} code={self.code} msg={self.msg[:180]}")


def _json_bytes(value) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _safe_json(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8") or "{}")
    except Exception:
        return {}


def _load_state() -> dict:
    with _lock:
        try:
            row = json.loads(STATE_PATH.read_text())
            if isinstance(row, dict):
                row.setdefault("buys", {})
                row.setdefault("ocos", {})
                row.setdefault("unknown", {})
                return row
        except Exception:
            pass
        return {"buys": {}, "ocos": {}, "unknown": {}}


def _save_state(state: dict) -> None:
    with _lock:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, separators=(",", ":"), sort_keys=True))
        os.replace(tmp, STATE_PATH)


def _clean_id(value: str, prefix: str, limit: int = 32) -> str:
    base = "".join(ch for ch in str(value or "") if ch.isalnum())
    if not base:
        base = "sig"
    return (prefix + base)[:limit]


def _logical_signal_id(packed: str) -> str:
    return str(packed or "").split(".", 1)[0][:24]


def _signed_query(params: dict) -> str:
    pairs = [(k, str(v)) for k, v in params.items() if v is not None and v != ""]
    pairs += [("recvWindow", "5000"), ("timestamp", str(int(time.time() * 1000)))]
    query = urllib.parse.urlencode(pairs)
    sig = hmac.new(API_SECRET.encode(), query.encode(), hashlib.sha256).hexdigest()
    return query + "&signature=" + sig


def _binance(method: str, path: str, params: dict | None = None, timeout: int = 15):
    if not API_KEY or not API_SECRET:
        raise RuntimeError("BINANCE_CREDENTIALS_MISSING")
    query = _signed_query(params or {})
    last = None
    for base in PUBLIC_BASES:
        url = f"{base}{path}?{query}"
        req = urllib.request.Request(
            url,
            method=method,
            headers={"X-MBX-APIKEY": API_KEY, "User-Agent": "tst-railway-v2-executor/1.0"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return _safe_json(response.read())
        except urllib.error.HTTPError as exc:
            data = _safe_json(exc.read() or b"{}")
            raise BinanceError(exc.code, data.get("code"), data.get("msg") or "")
        except Exception as exc:
            last = exc
            continue
    raise RuntimeError(f"BINANCE_TRANSPORT_UNKNOWN:{type(last).__name__ if last else 'UNKNOWN'}")


def _public(path: str, timeout: int = 12):
    last = None
    for base in PUBLIC_BASES:
        try:
            req = urllib.request.Request(base + path, headers={"User-Agent": "tst-railway-v2-executor/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as response:
                return _safe_json(response.read())
        except Exception as exc:
            last = exc
    raise RuntimeError(f"PUBLIC_BINANCE_UNAVAILABLE:{type(last).__name__ if last else 'UNKNOWN'}")


def _verify_envelope(body: dict):
    raw = _json_bytes(body)
    req = urllib.request.Request(
        VERIFY_URL,
        data=raw,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "tst-railway-v2-executor/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            data = _safe_json(response.read())
            if response.status == 200 and data.get("ok") is True and data.get("status") == "BRIDGE_AUTH_OK":
                return data
            raise RuntimeError(f"BRIDGE_VERIFY_FAILED:{data.get('status') or response.status}")
    except urllib.error.HTTPError as exc:
        data = _safe_json(exc.read() or b"{}")
        raise PermissionError(str(data.get("status") or f"HTTP_{exc.code}"))


def _decimals(step: float) -> int:
    s = f"{step:.16f}".rstrip("0")
    return len(s.split(".")[1]) if "." in s else 0


def _floor_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    d = _decimals(step)
    return round(math.floor((value + 1e-12) / step) * step, d)


def _floor_tick(value: float, tick: float) -> float:
    return _floor_step(value, tick)


def _ceil_tick(value: float, tick: float) -> float:
    if tick <= 0:
        return value
    d = _decimals(tick)
    return round(math.ceil((value - 1e-12) / tick) * tick, d)


def _market_info(symbol: str) -> dict:
    info = _public("/api/v3/exchangeInfo?symbol=" + urllib.parse.quote(symbol))
    market = (info.get("symbols") or [None])[0]
    if not market or market.get("status") != "TRADING" or not market.get("isSpotTradingAllowed"):
        raise RuntimeError("PAIR_NOT_TRADABLE_SPOT")
    if market.get("quoteAsset") != "USDT":
        raise RuntimeError("QUOTE_ASSET_NOT_USDT")
    filters = {f.get("filterType"): f for f in market.get("filters") or []}
    lot = filters.get("LOT_SIZE") or {}
    price = filters.get("PRICE_FILTER") or {}
    notion = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
    return {
        "baseAsset": market.get("baseAsset"),
        "stepSize": float(lot.get("stepSize") or 0),
        "minQty": float(lot.get("minQty") or 0),
        "tickSize": float(price.get("tickSize") or 0),
        "minNotional": float(notion.get("minNotional") or 5),
        "ocoAllowed": bool(market.get("ocoAllowed")),
    }


def _order_by_client(symbol: str, client_id: str):
    try:
        return _binance("GET", "/api/v3/order", {"symbol": symbol, "origClientOrderId": client_id})
    except BinanceError as exc:
        if exc.code == -2013:
            return None
        raise


def _oco_by_client(client_id: str):
    try:
        return _binance("GET", "/api/v3/orderList", {"origClientOrderId": client_id})
    except BinanceError as exc:
        if exc.code in (-2013, -2011):
            return None
        raise


def _commission_summary(fills: list, base_asset: str) -> tuple[float, list]:
    base_commission = 0.0
    rows = []
    for fill in fills or []:
        commission = float(fill.get("commission") or 0)
        asset = str(fill.get("commissionAsset") or "")
        rows.append({"asset": asset, "commission": commission})
        if asset == base_asset:
            base_commission += commission
    return base_commission, rows


def _buy_payload(order: dict, symbol: str, logical_id: str, recovered: bool = False) -> dict:
    info = _market_info(symbol)
    gross_qty = float(order.get("executedQty") or 0)
    quote = float(order.get("cummulativeQuoteQty") or order.get("cumulativeQuoteQty") or 0)
    fills = order.get("fills") or []
    base_commission, commissions = _commission_summary(fills, str(info["baseAsset"] or ""))
    net_qty = max(0.0, gross_qty - base_commission)
    weighted = quote / gross_qty if gross_qty > 0 else 0.0
    return {
        "ok": gross_qty > 0 and quote > 0,
        "status": "BUY_FILLED" if gross_qty > 0 and quote > 0 else "BUY_FILL_QTY_MISSING",
        "signal_id": logical_id,
        "symbol": symbol,
        "order_id": order.get("orderId"),
        "client_order_id": order.get("clientOrderId"),
        "executed_qty": net_qty,
        "gross_executed_qty": gross_qty,
        "quote_spent": quote,
        "weighted_price": weighted,
        "base_asset": info["baseAsset"],
        "commissions": commissions,
        "recovered": recovered,
        "execution_provider": "RAILWAY_V2",
    }


def _validate_common(body: dict, action: str, allow_dry: bool = True) -> tuple[str, str]:
    logical_id = _logical_signal_id(body.get("signal_id"))
    symbol = str(body.get("symbol") or "").upper()
    if not logical_id:
        raise ValueError("MISSING_SIGNAL_ID")
    if not symbol.endswith("USDT") or not symbol.isalnum():
        raise ValueError("BAD_SYMBOL")
    if str(body.get("action") or "").upper() != action:
        raise ValueError("BAD_ACTION")
    ts = int(body.get("timestamp") or 0)
    if abs(int(time.time()) - ts) > 120:
        raise ValueError("STALE_REQUEST")
    dry = body.get("dry_run") is True and body.get("confirmed") is False
    if dry and allow_dry:
        return logical_id, symbol
    if body.get("confirmed") is not True or body.get("dry_run") is not False:
        raise ValueError("EXPLICIT_CONFIRMATION_REQUIRED")
    return logical_id, symbol


def _dry_probe(symbol: str) -> dict:
    info = _public("/api/v3/ticker/price?symbol=" + urllib.parse.quote(symbol))
    account = _binance("GET", "/api/v3/account", {"omitZeroBalances": "true"})
    return {
        "ok": bool(account.get("canTrade")) and float(info.get("price") or 0) > 0,
        "status": "RAILWAY_V2_DRY_PROBE_OK",
        "canTrade": bool(account.get("canTrade")),
        "symbol": symbol,
        "financialAction": False,
        "execution_provider": "RAILWAY_V2",
    }


def _execute_buy(body: dict, logical_id: str, symbol: str) -> tuple[int, dict]:
    quote = float(body.get("quote_amount_usdt") or 0)
    if not (5 <= quote <= MAX_QUOTE <= 5.5):
        return 400, {"ok": False, "status": "ORDER_SIZE_BLOCKED", "noOrderSent": True}
    client_id = _clean_id(logical_id, "TSTB")
    existing = _order_by_client(symbol, client_id)
    if existing:
        status = str(existing.get("status") or "").upper()
        if status == "FILLED":
            row = _buy_payload(existing, symbol, logical_id, recovered=True)
            state = _load_state()
            state["buys"][logical_id] = {**row, "at": time.time()}
            _save_state(state)
            return 200, row
        return 409, {
            "ok": False,
            "status": "BUY_ALREADY_EXISTS_NONFINAL",
            "binance_status": status,
            "reconciliation_required": True,
            "may_resend": False,
        }

    params = {
        "symbol": symbol,
        "side": "BUY",
        "type": "MARKET",
        "quoteOrderQty": f"{quote:.2f}",
        "newClientOrderId": client_id,
        "newOrderRespType": "FULL",
    }
    try:
        order = _binance("POST", "/api/v3/order", params)
    except BinanceError as exc:
        if exc.code == -2010 and "duplicate" in exc.msg.lower():
            existing = _order_by_client(symbol, client_id)
            if existing:
                return 200, _buy_payload(existing, symbol, logical_id, recovered=True)
        return 400, {
            "ok": False,
            "status": "BUY_REJECTED",
            "binance_code": exc.code,
            "noOrderSent": True,
        }
    except Exception:
        recovered = None
        try:
            recovered = _order_by_client(symbol, client_id)
        except Exception:
            recovered = None
        if recovered:
            row = _buy_payload(recovered, symbol, logical_id, recovered=True)
            if row.get("ok"):
                state = _load_state()
                state["buys"][logical_id] = {**row, "at": time.time()}
                _save_state(state)
                return 200, row
        state = _load_state()
        state["unknown"][logical_id + ":BUY"] = {"symbol": symbol, "at": time.time(), "client_order_id": client_id}
        _save_state(state)
        return 503, {
            "ok": False,
            "status": "BUY_SUBMISSION_UNKNOWN",
            "reconciliation_required": True,
            "may_resend": False,
        }

    row = _buy_payload(order, symbol, logical_id)
    if not row.get("ok"):
        return 503, {
            "ok": False,
            "status": "BUY_SUBMISSION_UNKNOWN",
            "reconciliation_required": True,
            "may_resend": False,
        }
    state = _load_state()
    state["buys"][logical_id] = {**row, "at": time.time()}
    state["unknown"].pop(logical_id + ":BUY", None)
    _save_state(state)
    return 200, row


def _emergency_close(logical_id: str, symbol: str, qty: float) -> tuple[int, dict]:
    client_id = _clean_id(logical_id, "TSTE")
    existing = _order_by_client(symbol, client_id)
    if existing and str(existing.get("status") or "").upper() == "FILLED":
        return 200, {
            "ok": True,
            "status": "PROTECTION_FAILED_EMERGENCY_CLOSED",
            "emergency_order_id": existing.get("orderId"),
            "recovered": True,
            "execution_provider": "RAILWAY_V2",
        }
    try:
        order = _binance(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": str(qty),
                "newClientOrderId": client_id,
                "newOrderRespType": "FULL",
            },
        )
        return 200, {
            "ok": True,
            "status": "PROTECTION_FAILED_EMERGENCY_CLOSED",
            "emergency_order_id": order.get("orderId"),
            "execution_provider": "RAILWAY_V2",
        }
    except Exception:
        try:
            existing = _order_by_client(symbol, client_id)
        except Exception:
            existing = None
        if existing and float(existing.get("executedQty") or 0) > 0:
            return 200, {
                "ok": True,
                "status": "PROTECTION_FAILED_EMERGENCY_CLOSED",
                "emergency_order_id": existing.get("orderId"),
                "recovered": True,
                "execution_provider": "RAILWAY_V2",
            }
        return 503, {
            "ok": False,
            "status": "EMERGENCY_CLOSE_UNKNOWN",
            "reconciliation_required": True,
            "may_resend": False,
        }


def _execute_oco(body: dict, logical_id: str, symbol: str) -> tuple[int, dict]:
    state = _load_state()
    buy = state["buys"].get(logical_id)
    if not buy or buy.get("status") != "BUY_FILLED" or buy.get("symbol") != symbol:
        return 409, {"ok": False, "status": "MATCHING_BUY_REQUIRED", "noOrderSent": True}

    requested_qty = float(body.get("quantity") or 0)
    available = float(buy.get("executed_qty") or 0)
    if not (requested_qty > 0 and requested_qty <= available + 1e-12):
        return 400, {"ok": False, "status": "OCO_QUANTITY_BLOCKED", "noOrderSent": True}

    info = _market_info(symbol)
    if not info["ocoAllowed"]:
        return 400, {"ok": False, "status": "OCO_NOT_SUPPORTED", "noOrderSent": True}

    qty = _floor_step(requested_qty, info["stepSize"])
    tp = _ceil_tick(float(body.get("take_profit_price") or 0), info["tickSize"])
    stop = _floor_tick(float(body.get("stop_loss_price") or 0), info["tickSize"])
    stop_limit = _floor_tick(float(body.get("stop_limit_price") or 0), info["tickSize"])
    if qty < info["minQty"] or qty <= 0:
        return 400, {"ok": False, "status": "OCO_QUANTITY_BELOW_MIN", "noOrderSent": True}
    if not (tp > stop > stop_limit > 0):
        return 400, {"ok": False, "status": "OCO_PRICE_GEOMETRY_BLOCKED", "noOrderSent": True}

    book = _public("/api/v3/ticker/bookTicker?symbol=" + urllib.parse.quote(symbol))
    mid = (float(book.get("bidPrice") or 0) + float(book.get("askPrice") or 0)) / 2
    if not (mid > 0 and qty * mid >= info["minNotional"]):
        return 400, {"ok": False, "status": "OCO_MIN_NOTIONAL_BLOCKED", "noOrderSent": True}

    list_id = str(body.get("list_client_order_id") or _clean_id(logical_id, "TSTL"))[:32]
    stop_id = str(body.get("stop_client_order_id") or _clean_id(logical_id, "TSTS"))[:32]
    limit_id = str(body.get("limit_client_order_id") or _clean_id(logical_id, "TSTT"))[:32]

    existing = _oco_by_client(list_id)
    if existing and int(existing.get("orderListId") or 0) > 0:
        row = {
            "ok": True,
            "status": "OCO_PLACED",
            "oco_order_list_id": existing.get("orderListId"),
            "recovered": True,
            "execution_provider": "RAILWAY_V2",
        }
        state["ocos"][logical_id] = {**row, "at": time.time()}
        _save_state(state)
        return 200, row

    params = {
        "symbol": symbol,
        "side": "SELL",
        "quantity": str(qty),
        "listClientOrderId": list_id,
        "aboveType": "LIMIT_MAKER",
        "abovePrice": str(tp),
        "aboveClientOrderId": limit_id,
        "belowType": "STOP_LOSS_LIMIT",
        "belowStopPrice": str(stop),
        "belowPrice": str(stop_limit),
        "belowClientOrderId": stop_id,
        "belowTimeInForce": "GTC",
        "newOrderRespType": "RESULT",
    }

    try:
        oco = _binance("POST", "/api/v3/orderList/oco", params)
    except BinanceError as exc:
        # Definite Binance rejection: verify no OCO exists before emergency close.
        try:
            existing = _oco_by_client(list_id)
        except Exception:
            existing = None
        if existing and int(existing.get("orderListId") or 0) > 0:
            row = {
                "ok": True,
                "status": "OCO_PLACED",
                "oco_order_list_id": existing.get("orderListId"),
                "recovered": True,
                "execution_provider": "RAILWAY_V2",
            }
            state["ocos"][logical_id] = {**row, "at": time.time()}
            _save_state(state)
            return 200, row
        code, closed = _emergency_close(logical_id, symbol, qty)
        closed["oco_reject_code"] = exc.code
        return code, closed
    except Exception:
        # Transport uncertainty: never blind-close because OCO may have landed.
        try:
            existing = _oco_by_client(list_id)
        except Exception:
            existing = None
        if existing and int(existing.get("orderListId") or 0) > 0:
            row = {
                "ok": True,
                "status": "OCO_PLACED",
                "oco_order_list_id": existing.get("orderListId"),
                "recovered": True,
                "execution_provider": "RAILWAY_V2",
            }
            state["ocos"][logical_id] = {**row, "at": time.time()}
            _save_state(state)
            return 200, row
        state["unknown"][logical_id + ":OCO"] = {"symbol": symbol, "at": time.time(), "list_client_order_id": list_id}
        _save_state(state)
        return 503, {
            "ok": False,
            "status": "OCO_SUBMISSION_UNKNOWN",
            "reconciliation_required": True,
            "may_resend": False,
        }

    order_list_id = int(oco.get("orderListId") or 0)
    if order_list_id <= 0:
        return 503, {
            "ok": False,
            "status": "OCO_SUBMISSION_UNKNOWN",
            "reconciliation_required": True,
            "may_resend": False,
        }
    row = {
        "ok": True,
        "status": "OCO_PLACED",
        "oco_order_list_id": order_list_id,
        "quantity": qty,
        "execution_provider": "RAILWAY_V2",
    }
    state["ocos"][logical_id] = {**row, "at": time.time()}
    state["unknown"].pop(logical_id + ":OCO", None)
    _save_state(state)
    return 200, row


class Handler(BaseHTTPRequestHandler):
    server_version = "TSTRailwayV2/1.0"

    def _reply(self, status: int, payload: dict):
        raw = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/health":
            return self._reply(404, {"ok": False, "status": "NOT_FOUND"})
        try:
            account = _binance("GET", "/api/v3/account", {"omitZeroBalances": "true"})
            return self._reply(
                200,
                {
                    "ok": bool(account.get("canTrade")),
                    "status": "RAILWAY_V2_HEALTHY" if account.get("canTrade") else "RAILWAY_V2_ACCOUNT_BLOCKED",
                    "canTrade": bool(account.get("canTrade")),
                    "execution_provider": "RAILWAY_V2",
                    "noBalanceValuesExposed": True,
                    "noSecretValuesExposed": True,
                },
            )
        except Exception as exc:
            return self._reply(
                503,
                {
                    "ok": False,
                    "status": "RAILWAY_V2_HEALTH_FAILED",
                    "reason": type(exc).__name__,
                    "execution_provider": "RAILWAY_V2",
                    "noSecretValuesExposed": True,
                },
            )

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/v2/buy", "/v2/oco"):
            return self._reply(404, {"ok": False, "status": "NOT_FOUND"})
        try:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = _safe_json(raw)
            if not isinstance(body, dict):
                raise ValueError("BAD_JSON")
            verified = _verify_envelope(body)
            action = "BUY" if path == "/v2/buy" else "OCO"
            logical_id, symbol = _validate_common(body, action)
            if body.get("dry_run") is True and body.get("confirmed") is False:
                return self._reply(200, _dry_probe(symbol))
            if verified.get("logicalSignalId") != logical_id:
                raise PermissionError("LOGICAL_SIGNAL_MISMATCH")
            if action == "BUY":
                code, payload = _execute_buy(body, logical_id, symbol)
            else:
                code, payload = _execute_oco(body, logical_id, symbol)
            return self._reply(code, payload)
        except PermissionError as exc:
            return self._reply(401, {"ok": False, "status": str(exc)[:80], "noOrderSent": True})
        except ValueError as exc:
            return self._reply(400, {"ok": False, "status": str(exc)[:80], "noOrderSent": True})
        except Exception as exc:
            return self._reply(
                503,
                {
                    "ok": False,
                    "status": "RAILWAY_V2_INTERNAL_ERROR",
                    "reason": type(exc).__name__,
                    "reconciliation_required": True,
                    "may_resend": False,
                },
            )

    def log_message(self, *_):
        return


if __name__ == "__main__":
    print(
        f"[railway-v2-executor] ONLINE port={PORT} configured={bool(API_KEY and API_SECRET)} max_quote={MAX_QUOTE}",
        flush=True,
    )
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
