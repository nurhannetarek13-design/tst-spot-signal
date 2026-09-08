from __future__ import annotations

"""Cloudflare-only compatibility shim.

Production confirmation ownership lives in Cloudflare:
Telegram callback -> Cloudflare -> signed Railway relay -> Make -> Binance.

This module intentionally contains no executor-link signing, no /execute URLs,
and no Telegram getUpdates polling. It only preserves the small interface that
legacy imports expect while failing closed for obsolete execution calls.
"""

from pathlib import Path

CHAT_ID_FILE = Path('/tmp/cloudflare_telegram_owner')


def get_chat_id() -> str:
    # Sentinel used only so the legacy scanner's readiness check does not try to
    # discover a chat via Telegram. Cloudflare already owns and validates it.
    return 'cloudflare-owned'


def tg_api(method: str, payload=None):
    # Local compatibility responses only. Never call Telegram from Railway.
    if method == 'getMe':
        return {'ok': True, 'result': {'id': 0, 'username': 'cloudflare-owned'}}
    if method == 'getUpdates':
        return {'ok': True, 'result': []}
    if method == 'sendChatAction':
        return {'ok': True, 'result': True}
    raise RuntimeError(f'RAILWAY_TELEGRAM_DISABLED:{method}')


def get_free_usdt_balance():
    return None


def send_prealert(**_kwargs) -> None:
    return None


def send_opportunity(*_args, **_kwargs):
    raise RuntimeError(
        'LEGACY_ONE_TAP_DISABLED: use FAST_INGEST_URL -> Cloudflare Telegram confirmation flow'
    )
