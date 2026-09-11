from __future__ import annotations

import hashlib
import hmac
import json
import time
from decimal import Decimal
from typing import Any, Callable, Mapping
from urllib.parse import urlencode

import httpx


class BinanceAPIError(RuntimeError):
    def __init__(
        self,
        *,
        status_code: int,
        code: int | None,
        message: str,
        payload: Any = None,
    ) -> None:
        super().__init__(f"Binance API error {status_code} code={code}: {message}")
        self.status_code = int(status_code)
        self.code = code
        self.message = message
        self.payload = payload

    @property
    def execution_unknown(self) -> bool:
        # Binance documents 5xx responses as potentially having unknown
        # execution status. Never blindly retry a trading request in this case.
        return self.status_code >= 500


class BinanceSignedSpotClient:
    """Minimal signed Spot REST client for the future V2 live adapter.

    This module deliberately contains no retry loop for trading endpoints.
    Callers must reconcile by client order ID after transport errors or any
    response whose execution status is uncertain.
    """

    def __init__(
        self,
        *,
        api_key: str,
        api_secret: str,
        base_url: str = "https://api.binance.com",
        recv_window_ms: int = 5000,
        timeout_seconds: float = 10.0,
        client: httpx.Client | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if not api_key.strip() or not api_secret.strip():
            raise ValueError("api_key and api_secret are required")
        if not 1 <= int(recv_window_ms) <= 60000:
            raise ValueError("recv_window_ms must be between 1 and 60000")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be > 0")

        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip().encode("utf-8")
        self.base_url = base_url.rstrip("/")
        self.recv_window_ms = int(recv_window_ms)
        self.clock = clock
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=timeout_seconds)

    def close(self) -> None:
        if self._owns_client:
            self.client.close()

    @staticmethod
    def _plain(value: Any) -> str:
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, Decimal):
            return format(value, "f")
        return str(value)

    def _signed_payload(self, params: Mapping[str, Any]) -> str:
        signed = {str(key): self._plain(value) for key, value in params.items() if value is not None}
        signed["recvWindow"] = str(self.recv_window_ms)
        signed["timestamp"] = str(int(self.clock() * 1000))

        # Encode first, then sign the exact encoded bytes that will be sent.
        # Sorting provides deterministic signatures and deterministic tests.
        query = urlencode(sorted(signed.items()), doseq=False, safe="")
        signature = hmac.new(
            self.api_secret,
            query.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return f"{query}&signature={signature}"

    @staticmethod
    def _decode_response(response: httpx.Response) -> Any:
        try:
            return response.json()
        except (json.JSONDecodeError, ValueError):
            return {"raw": response.text}

    def _signed_request(self, method: str, path: str, params: Mapping[str, Any]) -> Any:
        payload = self._signed_payload(params)
        headers = {"X-MBX-APIKEY": self.api_key}
        url = f"{self.base_url}{path}"
        method = method.upper()

        if method in {"GET", "DELETE"}:
            response = self.client.request(method, f"{url}?{payload}", headers=headers)
        else:
            response = self.client.request(
                method,
                url,
                headers={**headers, "Content-Type": "application/x-www-form-urlencoded"},
                content=payload,
            )

        data = self._decode_response(response)
        if response.status_code >= 400:
            code: int | None = None
            message = response.reason_phrase or "request failed"
            if isinstance(data, dict):
                raw_code = data.get("code")
                try:
                    code = int(raw_code) if raw_code is not None else None
                except (TypeError, ValueError):
                    code = None
                message = str(data.get("msg") or message)
            raise BinanceAPIError(
                status_code=response.status_code,
                code=code,
                message=message,
                payload=data,
            )
        return data

    # Read-only USER_DATA methods used by future crash/account reconciliation.
    def account_information(self, *, omit_zero_balances: bool = True) -> dict[str, Any]:
        result = self._signed_request(
            "GET",
            "/api/v3/account",
            {"omitZeroBalances": omit_zero_balances},
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected_account_response")
        return result

    def open_orders(self, *, symbol: str | None = None) -> list[dict[str, Any]]:
        result = self._signed_request(
            "GET",
            "/api/v3/openOrders",
            {"symbol": symbol},
        )
        if not isinstance(result, list):
            raise RuntimeError("unexpected_open_orders_response")
        return [row for row in result if isinstance(row, dict)]

    def open_order_lists(self) -> list[dict[str, Any]]:
        result = self._signed_request(
            "GET",
            "/api/v3/openOrderList",
            {},
        )
        if not isinstance(result, list):
            raise RuntimeError("unexpected_open_order_lists_response")
        return [row for row in result if isinstance(row, dict)]

    def market_buy_quote(
        self,
        *,
        symbol: str,
        quote_order_qty: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        result = self._signed_request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "BUY",
                "type": "MARKET",
                "quoteOrderQty": quote_order_qty,
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected_market_buy_response")
        return result

    def market_sell_quantity(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        client_order_id: str,
    ) -> dict[str, Any]:
        result = self._signed_request(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": "SELL",
                "type": "MARKET",
                "quantity": quantity,
                "newClientOrderId": client_order_id,
                "newOrderRespType": "FULL",
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected_market_sell_response")
        return result

    def query_order(self, *, symbol: str, client_order_id: str) -> dict[str, Any]:
        result = self._signed_request(
            "GET",
            "/api/v3/order",
            {"symbol": symbol, "origClientOrderId": client_order_id},
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected_query_order_response")
        return result

    def place_oco_market_protection(
        self,
        *,
        symbol: str,
        quantity: Decimal,
        take_profit_trigger: Decimal,
        stop_loss_trigger: Decimal,
        list_client_order_id: str,
        above_client_order_id: str,
        below_client_order_id: str,
    ) -> dict[str, Any]:
        """Place SELL OCO using market-triggered TP and SL legs.

        Using TAKE_PROFIT + STOP_LOSS avoids a second limit-price dependency.
        Binance still enforces the OCO price relation at placement time.
        """
        result = self._signed_request(
            "POST",
            "/api/v3/orderList/oco",
            {
                "symbol": symbol,
                "side": "SELL",
                "quantity": quantity,
                "listClientOrderId": list_client_order_id,
                "aboveType": "TAKE_PROFIT",
                "aboveStopPrice": take_profit_trigger,
                "aboveClientOrderId": above_client_order_id,
                "belowType": "STOP_LOSS",
                "belowStopPrice": stop_loss_trigger,
                "belowClientOrderId": below_client_order_id,
                "newOrderRespType": "RESULT",
            },
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected_oco_response")
        return result

    def query_order_list(self, *, list_client_order_id: str) -> dict[str, Any]:
        result = self._signed_request(
            "GET",
            "/api/v3/orderList",
            {"origClientOrderId": list_client_order_id},
        )
        if not isinstance(result, dict):
            raise RuntimeError("unexpected_query_order_list_response")
        return result
