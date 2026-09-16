from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import urllib.error
from typing import Any, Dict, Optional, Tuple

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from app.notifier.vapid import _b64url_decode, _b64url_encode, create_vapid_jwt

logger = logging.getLogger(__name__)

# Whitelist of authentic browser push service domains (RFC 8291 / RFC 8292)
ALLOWED_PUSH_DOMAINS: tuple[str, ...] = (
    ".googleapis.com",
    "googleapis.com",
    ".push.apple.com",
    "push.apple.com",
    ".push.services.mozilla.com",
    "push.services.mozilla.com",
    ".services.mozilla.com",
    "services.mozilla.com",
    ".mozilla.com",
    "mozilla.com",
    ".notify.windows.com",
    "notify.windows.com",
)


def is_valid_push_endpoint(endpoint: str) -> bool:
    """
    Validates that a Web Push subscription endpoint strictly targets an authentic
    public browser push gateway over HTTPS, preventing SSRF attacks against internal LAN services.
    """
    if not endpoint or not isinstance(endpoint, str):
        return False
    try:
        parsed = urllib.parse.urlparse(endpoint.strip())
        if parsed.scheme.lower() != "https":
            return False
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False

        # Disallow raw IP address literals (IPv4 / IPv6) to prevent direct network probes
        try:
            ipaddress.ip_address(host.strip("[]"))
            return False
        except ValueError:
            pass

        # Must match known official browser push gateways
        for allowed in ALLOWED_PUSH_DOMAINS:
            if host == allowed or host.endswith(allowed if allowed.startswith(".") else "." + allowed):
                return True
        return False
    except Exception:
        return False


def encrypt_web_push_payload(
    client_public_key_b64: str,
    client_auth_b64: str,
    plaintext: bytes,
) -> bytes:
    """
    Encrypts plaintext bytes for a Web Push client using RFC 8291 aes128gcm encoding.
    """
    client_raw_pub = _b64url_decode(client_public_key_b64)
    client_auth = _b64url_decode(client_auth_b64)

    if len(client_raw_pub) != 65 or client_raw_pub[0] != 0x04:
        raise ValueError("Invalid client p256dh public key (expected 65-byte uncompressed point)")
    if len(client_auth) < 16:
        raise ValueError("Invalid client auth secret (expected at least 16 bytes)")

    # Load client public key
    client_pub = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), client_raw_pub)

    # 1. Ephemeral server keypair
    server_priv = ec.generate_private_key(ec.SECP256R1())
    server_pub = server_priv.public_key()
    server_raw_pub = server_pub.public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)

    # 2. ECDH shared secret
    ecdh_secret = server_priv.exchange(ec.ECDH(), client_pub)

    # 3. Derive PRK / IKM (RFC 8291 Section 3.2)
    key_info = b"WebPush: info" + bytes([0]) + client_raw_pub + server_raw_pub
    ikm = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=client_auth,
        info=key_info,
    ).derive(ecdh_secret)

    # 4. Derive CEK & Nonce with random salt (RFC 8291 Section 3.3)
    salt = os.urandom(16)
    cek = HKDF(
        algorithm=hashes.SHA256(),
        length=16,
        salt=salt,
        info=b"Content-Encoding: aes128gcm" + bytes([0]),
    ).derive(ikm)

    nonce = HKDF(
        algorithm=hashes.SHA256(),
        length=12,
        salt=salt,
        info=b"Content-Encoding: nonce" + bytes([0]),
    ).derive(ikm)

    # 5. Encrypt record with AES-128-GCM (RFC 8291 Section 4)
    # Delimiter is 0x02 for the last record
    record = plaintext + bytes([2])
    aesgcm = AESGCM(cek)
    ciphertext = aesgcm.encrypt(nonce, record, associated_data=None)

    # 6. Build aes128gcm header (RFC 8291 Section 2.1):
    # salt (16) + record_size (4) + key_id_len (1) + server_raw_pub (65)
    record_size = 4096
    header = salt + record_size.to_bytes(4, "big") + bytes([len(server_raw_pub)]) + server_raw_pub
    return header + ciphertext


def sanitize_webpush_topic(name: str | None, prefix: str = "cam-") -> str:
    """
    Sanitizes and formats a topic string conforming to RFC 8030 Section 5.4:
    - Allowed charset: URL-safe base64 [A-Za-z0-9_-] (RFC 7515)
    - Length: 1 to 32 characters
    """
    raw = (name or "").strip()
    if not raw:
        raw = "default"
    # Replace whitespace and common separators/punctuation with hyphens
    slug = re.sub(r"[\s/\\.:@#&+=~]+", "-", raw)
    # Strip any characters not in the RFC 8030 base64url charset [A-Za-z0-9_-]
    slug = re.sub(r"[^A-Za-z0-9_-]", "", slug)
    # Collapse multiple consecutive hyphens
    slug = re.sub(r"-+", "-", slug).strip("-_")
    if not slug:
        slug = "alert"

    full_topic = f"{prefix}{slug}"
    # Truncate to RFC 8030 32-character maximum and strip trailing hyphen/underscore
    truncated = full_topic[:32].rstrip("-_")
    return truncated if truncated else "alert"


def _send_web_push_sync(
    endpoint: str,
    encrypted_body: bytes,
    vapid_auth_header: str,
    timeout: float = 8.0,
    topic: Optional[str] = None,
    ttl: int = 86400,
) -> int:
    """Performs synchronous HTTP POST request to the push service endpoint."""
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Encoding": "aes128gcm",
        "Authorization": vapid_auth_header,
        "TTL": str(max(0, int(ttl))),
        "Urgency": "high",
    }
    if topic:
        clean_topic = (
            sanitize_webpush_topic(topic, prefix="")
            if not (topic.startswith("cam-") or topic.startswith("sightline-"))
            else topic[:32]
        )
        if clean_topic:
            headers["Topic"] = clean_topic

    req = urllib.request.Request(
        endpoint,
        data=encrypted_body,
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return int(resp.status)
    except urllib.error.HTTPError as err:
        return int(err.code)
    except Exception as exc:
        logger.warning(f"[webpush] network error delivering to {endpoint[:45]}...: {exc}")
        return 0


async def send_web_push(
    subscription: Dict[str, Any],
    payload: Dict[str, Any] | str,
    vapid_privkey: ec.EllipticCurvePrivateKey,
    vapid_pub_b64: str,
    vapid_sub: str,
    timeout: float = 8.0,
    topic: Optional[str] = None,
    ttl: int = 86400,
) -> int:
    """
    Encrypts and sends a Web Push notification to a single client subscription asynchronously.
    Returns HTTP status code (201 Created on success, 404/410 if expired/unregistered).
    """
    endpoint = subscription.get("endpoint", "").strip()
    p256dh = subscription.get("p256dh", "").strip()
    auth = subscription.get("auth", "").strip()

    if not endpoint or not p256dh or not auth:
        logger.warning("[webpush] subscription missing endpoint or keys")
        return 400

    if not is_valid_push_endpoint(endpoint):
        logger.warning(f"[webpush] rejecting untrusted push endpoint: {endpoint[:50]}")
        return 400

    parsed = urllib.parse.urlparse(endpoint)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    raw_json = payload if isinstance(payload, str) else json.dumps(payload)
    encrypted_body = encrypt_web_push_payload(p256dh, auth, raw_json.encode("utf-8"))

    vapid_jwt = create_vapid_jwt(origin, vapid_privkey, vapid_sub)
    vapid_auth_header = f"vapid t={vapid_jwt}, k={vapid_pub_b64}"

    # Offload blocking HTTP call to asyncio executor
    loop = asyncio.get_running_loop()
    status_code = await loop.run_in_executor(
        None,
        _send_web_push_sync,
        endpoint,
        encrypted_body,
        vapid_auth_header,
        timeout,
        topic,
        ttl,
    )
    return status_code
