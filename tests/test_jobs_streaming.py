"""Streaming de la salida de jobs: bordes del cursor y del corte de bytes.

Tres defectos reales del lector de jobs, todos del mismo lugar (`read`):

1. El corte por `max_chars` caia donde cayera: si partia un `á` (2 bytes) la
   salida traia U+FFFD y el tramo siguiente arrancaba en medio del caracter,
   otro U+FFFD. En un log en castellano (build, pytest, npm) eso es corrupcion
   constante en lo que ve el modelo.
2. `max_chars` no tenia techo: `job_read(max_chars=10**12)` hacia que
   `fh.read(n)` intentara reservar ese tamano, saltaba MemoryError, el `except`
   lo tragaba y el agente recibia `output: ""` con `pending_bytes > 0`: un job
   con salida que parecia mudo. Verificado: 3 MB en el log, 0 caracteres
   devueltos.
3. `wait` leia siempre desde 0, asi que un agente que ya habia leido 50 KB
   recibia la cabeza otra vez y su cursor retrocedia a 30000 (invita a re-leer
   en circulo, justo lo que el anti-loop castiga).
"""
from __future__ import annotations

import time

import pytest

from src.core.jobs import (DEFAULT_READ_CHARS, MAX_READ_CHARS, JobRegistry,
                           _decode_tramo)


class _ProcTerminado:
    def poll(self):
        return 0


def _job_con_log(reg: JobRegistry, path, job_id: str = "j1") -> str:
    """Registra un job ya terminado cuyo log es un archivo preparado por el test."""
    reg._jobs[job_id] = {
        "id": job_id, "label": "t", "command": "echo", "cwd": str(path.parent),
        "pid": 1, "log": str(path), "proc": _ProcTerminado(), "started": time.time(),
    }
    return job_id


@pytest.fixture()
def reg(tmp_path):
    return JobRegistry()


def _leer_todo(reg, jid, max_chars):
    """Lee desde el cursor 0 hasta agotar `pending_bytes`."""
    partes, cursor, vueltas = [], 0, 0
    while vueltas < 200:
        vueltas += 1
        out = reg.read(jid, cursor=cursor, max_chars=max_chars)
        partes.append(out["output"])
        assert out["cursor"] >= cursor, "el cursor no puede retroceder"
        assert not (out["cursor"] == cursor and out["pending_bytes"] > 0), \
            "lectura sin avance: el agente quedaria trabado"
        cursor = out["cursor"]
        if out["pending_bytes"] == 0:
            break
    return "".join(partes), cursor


def test_corte_utf8_no_corrompe_acentos(reg, tmp_path):
    log = tmp_path / "acentos.log"
    original = "á" * 4000 + "ñ" * 2000
    log.write_bytes(original.encode("utf-8"))
    jid = _job_con_log(reg, log)

    leido, cursor = _leer_todo(reg, jid, max_chars=997)

    assert "\ufffd" not in leido, "el corte partio un caracter multibyte"
    assert leido == original
    assert cursor == len(original.encode("utf-8"))


def test_caracter_de_4_bytes_no_traba_el_cursor(reg, tmp_path):
    """Un emoji (4 bytes) con max_chars=2: hay que avanzar igual, sin bucle."""
    log = tmp_path / "emoji.log"
    original = "😀" * 50
    log.write_bytes(original.encode("utf-8"))
    jid = _job_con_log(reg, log)

    leido, cursor = _leer_todo(reg, jid, max_chars=2)

    assert cursor > 0
    assert leido.count("😀") == 50


def test_max_chars_gigante_no_revienta_ni_devuelve_vacio(reg, tmp_path):
    log = tmp_path / "grande.log"
    log.write_bytes(b"x" * 300_000)
    jid = _job_con_log(reg, log)

    out = reg.read(jid, cursor=0, max_chars=10**12)

    assert len(out["output"]) == MAX_READ_CHARS
    assert out["cursor"] == MAX_READ_CHARS
    assert out["pending_bytes"] == 300_000 - MAX_READ_CHARS


def test_max_chars_cero_o_negativo_usa_el_default(reg, tmp_path):
    log = tmp_path / "chico.log"
    log.write_bytes(b"y" * 500)
    jid = _job_con_log(reg, log)

    for valor in (0, -10, None, "abc"):
        out = reg.read(jid, cursor=0, max_chars=valor)
        assert len(out["output"]) == 500
        assert out["cursor"] == 500
        assert DEFAULT_READ_CHARS > 500


def test_cursor_mas_alla_del_log_se_resetea_y_avisa(reg, tmp_path):
    """Cursor de un log que ya no esta (o inventado): leer desde 0, no "" eterno."""
    log = tmp_path / "vivo.log"
    log.write_bytes(b"contenido real\n")
    jid = _job_con_log(reg, log)

    out = reg.read(jid, cursor=10**9)

    assert out["reset"] is True
    assert out["output"] == "contenido real\n"
    assert out["cursor"] == len(b"contenido real\n")

    # un cursor valido no marca reset
    assert "reset" not in reg.read(jid, cursor=0)


def test_wait_respeta_el_cursor_y_no_retrocede(reg, tmp_path):
    log = tmp_path / "largo.log"
    log.write_bytes(b"A" * 20_000 + b"FINAL")
    jid = _job_con_log(reg, log)

    primero = reg.read(jid, cursor=0, max_chars=100)
    assert primero["cursor"] == 100

    resto = reg.wait(jid, timeout_s=1, cursor=primero["cursor"])

    assert resto["output"].startswith("A"), "wait no debe repetir el tramo ya leido"
    assert "FINAL" in resto["output"]
    assert resto["cursor"] > primero["cursor"]


def test_wait_no_miente_sobre_el_final_de_un_log_largo(reg, tmp_path):
    """Si la salida no entra en un tramo, `pending_bytes` lo dice y el cursor avanza."""
    log = tmp_path / "enorme.log"
    log.write_bytes(b"A" * 50_000 + b"FINAL")
    jid = _job_con_log(reg, log)

    out = reg.wait(jid, timeout_s=1, cursor=100, max_chars=30_000)

    assert out["cursor"] == 100 + 30_000
    assert out["pending_bytes"] == len(log.read_bytes()) - out["cursor"]
    assert "FINAL" not in out["output"]


def test_wait_sin_cursor_sigue_devolviendo_desde_el_principio(reg, tmp_path):
    """Compatibilidad: el default sigue siendo cursor=0 (como antes)."""
    log = tmp_path / "corto.log"
    log.write_bytes(b"linea uno\nlinea dos\n")
    jid = _job_con_log(reg, log)

    out = reg.wait(jid, timeout_s=1)

    assert out["output"] == "linea uno\nlinea dos\n"


def test_decode_tramo_bordes():
    assert _decode_tramo(b"") == ("", 0)
    assert _decode_tramo(b"abc") == ("abc", 3)
    # un caracter partido en el corte: se devuelven los bytes que si cierran
    texto, consumidos = _decode_tramo("aá".encode("utf-8")[:-1])
    assert texto == "a"
    assert consumidos == 1
    # byte invalido de verdad en el medio: se consume todo con reemplazo
    texto, consumidos = _decode_tramo(b"a\xffb")
    assert consumidos == 3
    assert texto.startswith("a")


def test_job_inexistente_sigue_siendo_error(reg):
    with pytest.raises(KeyError):
        reg.read("no-existe")
    with pytest.raises(KeyError):
        reg.wait("no-existe", timeout_s=1)
