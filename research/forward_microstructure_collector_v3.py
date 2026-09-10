#!/usr/bin/env python3
"""Compatibility launcher for microstructure collector v2 using combined streams.

Binance documents both raw and combined WebSocket modes. This launcher keeps
all v2 storage/research logic unchanged while switching the transport to the
combined-stream endpoint and unwrapping {stream,data} messages.
"""
import json
import os

import forward_microstructure_collector_v2 as core

core.WS_URL = os.getenv("MICRO_WS_URL", "wss://fstream.binance.com/stream")
_original_handle_payload = core.handle_payload


def combined_handle_payload(state, payload):
    if isinstance(payload, dict) and "stream" in payload and "data" in payload:
        return _original_handle_payload(state, payload.get("data"))
    if isinstance(payload, dict) and ("result" in payload or "code" in payload):
        print(json.dumps({
            "kind": "microstructure_ws_control",
            "authorization": core.AUTHORIZATION,
            "payload": payload,
        }, separators=(",", ":")), flush=True)
    return _original_handle_payload(state, payload)


core.handle_payload = combined_handle_payload

if __name__ == "__main__":
    core.main()
