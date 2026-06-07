import base64
import hashlib
import hmac
import json

from django.conf import settings
from django.utils import timezone


class QRTokenError(Exception):
    pass


def _b64url_encode(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64url_decode(value):
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(f"{value}{padding}".encode("ascii"))


def build_customer_token_id(customer_id):
    return f"USR-{int(customer_id):04d}"


def build_signed_qr_token(order_id, customer_id, expires_at, nonce):
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {
        "order_id": order_id,
        "customer_id": customer_id,
        "exp": int(expires_at.timestamp()),
        "iat": int(timezone.now().timestamp()),
        "nonce": nonce,
    }
    header_part = _b64url_encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    payload_part = _b64url_encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    signature = hmac.new(_get_secret_bytes(), signing_input, hashlib.sha256).digest()
    return f"{header_part}.{payload_part}.{_b64url_encode(signature)}"


def decode_signed_qr_token(token):
    if not token or token.count(".") != 2:
        raise QRTokenError("Malformed QR token.")

    header_part, payload_part, signature_part = token.split(".")
    signing_input = f"{header_part}.{payload_part}".encode("ascii")
    expected_signature = hmac.new(_get_secret_bytes(), signing_input, hashlib.sha256).digest()
    submitted_signature = _b64url_decode(signature_part)
    if not hmac.compare_digest(expected_signature, submitted_signature):
        raise QRTokenError("QR token signature is invalid.")

    try:
        header = json.loads(_b64url_decode(header_part).decode("utf-8"))
        payload = json.loads(_b64url_decode(payload_part).decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        raise QRTokenError("QR token payload could not be decoded.") from exc

    if header.get("alg") != "HS256":
        raise QRTokenError("Unexpected QR token algorithm.")
    if "exp" not in payload:
        raise QRTokenError("QR token is missing an expiry timestamp.")
    return payload


def _get_secret_bytes():
    secret = getattr(settings, "QR_TOKEN_SECRET", "") or settings.SECRET_KEY
    return secret.encode("utf-8")
