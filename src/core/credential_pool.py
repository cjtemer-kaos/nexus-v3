"""
CredentialPool - Multi-credential failover para SuperNEXUS v2

Soporta multiples API keys por proveedor con failover automatico
cuando una key esta rate-limited o agotada.

Simplificado para SuperNEXUS v2: env vars + .env + cooldown tracking.
"""

import logging
import os
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger("nexus-credentials")

# Cooldowns
COOLDOWN_429 = 300       # 5 min para rate limit
COOLDOWN_401 = 60        # 1 min para auth failure
COOLDOWN_DEFAULT = 600   # 10 min default

# Estrategias
STRATEGY_ROUND_ROBIN = "round_robin"
STRATEGY_RANDOM = "random"
STRATEGY_LEAST_USED = "least_used"


@dataclass
class Credential:
    """Una credencial individual"""
    provider: str  # ollama, openai, anthropic, google
    key: str
    label: str = ""
    priority: int = 0
    is_active: bool = True
    usage_count: int = 0
    last_error: Optional[str] = None
    last_error_time: float = 0.0
    cooldown_until: float = 0.0
    created_at: float = field(default_factory=time.time)

    @property
    def is_available(self) -> bool:
        if not self.is_active:
            return False
        if time.time() < self.cooldown_until:
            return False
        return True


@dataclass
class PoolConfig:
    """Configuracion del pool"""
    strategy: str = STRATEGY_ROUND_ROBIN
    max_retries: int = 3
    auto_discover_env: bool = True
    env_prefixes: Dict[str, List[str]] = field(default_factory=lambda: {
        "openai": ["OPENAI_API_KEY", "OPENAI_API_KEYS"],
        "anthropic": ["ANTHROPIC_API_KEY", "ANTHROPIC_API_KEYS"],
        "google": ["GOOGLE_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEYS"],
        "openrouter": ["OPENROUTER_API_KEY"],
    })


class CredentialPool:
    """
    Pool de credenciales con failover automatico.
    
    Uso:
        pool = CredentialPool()
        pool.add_credential("openai", "sk-...", label="primary")
        pool.add_credential("openai", "sk-...", label="backup")
        
        cred = pool.get_credential("openai")
        if cred:
            # usar cred.key
            pool.record_success(cred)
        else:
            pool.record_failure(cred, "429", "Rate limited")
    """

    def __init__(self, config: Optional[PoolConfig] = None):
        self.config = config or PoolConfig()
        self._credentials: Dict[str, List[Credential]] = {}  # provider -> [credentials]
        self._round_robin_idx: Dict[str, int] = {}
        self._lock = threading.Lock()

        if self.config.auto_discover_env:
            self._discover_from_env()

    def _discover_from_env(self):
        """Descubre API keys desde variables de entorno"""
        for provider, env_vars in self.config.env_prefixes.items():
            for env_var in env_vars:
                value = os.environ.get(env_var, "")
                if not value:
                    continue

                # Soporta multiples keys separadas por coma
                keys = [k.strip() for k in value.split(",") if k.strip()]
                for i, key in enumerate(keys):
                    label = f"{env_var}_{i}" if len(keys) > 1 else env_var
                    self.add_credential(provider, key, label=label, priority=i)

    def add_credential(self, provider: str, key: str, label: str = "", priority: int = 0) -> Credential:
        """Agrega una credencial al pool"""
        cred = Credential(
            provider=provider,
            key=key,
            label=label or f"{provider}_{len(self._credentials.get(provider, []))}",
            priority=priority,
        )
        with self._lock:
            if provider not in self._credentials:
                self._credentials[provider] = []
                self._round_robin_idx[provider] = 0
            self._credentials[provider].append(cred)
        logger.info(f"Added credential: {cred.label} ({provider})")
        return cred

    def remove_credential(self, provider: str, label: str) -> bool:
        """Elimina una credencial del pool"""
        with self._lock:
            creds = self._credentials.get(provider, [])
            for i, cred in enumerate(creds):
                if cred.label == label:
                    creds.pop(i)
                    return True
        return False

    def get_credential(self, provider: str) -> Optional[Credential]:
        """Obtiene la siguiente credencial disponible segun la estrategia"""
        with self._lock:
            creds = self._credentials.get(provider, [])
            if not creds:
                return None

            available = [c for c in creds if c.is_available]
            if not available:
                # Si ninguna esta disponible, intentar la de mayor prioridad
                # aunque este en cooldown (fallback de emergencia)
                available = sorted(creds, key=lambda c: c.priority)
                if available:
                    logger.warning(f"No available credentials for {provider}, using fallback")
                    return available[0]
                return None

            if self.config.strategy == STRATEGY_ROUND_ROBIN:
                idx = self._round_robin_idx.get(provider, 0)
                cred = available[idx % len(available)]
                self._round_robin_idx[provider] = (idx + 1) % len(available)
                return cred

            elif self.config.strategy == STRATEGY_RANDOM:
                return random.choice(available)

            elif self.config.strategy == STRATEGY_LEAST_USED:
                return min(available, key=lambda c: c.usage_count)

            # Default: primera disponible
            return available[0]

    def record_success(self, cred: Credential):
        """Registra uso exitoso"""
        with self._lock:
            cred.usage_count += 1
            cred.last_error = None
            cred.cooldown_until = 0.0

    def record_failure(self, cred: Credential, error_code: str = "", error_msg: str = ""):
        """Registra fallo y aplica cooldown"""
        with self._lock:
            cred.last_error = error_msg or error_code
            cred.last_error_time = time.time()

            if error_code == "429":
                cred.cooldown_until = time.time() + COOLDOWN_429
            elif error_code == "401":
                cred.cooldown_until = time.time() + COOLDOWN_401
            else:
                cred.cooldown_until = time.time() + COOLDOWN_DEFAULT

            logger.warning(f"Credential {cred.label} failed ({error_code}): cooldown until {cred.cooldown_until}")

    def get_stats(self, provider: str = None) -> Dict:
        """Obtiene estadisticas del pool"""
        with self._lock:
            if provider:
                providers = {provider: self._credentials.get(provider, [])}
            else:
                providers = self._credentials

            result = {}
            for prov, creds in providers.items():
                result[prov] = {
                    "total": len(creds),
                    "active": sum(1 for c in creds if c.is_active),
                    "available": sum(1 for c in creds if c.is_available),
                    "total_usage": sum(c.usage_count for c in creds),
                    "credentials": [
                        {
                            "label": c.label,
                            "priority": c.priority,
                            "is_active": c.is_active,
                            "is_available": c.is_available,
                            "usage_count": c.usage_count,
                            "last_error": c.last_error,
                        }
                        for c in creds
                    ],
                }
            return result

    def list_providers(self) -> List[str]:
        """Lista proveedores con credenciales"""
        with self._lock:
            return list(self._credentials.keys())

    def has_credentials(self, provider: str) -> bool:
        """Verifica si hay credenciales para un proveedor"""
        with self._lock:
            return bool(self._credentials.get(provider))

    def mark_error(self, provider: str, error_msg: str = ""):
        """Marca la ultima credencial usada con error (para failover)"""
        with self._lock:
            creds = self._credentials.get(provider, [])
            if creds:
                last_used = max(creds, key=lambda c: c.usage_count)
                self.record_failure(last_used, "error", error_msg)

    def mark_cooldown(self, cred: Credential, error_code: int):
        """Aplica cooldown a una credencial especifica"""
        if cred:
            self.record_failure(cred, str(error_code))
