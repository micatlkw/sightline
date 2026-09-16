from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from pathlib import Path
from typing import Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)

logger = logging.getLogger(__name__)


def _b64url_encode(data: bytes) -> str:
    """Encodes bytes to base64url string without trailing '=' padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(s: str) -> bytes:
    """Decodes base64url string with or without padding."""
    s = s.strip()
    padding = len(s) % 4
    if padding:
        s += "=" * (4 - padding)
    return base64.urlsafe_b64decode(s.encode("ascii"))


def get_or_create_vapid_keys(storage_path: Path) -> Tuple[ec.EllipticCurvePrivateKey, str]:
    """
    Loads existing VAPID P-256 EC keypair from storage_path, or generates a new
    keypair and persists it. Returns (private_key, public_key_b64url).
    """
    try:
        if storage_path.is_file():
            try:
                storage_path.chmod(0o600)
            except OSError:
                pass
            data = json.loads(storage_path.read_text(encoding="utf-8"))
            priv_pem = data.get("private_key_pem", "").encode("utf-8")
            pub_b64 = data.get("public_key_b64", "")
            if priv_pem and pub_b64:
                priv_key = load_pem_private_key(priv_pem, password=None)
                if isinstance(priv_key, ec.EllipticCurvePrivateKey):
                    return priv_key, pub_b64
    except Exception as exc:
        logger.warning(f"[vapid] could not load existing keys from {storage_path}: {exc}")

    # Generate new NIST P-256 (secp256r1) keypair
    logger.info(f"[vapid] generating new VAPID P-256 keypair at {storage_path}...")
    priv_key = ec.generate_private_key(ec.SECP256R1())
    pub_key = priv_key.public_key()

    raw_pub = pub_key.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    pub_b64 = _b64url_encode(raw_pub)

    priv_pem = priv_key.private_bytes(
        encoding=Encoding.PEM,
        format=PrivateFormat.PKCS8,
        encryption_algorithm=NoEncryption(),
    ).decode("utf-8")

    try:
        storage_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "public_key_b64": pub_b64,
            "private_key_pem": priv_pem,
            "created_at": int(time.time()),
        }
        temp_file = storage_path.with_suffix(".tmp")
        temp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        try:
            temp_file.chmod(0o600)
        except OSError:
            pass
        temp_file.replace(storage_path)
        try:
            storage_path.chmod(0o600)
        except OSError:
            pass
        logger.info(f"[vapid] VAPID keypair successfully saved to {storage_path}")
    except Exception as exc:
        logger.error(f"[vapid] failed to save keypair to {storage_path}: {exc}")

    return priv_key, pub_b64


def create_vapid_jwt(
    endpoint_origin: str,
    private_key: ec.EllipticCurvePrivateKey,
    subscriber_claim: str,
    expiration_seconds: int = 12 * 3600,
) -> str:
    """
    Creates an RFC 8292 compliant VAPID JWT signed with ES256 using raw R || S (64-byte) signature.
    """
    header = _b64url_encode(json.dumps({"typ": "JWT", "alg": "ES256"}).encode("utf-8"))
    payload = _b64url_encode(
        json.dumps({
            "aud": endpoint_origin,
            "exp": int(time.time()) + expiration_seconds,
            "sub": subscriber_claim,
        }).encode("utf-8")
    )
    signing_input = f"{header}.{payload}".encode("ascii")

    # Sign using ECDSA SHA256
    der_sig = private_key.sign(signing_input, ec.ECDSA(hashes.SHA256()))
    r, s = decode_dss_signature(der_sig)

    # IEEE P1363 format: 32 bytes R + 32 bytes S
    raw_sig = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    sig_b64 = _b64url_encode(raw_sig)

    return f"{header}.{payload}.{sig_b64}"


def sign_thumbnail_token(event_id: int, secret: str, ttl_seconds: int = 900) -> Tuple[str, int]:
    """Generates an HMAC signature and expiration timestamp for unauthenticated thumbnail access (15m default)."""
    exp = int(time.time()) + ttl_seconds
    msg = f"{event_id}:{exp}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return sig, exp


def verify_thumbnail_token(event_id: int, sig: str, exp: int, secret: str) -> bool:
    """Verifies that an HMAC thumbnail token is valid and has not expired."""
    if not sig or exp < time.time():
        return False
    msg = f"{event_id}:{exp}".encode("utf-8")
    expected_sig = hmac.new(secret.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_sig, sig.strip().lower())
