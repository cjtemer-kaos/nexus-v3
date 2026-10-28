"""Clasificacion de errores de tool: que se reintenta, que se replantea y que aborta.

El caso que motiva el archivo: `grep` sobre una ruta inexistente mataba el turno
completo. Se midio en la corrida de eMule: 68 vueltas de trabajo real terminaron
en `[abortado: grep -> Path not found: ...]` porque "not found" estaba en la
lista de errores fatales. Una ruta mal escrita no es fatal.
"""
from __future__ import annotations

import pytest

from src.core.error_classifier import ErrorAction, classify_error


@pytest.mark.parametrize("err", [
    "Path not found: D:\\x\\backend\\tests",
    "No such file or directory: 'config.yaml'",
    "File not found: /tmp/x",
    "Directory not found: src/nope",
    "no existe la ruta indicada",
])
def test_ruta_inexistente_se_replantea_no_aborta(err):
    assert classify_error("grep", err) == ErrorAction.REPLAN


def test_modelo_inexistente_sigue_siendo_fatal():
    assert classify_error("llm", "model not found: gpt-nope") == ErrorAction.ABORT


def test_permiso_denegado_sigue_siendo_fatal():
    assert classify_error("read_file", "permission denied: C:\\Windows\\x") == ErrorAction.ABORT


def test_timeout_se_reintenta():
    assert classify_error("exec", "timed out waiting for process") == ErrorAction.RETRY
