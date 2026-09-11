from __future__ import annotations

from typing import Any

import httpx

BASE_URL = "https://api.binance.com"


class BinancePublicClient:
    def __init__(self, timeout: float = 10.0) -> None:
        self._client = httpx.Client(base_url=BASE_URL, timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def ticker_24h(self) -> list[dict[str, Any]]:
        data = self._get("/api/v3/ticker/24hr")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected 24h ticker response")
        return data

    def book_tickers(self) -> dict[str, dict[str, float]]:
        data = self._get("/api/v3/ticker/bookTicker")
        if not isinstance(data, list):
            raise RuntimeError("Unexpected bookTicker response")
        out: dict[str, dict[str, float]] = {}
        for row in data:
            try:
                out[str(row["symbol"])] = {
                    "bid": float(row["bidPrice"]),
                    "ask": float(row["askPrice"]),
                }
            except (KeyError, TypeError, ValueError):
                continue
        return out

    def klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 120,
        *,
        closed_only: bool = True,
    ) -> list[dict[str, float]]:
        rows = self._get(
            "/api/v3/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
        )
        if not isinstance(rows, list):
            raise RuntimeError("Unexpected klines response")

        # Binance includes the currently-forming candle as the last kline.
        # V2 deliberately ignores it so signals are based only on closed bars
        # and cannot repaint as the live candle changes.
        if closed_only and rows:
            rows = rows[:-1]

        parsed: list[dict[str, float]] = []
        for row in rows:
            parsed.append(
                {
                    "open_time": float(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "close_time": float(row[6]),
                    "quote_volume": float(row[7]),
                    "trades": float(row[8]),
                    "taker_buy_base": float(row[9]),
                    "taker_buy_quote": float(row[10]),
                }
            )
        return parsed
