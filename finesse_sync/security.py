"""Encryption at rest, PAN validation/masking and small helpers."""
from __future__ import annotations

import re

from cryptography.fernet import Fernet, InvalidToken

from .config import ConfigError, get_settings

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def normalize_pan(pan: str | None) -> str:
    return re.sub(r"\s+", "", str(pan or "")).upper()


def is_valid_pan(pan: str | None) -> bool:
    return bool(PAN_RE.match(normalize_pan(pan)))


def mask_pan(pan: str | None) -> str:
    """ABCDE1234F -> ABCDE****F. Anything that is not a valid PAN is fully masked."""
    p = normalize_pan(pan)
    if not p:
        return ""
    if PAN_RE.match(p):
        return p[:5] + "****" + p[-1]
    return p[:2] + "*" * max(len(p) - 2, 1) if len(p) > 2 else "*" * len(p)


def mask_secret(value: str | None) -> str:
    v = str(value or "")
    if len(v) <= 2:
        return "*" * len(v)
    return v[0] + "*" * (len(v) - 2) + v[-1]


def default_password(pan: str | None) -> str:
    """The house rule: contract note password = PAN in uppercase."""
    return normalize_pan(pan)


def generate_key() -> str:
    return Fernet.generate_key().decode()


class Cipher:
    def __init__(self, key: str | None = None):
        key = key if key is not None else get_settings().encryption_key
        if not key:
            raise ConfigError("CNE_ENCRYPTION_KEY is not set. Generate one with: python -m finesse_sync genkey")
        try:
            self._f = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise ConfigError("CNE_ENCRYPTION_KEY is not a valid Fernet key (python -m finesse_sync genkey)") from e

    def encrypt(self, plain: str | None) -> str | None:
        if plain is None or plain == "":
            return None
        return self._f.encrypt(plain.encode()).decode()

    def decrypt(self, token: str | None) -> str:
        if not token:
            return ""
        try:
            return self._f.decrypt(token.encode()).decode()
        except InvalidToken as e:
            raise ConfigError("Stored data could not be decrypted — CNE_ENCRYPTION_KEY changed?") from e
