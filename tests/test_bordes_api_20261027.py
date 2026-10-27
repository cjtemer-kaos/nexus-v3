"""Bordes del barrido de la API: binario como texto, consulta RAG vacia y replay.

Tres defectos medidos contra :9000, todos del mismo tipo: una entrada rara
devolvia 200 con una respuesta que "parece valida" y no lo es.

1. `GET /api/fs/read` de un binario: 200 con **71 533 U+FFFD** para
   `python.exe` (241 KB -> 237 393 caracteres) y 63 874 para `session.db`.
   Basura que no se puede mostrar ni razonar, y que en un prompt son decenas de
   miles de tokens para nada.

2. `GET /api/rag/search?q=` (vacio): 200 con un resultado y score 0.62: el
   chunk `# No sources found.` que el propio indexador habia guardado. El
   embedding de una cadena vacia se parece a ese ruido. Y `limit=-1` caia en
   `max(1, min(20, -1))` = 1, o sea menos resultados que el default.

3. `GET /api/sessions/<id>/replay` de un id inexistente: 200 `{"events": []}`,
   que el cliente lee como "sesion vacia" (mismo caso que /messages y /tree,
   que ya eran 404).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.core.event_store import Event, EventStore
from src.core.session_log import Message, SessionLog


@pytest.fixture()
def cliente(tmp_path, monkeypatch):
    from src.api import server

    slog = SessionLog(str(tmp_path / "session.db"))
    es = EventStore(str(tmp_path / "events.db"), wal=False)
    monkeypatch.setitem(server._state, "session_log", slog)
    monkeypatch.setitem(server._state, "event_store", es)
    try:
        yield TestClient(server.app), slog, es
    finally:
        slog.close()
        es.close()


# --------------------------------------------------------------- fs/read


def test_fs_read_de_binario_es_415_y_no_devuelve_mojibake(cliente, tmp_path):
    http, _, _ = cliente
    exe = tmp_path / "falso.exe"
    exe.write_bytes(b"MZ\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff" + b"\x00" * 5000)

    r = http.get("/api/fs/read", params={"path": str(exe)})

    assert r.status_code == 415, r.text[:200]
    detalle = r.json()["detail"]
    assert "binario" in detalle
    assert str(len(exe.read_bytes())) in detalle, "el tamano tiene que aparecer"
    assert "\ufffd" not in r.text


def test_fs_read_de_texto_sigue_funcionando(cliente, tmp_path):
    http, _, _ = cliente
    f = tmp_path / "nota.txt"
    # write_bytes, no write_text: en Windows write_text traduce \n a \r\n y el
    # contenido servido es el de los bytes reales del archivo.
    f.write_bytes("hola\nacentos: ñ á\n".encode("utf-8"))

    r = http.get("/api/fs/read", params={"path": str(f)})

    assert r.status_code == 200
    assert r.json()["content"] == "hola\nacentos: ñ á\n"
    assert r.json()["size"] == f.stat().st_size


def test_fs_read_de_utf16_con_bom_no_lo_llama_binario(cliente, tmp_path):
    """UTF-16 tiene NULs pero es texto: el BOM manda."""
    http, _, _ = cliente
    f = tmp_path / "windows.txt"
    f.write_text("texto en utf-16 con acentos: ñ", encoding="utf-16")

    r = http.get("/api/fs/read", params={"path": str(f)})

    assert r.status_code == 200, r.text[:200]
    assert "ñ" in r.json()["content"]


# --------------------------------------------------------------- rag/search


def test_rag_search_con_q_vacio_es_400(cliente):
    http, _, _ = cliente
    for qs in ("", "   ", "\t\n"):
        r = http.get("/api/rag/search", params={"q": qs})
        assert r.status_code == 400, (qs, r.text[:160])
        assert "q required" in r.json()["detail"]


def test_rag_search_limit_negativo_usa_el_default(cliente, monkeypatch):
    from src.core import rag

    vistos: list[int] = []

    def falso(query, limit=5, min_score=0.25):
        vistos.append(limit)
        return [{"score": 1.0, "source": "s", "content": "c"}] * min(limit, 3)

    monkeypatch.setattr(rag, "search", falso)
    http, _, _ = cliente

    for qs in ({"limit": -1}, {"limit": 0}, {}):
        r = http.get("/api/rag/search", params={"q": "compaction", **qs})
        assert r.status_code == 200, r.text[:160]
        assert r.json()["results"], f"{qs}: un negativo no puede traer menos que el default"

    assert vistos == [5, 5, 5], vistos


def test_rag_search_tope_superior(cliente, monkeypatch):
    from src.core import rag

    vistos: list[int] = []

    def falso(query, limit=5, min_score=0.25):
        vistos.append(limit)
        return []

    monkeypatch.setattr(rag, "search", falso)
    http, _, _ = cliente

    http.get("/api/rag/search", params={"q": "x", "limit": 9999})
    assert vistos == [20]


def test_rag_search_vacio_devuelve_lista_vacia_en_el_core():
    """Defensa en profundidad: la tool y los sub-agentes llaman al core directo."""
    from src.core import rag

    assert rag.search("") == []
    assert rag.search("   ") == []


# --------------------------------------------------------------- replay


def test_replay_de_sesion_inexistente_es_404(cliente):
    http, _, _ = cliente
    r = http.get("/api/sessions/no-existe-xyz/replay")

    assert r.status_code == 404, r.text[:200]
    assert "no-existe-xyz" in r.json()["detail"]


def test_replay_de_sesion_existente_sin_eventos_sigue_siendo_200(cliente):
    http, slog, _ = cliente
    slog.create_session(project="r", session_id="viva")
    slog.append("viva", Message(role="user", content="hola"))

    r = http.get("/api/sessions/viva/replay")

    assert r.status_code == 200
    assert r.json()["events"] == []


def test_replay_devuelve_eventos_de_una_sesion_podada(cliente):
    """Los eventos viven en otro store: si no hay fila en session_log pero si
    eventos, se devuelven (una sesion podada no es una sesion inexistente)."""
    http, _, es = cliente
    es.append(Event(kind="user_message", data={"text": "hola"}, session_id="podada"))

    r = http.get("/api/sessions/podada/replay")

    assert r.status_code == 200, r.text[:200]
    eventos = r.json()["events"]
    assert len(eventos) == 1
    assert eventos[0]["kind"] == "user_message"
    assert eventos[0]["session_id"] == "podada"
