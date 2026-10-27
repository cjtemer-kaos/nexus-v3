"""El contrato de la API no puede derivar: Python es la fuente, el `.ts` es el artefacto.

Estos tests cubren las cuatro formas en que el contrato se rompe en silencio:

1. alguien edita `server.py` y no regenera `api-contract.generated.ts`
   (la UI compila contra rutas que ya no existen);
2. la UI pide una ruta que el backend nunca declaró (404 en runtime);
3. dos handlers declaran el mismo método+path (el segundo gana y el primero
   queda muerto, sin error);
4. el contrato generado y lo que FastAPI registró de verdad difieren (por
   ejemplo un `@app.get` dentro de una función, que `ast` no ve como ruta).

El caso 4 se comprueba contra el endpoint `/api/contract`, que compara las
rutas extraídas del fuente con `app.routes` en caliente.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.core.api_contract import (
    CONTRACT_PATH,
    SERVER_PATH,
    contract_version,
    extract_routes,
    normalizar_path_de_ui,
    paths_usados_por_la_ui,
    render,
    rutas_no_declaradas,
)

ROOT = Path(__file__).resolve().parents[1]
UI_SRC = ROOT / "ui" / "src"
METODOS = {"GET", "POST", "PUT", "PATCH", "DELETE"}


@pytest.fixture(scope="module")
def rutas():
    return extract_routes(SERVER_PATH.read_text(encoding="utf-8"))


# ------------------------------------------------------------------ frescura


def test_el_artefacto_esta_al_dia():
    """Si `server.py` cambió, este test falla hasta que se regenere el `.ts`."""
    esperado = render()
    actual = CONTRACT_PATH.read_text(encoding="utf-8") if CONTRACT_PATH.is_file() else ""
    assert actual == esperado, (
        "ui/src/lib/api-contract.generated.ts quedó viejo; regenerá con "
        ".venv-ft314\\Scripts\\python.exe scripts\\gen_api_contract.py"
    )


def test_el_cli_de_check_falla_si_el_artefacto_deriva(tmp_path):
    """El gate de CI es el CLI, no el test: se prueba el que se va a usar."""
    ok = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "gen_api_contract.py"), "--check"],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr

    copia = tmp_path / "contrato-viejo.ts"
    copia.write_text("// viejo\n", encoding="utf-8")
    viejo = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "gen_api_contract.py"),
            "--check",
            "--out",
            str(copia),
        ],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    assert viejo.returncode == 1
    assert "desactualizado" in (viejo.stdout + viejo.stderr)
    assert copia.read_text(encoding="utf-8") == "// viejo\n"


# ------------------------------------------------------------------- forma


def test_rutas_bien_formadas_y_sin_duplicados(rutas):
    assert len(rutas) >= 130, f"solo se extrajeron {len(rutas)} rutas"
    vistas = set()
    for r in rutas:
        assert r.method in METODOS, f"{r.handler}: método {r.method}"
        assert r.path.startswith("/"), f"{r.handler}: path {r.path}"
        clave = (r.method, r.path)
        assert clave not in vistas, f"ruta duplicada: {r.method} {r.path} ({r.handler})"
        vistas.add(clave)
        for p in r.path_params:
            assert "{" + p in r.path, f"{r.handler}: path param {p} no está en {r.path}"


def test_body_y_query_salen_del_handler(rutas):
    """Las claves no se inventan: salen de lo que el handler lee de verdad."""
    por_clave = {(r.method, r.path): r for r in rutas}

    sse = por_clave[("POST", "/api/chat/stream")]
    assert {"message", "session_id", "model"} <= set(sse.body)
    assert sse.stream is True

    assert por_clave[("GET", "/api/chat/stream")].stream is True
    assert por_clave[("GET", "/api/health")].stream is False

    salud = por_clave[("GET", "/api/health")]
    assert {"status", "version", "brain"} <= set(salud.response)

    niveles = por_clave[("GET", "/api/permission/levels")]
    assert "session_id" in niveles.query_params
    assert niveles.body_open is False


def test_body_open_solo_en_handlers_que_aceptan_cualquier_clave(rutas):
    abiertos = [r for r in rutas if r.body_open]
    assert abiertos, "se esperaba al menos un handler de patch parcial"
    for r in abiertos:
        assert r.body == [], f"{r.method} {r.path}: bodyOpen con claves fijas {r.body}"


# ------------------------------------------------------------------ la UI


def test_la_ui_solo_pide_rutas_declaradas(rutas):
    archivos = sorted([*UI_SRC.rglob("*.ts"), *UI_SRC.rglob("*.tsx")])
    assert archivos, f"no se encontraron fuentes en {UI_SRC}"
    usadas = paths_usados_por_la_ui(*archivos)
    assert len(usadas) > 50, f"solo {len(usadas)} usos de /api detectados"
    faltan = rutas_no_declaradas({r.path for r in rutas}, usadas)
    assert not faltan, "la UI pide rutas que el backend no declara: " + ", ".join(faltan)


@pytest.mark.parametrize(
    "crudo, esperado",
    [
        ("/api/sessions/${id}/fork", "/api/sessions/{}/fork"),
        ("/api/graph/node/${detail.id}/cite", "/api/graph/node/{}/cite"),
        ("/api/permission/levels${q}", "/api/permission/levels"),
        ("/api/checkpoints?session_id=${s}", "/api/checkpoints"),
        ("/api/sessions/${activeSession}", "/api/sessions/{}"),
    ],
)
def test_normalizacion_de_paths_de_la_ui(crudo, esperado):
    assert normalizar_path_de_ui(crudo) == esperado


# ------------------------------------------------------- runtime vs generado


def test_endpoint_contract_coincide_con_el_generado(rutas):
    from src.api import server

    server._contract_cache.clear()
    with TestClient(server.app) as cliente:
        r = cliente.get("/api/contract")
    assert r.status_code == 200
    d = r.json()

    assert d["version"] == contract_version(rutas)
    assert d["count"] == len(rutas)
    assert d["routes"] == [x.to_dict() for x in rutas]
    assert d["only_in_code"] == [], f"declaradas en el código y no registradas: {d['only_in_code']}"
    assert d["only_in_runtime"] == [], f"registradas y no declaradas: {d['only_in_runtime']}"


def test_la_version_cambia_cuando_cambia_una_ruta():
    fuente = (
        "from fastapi import FastAPI\n"
        "app = FastAPI()\n"
        "@app.get('/api/a')\n"
        "async def a():\n"
        "    return {'x': 1}\n"
    )
    otra = fuente.replace("/api/a", "/api/b")
    assert contract_version(extract_routes(fuente)) != contract_version(extract_routes(otra))
    assert contract_version(extract_routes(fuente)) == contract_version(extract_routes(fuente))
