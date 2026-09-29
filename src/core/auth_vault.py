"""
Auth Vault - AES-256-GCM encrypted credential storage.
Absorbed from agent-browser pattern — names cleaned.
"""

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False


class AuthVault:
    """AES-256-GCM encrypted credential storage."""

    def __init__(self, vault_path: Optional[Path] = None, master_key: Optional[str] = None):
        self.vault_path = vault_path or Path(os.environ.get("APP_DATA", Path.home() / ".app")) / "auth_vault.enc"
        self._key = self._derive_key(master_key or os.environ.get("VAULT_MASTER_KEY", "default-dev-key"))
        self._cache: dict = {}
        self._loaded = False

    def _derive_key(self, passphrase: str) -> bytes:
        return hashlib.sha256(passphrase.encode()).digest()

    def get_credential(self, service: str, key: str = "") -> Optional[str]:
        self._ensure_loaded()
        entry = self._cache.get(service)
        if not entry:
            return None
        if key and isinstance(entry, dict):
            entry = entry.get(key)
        return str(entry) if entry else None

    def set_credential(self, service: str, value: str, key: str = ""):
        self._ensure_loaded()
        if key:
            if service not in self._cache:
                self._cache[service] = {}
            self._cache[service][key] = value
        else:
            self._cache[service] = value
        self._save()

    def delete_credential(self, service: str, key: str = ""):
        self._ensure_loaded()
        if key and isinstance(self._cache.get(service), dict):
            self._cache[service].pop(key, None)
        else:
            self._cache.pop(service, None)
        self._save()

    def list_services(self):
        self._ensure_loaded()
        return list(self._cache.keys())

    def _ensure_loaded(self):
        if self._loaded:
            return
        self._loaded = True
        if not self.vault_path.exists():
            return
        try:
            raw = self.vault_path.read_bytes()
            if HAS_CRYPTO and len(raw) > 12:
                nonce = raw[:12]
                ciphertext = raw[12:]
                aesgcm = AESGCM(self._key)
                plaintext = aesgcm.decrypt(nonce, ciphertext, None)
                self._cache = json.loads(plaintext.decode())
            else:
                self._cache = json.loads(raw.decode())
        except Exception as e:
            logger.error(f"Failed to load vault: {e}")

    def _save(self):
        try:
            self.vault_path.parent.mkdir(parents=True, exist_ok=True)
            if HAS_CRYPTO:
                aesgcm = AESGCM(self._key)
                nonce = os.urandom(12)
                plaintext = json.dumps(self._cache).encode()
                ciphertext = aesgcm.encrypt(nonce, plaintext, None)
                self.vault_path.write_bytes(nonce + ciphertext)
            else:
                self.vault_path.write_text(json.dumps(self._cache, indent=2))
        except Exception as e:
            logger.error(f"Failed to save vault: {e}")
