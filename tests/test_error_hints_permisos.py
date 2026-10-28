"""Mensajes de error accionables: cmd.exe no es bash y el gate no es el scope.

Dos falsas pistas mandaron al agente a investigar permisos en bucle:

- `mkdir -p a/b/c` (o `cmd /c mkdir ...`) falla con "La sintaxis del comando no
  es correcta." y el modelo concluia "el scope bloquea la escritura".
- `PermissionError: secuencia MEDIUM no aprobada ...` recibia la pista
  "verifica que la ruta este dentro de las raices permitidas", que es de otro
  gate (el scope).
"""
from __future__ import annotations

import pytest

from src.api.server import _cmd_shell_hint
from src.core.agent_loop import AgentLoop


def test_pista_para_mkdir_con_flags_de_unix():
    hint = _cmd_shell_hint("mkdir -p workspace/a/b", "La sintaxis del comando no es correcta.")
    assert "cmd.exe" in hint
    assert "mkdir a\\b\\c" in hint
    assert "write_file" in hint


def test_pista_para_mkdir_con_barra():
    hint = _cmd_shell_hint("cmd /c mkdir workspace/emule/src",
                           "La sintaxis del comando no es correcta.")
    assert "cmd.exe" in hint


def test_pista_en_ingles_tambien():
    hint = _cmd_shell_hint("mkdir -p x", "The syntax of the command is incorrect.")
    assert "cmd.exe" in hint


def test_sin_error_de_sintaxis_no_hay_pista():
    assert _cmd_shell_hint("dir", "ok") == ""
    assert _cmd_shell_hint("python -m pytest -q", "3 passed") == ""


def test_hint_de_aprobacion_no_habla_de_scope():
    err = PermissionError(
        "secuencia MEDIUM no aprobada para write_file: sin canal interactivo: "
        "fail-closed (policy=never) (gate de aprobaciones, no es el scope)")
    texto = AgentLoop._error_hint("write_file", err)
    assert "aprobaciones" in texto.lower()
    assert "raíces permitidas" not in texto


def test_hint_de_scope_sigue_igual_para_scope_real():
    texto = AgentLoop._error_hint("write_file", PermissionError(
        "ruta fuera de allowed_roots: ../secreto.txt"))
    assert "raíces permitidas" in texto
