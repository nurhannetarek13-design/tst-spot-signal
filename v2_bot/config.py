from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Settings:
    mode: str = os.getenv("V2_MODE", "shadow").strip().lower()
    live_trading: bool = _bool("V2_LIVE_TRADING", False)
    quote_asset: str = os.getenv("V2_QUOTE_ASSET", "USDT").upper()
    min_quote_volume_24h: float = _float("V2_MIN_QUOTE_VOLUME_24H", 20_000_000.0)
    max_spread_bps: float = _float("V2_MAX_SPREAD_BPS", 15.0)
    min_score: int = _int("V2_MIN_SCORE", 90)
    universe_limit: int = _int("V2_UNIVERSE_LIMIT", 12)
    scan_interval_seconds: int = _int("V2_SCAN_INTERVAL_SECONDS", 60)
    trade_size_usdt: float = _float("V2_TRADE_SIZE_USDT", 10.0)
    max_daily_loss_usdt: float = _float("V2_MAX_DAILY_LOSS_USDT", 2.0)
    max_open_positions: int = _int("V2_MAX_OPEN_POSITIONS", 1)
    take_profit_pct: float = _float("V2_TAKE_PROFIT_PCT", 0.009)
    stop_loss_pct: float = _float("V2_STOP_LOSS_PCT", 0.0062)
    paper_fee_rate: float = _float("V2_PAPER_FEE_RATE", 0.001)
    telegram_bot_token: str = os.getenv("V2_TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id: str = os.getenv("V2_TELEGRAM_CHAT_ID", "").strip()
    state_db: str = os.getenv("V2_STATE_DB", "v2_state.sqlite3")

    def validate(self) -> None:
        if self.mode not in {"shadow", "paper", "live"}:
            raise ValueError("V2_MODE must be shadow, paper, or live")
        if self.mode == "live" and not self.live_trading:
            raise RuntimeError("LIVE mode requested while V2_LIVE_TRADING=false")
        if not self.quote_asset:
            raise ValueError("V2_QUOTE_ASSET must not be empty")
        if self.min_quote_volume_24h <= 0:
            raise ValueError("V2_MIN_QUOTE_VOLUME_24H must be > 0")
        if self.max_spread_bps < 0:
            raise ValueError("V2_MAX_SPREAD_BPS must be >= 0")
        if not 0 <= self.min_score <= 100:
            raise ValueError("V2_MIN_SCORE must be between 0 and 100")
        if self.universe_limit <= 0:
            raise ValueError("V2_UNIVERSE_LIMIT must be > 0")
        if self.scan_interval_seconds <= 0:
            raise ValueError("V2_SCAN_INTERVAL_SECONDS must be > 0")
        if self.trade_size_usdt <= 0:
            raise ValueError("V2_TRADE_SIZE_USDT must be > 0")
        if self.max_daily_loss_usdt <= 0:
            raise ValueError("V2_MAX_DAILY_LOSS_USDT must be > 0")
        if self.max_open_positions <= 0:
            raise ValueError("V2_MAX_OPEN_POSITIONS must be > 0")
        if not 0 < self.take_profit_pct < 0.20:
            raise ValueError("V2_TAKE_PROFIT_PCT must be between 0 and 0.20")
        if not 0 < self.stop_loss_pct < 0.20:
            raise ValueError("V2_STOP_LOSS_PCT must be between 0 and 0.20")
        if not 0 <= self.paper_fee_rate < 0.02:
            raise ValueError("V2_PAPER_FEE_RATE must be between 0 and 0.02")


settings = Settings()
