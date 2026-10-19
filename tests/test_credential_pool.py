"""Tests del pool de credenciales (cooldowns y no-deadlock al marcar errores)."""
import threading

from src.core.credential_pool import CredentialPool, PoolConfig


def _pool():
    return CredentialPool(PoolConfig(auto_discover_env=False))


def test_mark_error_no_hace_deadlock_con_credencial():
    pool = _pool()
    pool.add_credential("openai", "sk-test")

    # Antes: mark_error tomaba el lock y record_failure lo volvia a tomar
    # (threading.Lock no es reentrante) -> el failover quedaba colgado.
    t = threading.Thread(target=pool.mark_error, args=("openai", "sin red"), daemon=True)
    t.start()
    t.join(5)
    assert not t.is_alive(), "mark_error se colgó tomando el lock dos veces"

    cred = pool.get_credential("openai")
    assert cred is not None
    assert cred.last_error == "sin red"
    assert cred.cooldown_until > 0


def test_mark_error_sin_credenciales_es_noop():
    pool = _pool()
    pool.mark_error("gemini", "sin red")
    assert pool.get_credential("gemini") is None


def test_round_robin_rota_entre_credenciales():
    pool = _pool()
    pool.add_credential("openai", "k1")
    pool.add_credential("openai", "k2")
    vistos = {pool.get_credential("openai").key for _ in range(4)}
    assert vistos == {"k1", "k2"}


def test_credencial_en_cooldown_se_evita_pero_hay_fallback():
    pool = _pool()
    pool.add_credential("openai", "k1")
    cred = pool.get_credential("openai")
    pool.record_failure(cred, "429")
    # con una sola credencial, se usa igual como fallback de emergencia
    assert pool.get_credential("openai") is cred
