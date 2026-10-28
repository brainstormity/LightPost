import json
import os
import re
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class Vault:
    """Encrypt account credentials with AES-256-GCM."""

    def __init__(self, key="", directory="data"):
        if not key:
            path = Path(directory) / "credentials.key"
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                key = path.read_text().strip()
            else:
                key = os.urandom(32).hex()
                with os.fdopen(fd, "w") as file:
                    file.write(key)
        if not re.fullmatch(r"[a-fA-F0-9]{64}", key):
            raise ValueError("ENCRYPTION_KEY must be exactly 64 hexadecimal characters.")
        self.cipher = AESGCM(bytes.fromhex(key))

    def encrypt(self, value):
        nonce = os.urandom(12)
        encrypted = self.cipher.encrypt(nonce, json.dumps(value).encode(), None)
        return ":".join((nonce.hex(), encrypted[-16:].hex(), encrypted[:-16].hex()))

    def decrypt(self, value):
        try:
            nonce, tag, data = (bytes.fromhex(part) for part in value.split(":"))
            return json.loads(self.cipher.decrypt(nonce, data + tag, None))
        except Exception:
            raise ValueError(
                "Cannot unlock this account. Restore its original encryption key or reconnect it."
            ) from None


def new_api_key():
    return "lp_" + secrets.token_urlsafe(32)


def load_api_key(directory="data"):
    """Return the saved API key, creating a private one on first use (like credentials.key)."""
    path = Path(directory) / "api.key"
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        key = path.read_text().strip()
        if len(key) >= 24:
            return key
        return save_api_key(directory, new_api_key())  # Empty or damaged file: replace it.
    key = new_api_key()
    with os.fdopen(fd, "w") as file:
        file.write(key)
    return key


def save_api_key(directory, key):
    """Atomically replace the saved key; the old one stops working immediately."""
    path = Path(directory) / "api.key"
    temporary = path.with_name("api.key.new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(key)
    os.replace(temporary, path)
    return key
