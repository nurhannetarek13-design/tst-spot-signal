from __future__ import annotations

import base64
import os
from functools import lru_cache

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature


@lru_cache(maxsize=1)
def _private_key():
    encoded = (os.getenv("FAST_INGEST_PRIVATE_KEY_B64") or "").strip()
    if not encoded:
        raise RuntimeError("FAST_INGEST_PRIVATE_KEY_B64 missing")
    pem = base64.b64decode(encoded)
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, ec.EllipticCurvePrivateKey):
        raise RuntimeError("FAST_INGEST private key is not EC")
    return key


def sign_fast_ingest(raw: bytes, ts: str) -> str:
    message = ts.encode("utf-8") + b"." + raw
    der = _private_key().sign(message, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der)
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    return base64.urlsafe_b64encode(raw_sig).decode("ascii").rstrip("=")
