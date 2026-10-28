"""Estado del harness: escritura concurrente, lock huérfano, historial y rollback.

El requisito que manda acá es que la automejora no se coma a sí misma: dos
escritores sobre el mismo archivo no pueden perder entradas, un JSON roto no
puede impedir arrancar y un lock de un proceso muerto no puede colgar el turno.
"""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

from src.core import harness_state as hs


def _estado(tmp_path, nombre="state.json", scope="local"):
    return hs.HarnessState(tmp_path / nombre, scope=scope)


def test_escritura_concurrente_no_pierde_entradas(tmp_path):
    uno = _estado(tmp_path)
    dos = _estado(tmp_path)
    uno.cargar()
    dos.cargar()
    errores: list[BaseException] = []

    def trabajar(estado, prefijo):
        try:
            for i in range(40):
                estado.create("memory", f"{prefijo}{i}", f"título {prefijo}{i}",
                              f"contenido {i}", path="concurrencia")
        except BaseException as e:  # pragma: no cover - sólo si algo falla
            errores.append(e)

    hilos = [threading.Thread(target=trabajar, args=(uno, "a")),
             threading.Thread(target=trabajar, args=(dos, "b"))]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert not errores
    final = _estado(tmp_path)
    final.cargar()
    ids = {f["id"] for f in final.entries("memory")}
    assert len(ids) == 80
    assert {f"a{i}" for i in range(40)} <= ids
    assert {f"b{i}" for i in range(40)} <= ids
    assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["schema"] == hs.SCHEMA


def test_escritura_lenta_no_hace_fallar_al_otro_escritor(tmp_path, monkeypatch):
    """Un fsync lento no puede convertirse en "lock ocupado".

    Antes el lock esperaba 50 × 20 ms = 1 s como máximo. Cuando la suite corre
    completa el disco está ocupado y un `fsync` pasa de ese segundo; el segundo
    escritor tiraba `HarnessError` en vez de esperar, y el test de concurrencia
    fallaba de forma intermitente. El presupuesto ahora es por tiempo (20 s).
    """
    real_fsync = os.fsync
    estado_fsync = {"n": 0}

    def fsync_lento(fd):
        estado_fsync["n"] += 1
        if estado_fsync["n"] <= 6:
            time.sleep(0.35)
        return real_fsync(fd)

    monkeypatch.setattr(hs.os, "fsync", fsync_lento)
    uno = _estado(tmp_path)
    dos = _estado(tmp_path)
    uno.cargar()
    dos.cargar()
    errores: list[BaseException] = []

    def trabajar(estado, prefijo):
        try:
            for i in range(3):
                estado.create("memory", f"{prefijo}{i}", f"t{i}", f"c{i}")
        except BaseException as e:  # pragma: no cover - solo si falla
            errores.append(e)

    hilos = [threading.Thread(target=trabajar, args=(uno, "a")),
             threading.Thread(target=trabajar, args=(dos, "b"))]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert not errores
    final = _estado(tmp_path)
    final.cargar()
    assert len({f["id"] for f in final.entries("memory")}) == 6


def test_archivo_corrupto_se_trata_como_vacio_y_se_regenera(tmp_path):
    ruta = tmp_path / "state.json"
    ruta.write_text("{esto no es json", encoding="utf-8")

    estado = _estado(tmp_path)
    assert estado.entries() == []
    estado.create("memory", "nueva", "t", "c")

    dato = json.loads(ruta.read_text(encoding="utf-8"))
    assert list(dato["entries"]["memory"]) == ["nueva"]
    assert dato["schema"] == hs.SCHEMA


def test_lock_huerfano_de_pid_muerto_se_roba(tmp_path):
    ruta = tmp_path / "state.json"
    ruta.write_text(json.dumps({"schema": 1, "entries": {}}), encoding="utf-8")
    lock = tmp_path / "state.json.lock"
    lock.mkdir()
    # pid inexistente y lock viejo: el dueño ya no está.
    (lock / "owner").write_text("999999 token.muerto", encoding="utf-8")
    viejo = time.time() - 60
    os.utime(lock, (viejo, viejo))

    estado = _estado(tmp_path)
    estado.cargar()
    fila = estado.create("memory", "tras-lock", "t", "c")
    assert fila["id"] == "tras-lock"
    assert not lock.exists(), "el lock robado se libera al terminar"


def test_lock_vivo_no_se_roba(tmp_path):
    ruta = tmp_path / "state.json"
    ruta.write_text(json.dumps({"schema": 1, "entries": {}}), encoding="utf-8")
    lock = tmp_path / "state.json.lock"
    lock.mkdir()
    (lock / "owner").write_text(f"{os.getpid()} token.vivo", encoding="utf-8")
    viejo = time.time() - 60
    os.utime(lock, (viejo, viejo))

    estado = _estado(tmp_path)
    estado.cargar()
    original = hs._LOCK_ESPERA_TOTAL
    hs._LOCK_ESPERA_TOTAL = 0.05
    try:
        with pytest.raises(hs.HarnessError, match="lock ocupado"):
            estado.create("memory", "x", "t", "c")
    finally:
        hs._LOCK_ESPERA_TOTAL = original
        (lock / "owner").unlink(missing_ok=True)
        lock.rmdir()


def test_base_system_prompt_rechazado_por_id_y_por_slug(tmp_path):
    estado = _estado(tmp_path)
    with pytest.raises(hs.HarnessError, match="reservado"):
        estado.create("prompt", "base_system_prompt", "t", "c")
    assert hs.slug("Base System Prompt") == "base-system-prompt"
    with pytest.raises(hs.HarnessError, match="reservado"):
        estado.create("prompt", "base-system-prompt", "t", "c")
    with pytest.raises(hs.HarnessError, match="id inválido"):
        estado.create("memory", "Con Mayúsculas", "t", "c")
    assert estado.entries() == []


def test_update_hereda_lo_que_no_trae_y_sube_version(tmp_path):
    estado = _estado(tmp_path)
    creada = estado.create("memory", "prefer_ruff", "Formatear con ruff",
                           "El venv trae ruff.", path="tooling", scope="global",
                           metadata={"origen": "manual"})
    time.sleep(0.01)
    actualizada = estado.update("prefer_ruff", content="Usar ruff antes de commitear.")

    assert actualizada["title"] == "Formatear con ruff"
    assert actualizada["path"] == "tooling"
    assert actualizada["metadata"] == {"origen": "manual"}
    assert actualizada["scope"] == "global", "el scope se hereda del before"
    assert actualizada["version"] == 2
    assert actualizada["created_at"] == creada["created_at"]
    assert actualizada["updated_at"] > creada["updated_at"]


def test_validaciones_por_tipo(tmp_path):
    estado = _estado(tmp_path)
    with pytest.raises(hs.HarnessError, match="kind inválido"):
        estado.create("inventado", "x", "t", "c")
    with pytest.raises(hs.HarnessError, match="title"):
        estado.create("memory", "sin-titulo", "", "c")
    with pytest.raises(hs.HarnessError, match="content"):
        estado.create("memory", "sin-contenido", "t", "")
    with pytest.raises(hs.HarnessError, match="reference y arguments"):
        estado.create("skill", "sk", "t", "c")
    estado.create("skill", "sk", "t", "c", reference={"tool": "x"}, arguments={"a": 1})
    with pytest.raises(hs.HarnessError, match="dag XOR"):
        estado.create("factory", "f1", "t", "c", arguments={"dag": {}, "machine": {}})
    with pytest.raises(hs.HarnessError, match="dag XOR"):
        estado.create("factory", "f2", "t", "c", arguments={})
    estado.create("factory", "f3", "t", "c", arguments={"dag": {"nodes": []}})


def test_id_unico_entre_tipos(tmp_path):
    estado = _estado(tmp_path)
    estado.create("memory", "m1", "t", "c")
    with pytest.raises(hs.HarnessError, match="ya existe"):
        estado.create("prompt", "m1", "t", "c")


def test_historial_y_rollback(tmp_path):
    estado = _estado(tmp_path)
    estado.create("memory", "m1", "v1", "primer contenido")
    estado.update("m1", content="segundo contenido")
    estado.update("m1", title="v3")
    assert estado.get("m1")["version"] == 3
    assert len(estado.history("m1")) == 3

    volvio = estado.rollback("m1")
    assert volvio["content"] == "segundo contenido"
    assert volvio["title"] == "v1", "vuelve al before del último cambio"
    assert volvio["version"] == 4, "el rollback también es un cambio deshacible"

    a_v1 = estado.rollback("m1", version=1)
    assert a_v1["title"] == "v1" and a_v1["content"] == "primer contenido"
    assert a_v1["version"] == 5

    with pytest.raises(hs.HarnessError, match="no hay versión"):
        estado.rollback("m1", version=99)
    with pytest.raises(hs.HarnessError, match="sin historial"):
        estado.rollback("no-existe")


def test_delete_y_search(tmp_path):
    estado = _estado(tmp_path)
    estado.create("memory", "ruff-antes", "Formatear", "pasar ruff antes del commit")
    estado.create("memory", "tests", "Correr tests", "pytest -q tras editar")
    estado.create("prompt", "conciso", "Responder corto", "sin relleno")

    assert [f["id"] for f in estado.search("ruff")] == ["ruff-antes"]
    assert len(estado.search("commit")) == 1
    assert {f["id"] for f in estado.search("")} == {"ruff-antes", "tests", "conciso"}
    assert [f["id"] for f in estado.search("", kind="prompt")] == ["conciso"]

    borrada = estado.delete("tests")
    assert borrada["id"] == "tests"
    assert estado.get("tests") is None
    assert [f["op"] for f in estado.history("tests")] == ["create", "delete"]


def test_digest_respeta_topes_y_marca_local(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path / "data"))
    global_state = hs.open_state("global")
    global_state.create("memory", "compartido", "Hecho global", "vale para todo")
    local = hs.open_state("local")
    for i in range(15):
        local.create("memory", f"m{i:02d}", f"hecho {i}", f"detalle {i}")
    local.create("memory", "compartido", "Pisa el global", "versión local")
    local.create("prompt", "conciso", "Responder corto", "sin relleno")

    texto = hs.digest_block()
    assert "ESTADO DEL AGENTE (global)" in texto
    assert "ESTADO DEL AGENTE (memoria del harness" in texto
    assert "local:compartido" in texto
    assert "conciso" in texto
    assert "harness" in texto

    assert hs.digest_block() == texto, "dos lecturas seguidas dan lo mismo"


def test_digest_vacio_sin_entradas(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path / "vacio"))
    assert hs.digest_block() == ""


def test_store_path_por_alcance(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path / "data"))
    assert hs.store_path("local").name == "state.json"
    assert hs.store_path("global").name == "global.json"
    assert "sesiones" in hs.store_path("session", session_id="s1").parts
    with pytest.raises(hs.HarnessError, match="session_id"):
        hs.store_path("session")
    proyecto = hs.store_path("project", project_root=tmp_path / "repo")
    assert proyecto == tmp_path / "repo" / ".nexus" / "harness.json"
