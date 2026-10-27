"""Tool `question`: el agente deja la pregunta y espera la respuesta del usuario.

Paridad con la tool `question` de opencode. Antes, nexus-v3 no tenía forma de
pedir una decisión a mitad de turno: el modelo adivinaba (o escribía la pregunta
como texto y el turno seguía sin respuesta).

Defecto de fondo que arregla `_despertar`: el que espera y el que responde
pueden estar en loops distintos (el POST lo puede atender otro hilo/worker), y
`asyncio.Event.set()` desde afuera del loop NO lo despierta. Por eso el registro
guarda el loop de cada pregunta y usa `call_soon_threadsafe`.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from src.core import questions as Q
from src.core.questions import QuestionRegistry, _limpiar_opciones, get_questions


def _run(coro):
    return asyncio.run(coro)


def test_ask_y_answer_en_paralelo_devuelve_la_respuesta():
    async def run():
        reg = QuestionRegistry()

        async def usuario():
            for _ in range(200):
                pend = reg.pending("s1")
                if pend:
                    r = reg.answer(pend[0]["id"], "b")
                    assert r["ok"] is True
                    return
                await asyncio.sleep(0.005)
            raise AssertionError("la pregunta nunca quedó pendiente")

        hilo = asyncio.create_task(usuario())
        res = await reg.ask("s1", "¿Cuál de las dos?", options=["a", "b"], timeout_s=10)
        await hilo

        assert res["answered"] is True
        assert res["answer"] == ["b"]
        assert res["text"] == "b"

    _run(run())


def test_respuesta_fuera_de_las_opciones_se_rechaza_con_las_validas():
    async def run():
        reg = QuestionRegistry()

        async def usuario():
            for _ in range(200):
                pend = reg.pending()
                if pend:
                    break
                await asyncio.sleep(0.005)
            malo = reg.answer(pend[0]["id"], "zzz")
            assert malo["ok"] is False
            assert "opción inválida" in malo["error"]
            assert "'a'" in malo["error"] and "'b'" in malo["error"]
            reg.answer(pend[0]["id"], "a")

        hilo = asyncio.create_task(usuario())
        res = await reg.ask("", "¿A o B?", options=["a", "b"], timeout_s=10)
        await hilo

        assert res["answer"] == ["a"]

    _run(run())


def test_sin_opciones_acepta_texto_libre():
    async def run():
        reg = QuestionRegistry()

        async def usuario():
            for _ in range(200):
                pend = reg.pending()
                if pend:
                    reg.answer(pend[0]["id"], "usá postgres 16")
                    return
                await asyncio.sleep(0.005)

        hilo = asyncio.create_task(usuario())
        res = await reg.ask("", "¿Qué base?", timeout_s=10)
        await hilo

        assert res["answer"] == ["usá postgres 16"]

    _run(run())


def test_multiple_devuelve_todas_las_elegidas_y_single_recorta():
    async def run():
        reg = QuestionRegistry()

        async def contestar(valor):
            for _ in range(200):
                pend = reg.pending()
                if pend:
                    reg.answer(pend[0]["id"], valor)
                    return
                await asyncio.sleep(0.005)

        hilo = asyncio.create_task(contestar(["a", "b"]))
        multi = await reg.ask("", "¿Cuáles?", options=["a", "b"], multiple=True, timeout_s=10)
        await hilo
        assert multi["answer"] == ["a", "b"]

        hilo = asyncio.create_task(contestar(["a", "b"]))
        single = await reg.ask("", "¿Cuál?", options=["a", "b"], timeout_s=10)
        await hilo
        assert single["answer"] == ["a"]

    _run(run())


def test_cancelar_avisa_que_no_adivine():
    async def run():
        reg = QuestionRegistry()

        async def usuario():
            for _ in range(200):
                pend = reg.pending()
                if pend:
                    assert reg.cancel(pend[0]["id"])["ok"] is True
                    return
                await asyncio.sleep(0.005)

        hilo = asyncio.create_task(usuario())
        res = await reg.ask("", "¿Sigo?", timeout_s=10)
        await hilo

        assert res["answered"] is False and res["cancelled"] is True
        assert "no adivines" in res["reason"]

    _run(run())


def test_timeout_no_cuelga_el_turno_y_dice_que_asumir(monkeypatch):
    monkeypatch.setattr(Q, "TIMEOUT_MIN", 0.05)

    async def run():
        reg = QuestionRegistry()
        res = await reg.ask("", "¿Alguien?", timeout_s=0.05)
        assert res["answered"] is False and res["timed_out"] is True
        assert "asumiste" in res["reason"]

    _run(run())


def test_doble_respuesta_se_rechaza():
    async def run():
        reg = QuestionRegistry()
        creada = {}

        async def usuario():
            for _ in range(200):
                pend = reg.pending()
                if pend:
                    creada["id"] = pend[0]["id"]
                    assert reg.answer(creada["id"], "a")["ok"] is True
                    return
                await asyncio.sleep(0.005)

        hilo = asyncio.create_task(usuario())
        await reg.ask("", "¿A?", options=["a"], timeout_s=10)
        await hilo

        otra = reg.answer(creada["id"], "a")
        assert otra["ok"] is False and "ya estaba respondida" in otra["error"]

    _run(run())


def test_pregunta_desconocida_lista_las_pendientes():
    reg = QuestionRegistry()
    res = reg.answer("q-no-existe", "a")
    assert res["ok"] is False
    assert "pregunta desconocida" in res["error"]

    res2 = reg.cancel("q-no-existe")
    assert res2["ok"] is False and "pregunta desconocida" in res2["error"]


def test_pregunta_vacia_se_rechaza():
    async def run():
        with pytest.raises(ValueError) as e:
            await QuestionRegistry().ask("s", "   ")
        assert "vacía" in str(e.value)

    _run(run())


def test_opciones_aceptan_csv_y_se_deduplican():
    assert _limpiar_opciones("a, b ,a,,c") == ["a", "b", "c"]
    assert _limpiar_opciones(["x", "x", "y"]) == ["x", "y"]
    assert _limpiar_opciones(None) == []
    assert len(_limpiar_opciones([str(i) for i in range(30)])) == Q.MAX_OPCIONES


def test_clear_borra_las_resueltas_y_deja_las_pendientes():
    async def run():
        reg = QuestionRegistry()

        async def usuario():
            for _ in range(200):
                pend = reg.pending()
                if pend:
                    reg.answer(pend[0]["id"], "a")
                    return
                await asyncio.sleep(0.005)

        hilo = asyncio.create_task(usuario())
        await reg.ask("s1", "¿A?", options=["a"], timeout_s=10)
        await hilo
        assert reg.all("s1"), "la resuelta tiene que seguir visible con all()"

        assert reg.clear() == 1
        assert reg.all("s1") == []

    _run(run())


def test_pending_count_solo_cuenta_las_vivas():
    async def run():
        reg = QuestionRegistry()

        async def usuario():
            for _ in range(200):
                if reg.pending_count() == 1:
                    assert reg.pending()[0]["status"] == "pending"
                    reg.answer(reg.pending()[0]["id"], "a")
                    return
                await asyncio.sleep(0.005)

        hilo = asyncio.create_task(usuario())
        await reg.ask("", "¿A?", options=["a"], timeout_s=10)
        await hilo

        assert reg.pending_count() == 0

    _run(run())


def test_la_tool_question_existe_en_el_padre_y_no_en_los_hijos():
    """Un hijo no tiene usuario: esperar 300 s una respuesta imposible es peor
    que no tener la tool."""
    from src.api import server as S

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    sid = f"test-q-{uuid.uuid4().hex[:8]}"

    padre = S._build_registry(scope, sid, cfg, interactive=True)
    hijo = S._build_registry(scope, sid + "::sub", cfg, include_subagents=False,
                             interactive=False)

    assert padre.get("question") is not None
    assert hijo.get("question") is None
    esq = padre.get("question").parameters
    assert esq["required"] == ["question"]
    assert esq["properties"]["options"]["type"] == "array"


def test_la_respuesta_llega_al_turno_que_espera_en_otro_hilo():
    """El POST puede atenderlo otro hilo/loop: `Event.set()` desde afuera no
    despierta al que espera, `call_soon_threadsafe` sí."""
    import threading
    import time

    reg = get_questions()
    salida: dict = {}

    def runner():
        salida["res"] = asyncio.run(reg.ask("probe-hilo", "¿Llega?", options=["si"],
                                            timeout_s=10))

    hilo = threading.Thread(target=runner)
    hilo.start()
    qid = ""
    for _ in range(300):
        pend = [q for q in reg.pending("probe-hilo")]
        if pend:
            qid = pend[0]["id"]
            break
        time.sleep(0.01)
    assert qid, "la pregunta nunca quedó pendiente"
    assert reg.answer(qid, "si")["ok"] is True
    hilo.join(10)
    assert not hilo.is_alive(), "el turno no se despertó desde el otro loop"
    assert salida["res"]["answer"] == ["si"]
