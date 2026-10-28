"""La seleccion de schemas por turno: el costo fijo mas grande del request.

Medido el 2026-10-28 con el payload real de la sesion 445b758f71fd: 17.029
tokens de prompt con 53 tools, y con el set recortado a 16 bajaba a 11.902
(−5.127 tokens, −30 %). Ese ahorro se paga en CADA iteracion del turno, no una
vez: en la corrida de 2.301 llamadas eran ~11,8 M de tokens.
"""
from __future__ import annotations

import json

from src.core import tool_select
from src.core.agent_loop import AgentLoop


def _tool(nombre: str, desc: str = "", props: dict | None = None) -> dict:
    return {"type": "function", "function": {
        "name": nombre,
        "description": desc or f"herramienta {nombre}",
        "parameters": {"type": "object", "properties": props or {"x": {"type": "string"}}},
    }}


CATALOGO = [
    _tool("read_file", "lee un archivo del disco"),
    _tool("write_file", "escribe un archivo"),
    _tool("edit", "edita un archivo con reemplazo exacto"),
    _tool("exec", "ejecuta un comando de shell"),
    _tool("list_dir", "lista un directorio"),
    _tool("grep", "busca texto en archivos"),
    _tool("glob", "busca archivos por patron"),
    _tool("apply_patch", "aplica un parche unificado"),
    _tool("todo_write", "escribe la lista de tareas"),
    _tool("question", "pregunta al usuario"),
    _tool("tool_search", "busca herramientas por texto"),
    _tool("git_commit", "crea un commit con los cambios"),
    _tool("git_log", "historial de commits"),
    _tool("web_fetch", "descarga una url"),
    _tool("web_search", "busca en internet"),
    _tool("run_tests", "corre pytest"),
    _tool("image_generate", "genera una imagen"),
    _tool("learn", "guarda un aprendizaje en el grafo"),
    _tool("mcp_call", "llama una tool de un servidor mcp"),
    _tool("job_start", "arranca un proceso en segundo plano"),
]


def _nombres(tools) -> list[str]:
    return [t["function"]["name"] for t in tools]


def test_el_nucleo_viaja_siempre():
    elegidas = _nombres(tool_select.select("hola", CATALOGO, max_tools=11))
    for nombre in tool_select.CORE:
        assert nombre in elegidas


def test_el_pedido_trae_las_tools_que_le_sirven():
    elegidas = _nombres(tool_select.select("commitea los cambios del repo", CATALOGO, max_tools=16))
    assert "git_commit" in elegidas
    assert "git_log" in elegidas


def test_el_pedido_en_espanol_encuentra_la_tool_en_ingles():
    # Sin el puente de sinonimos, "descargá esto" no matchea `web_fetch`.
    elegidas = _nombres(tool_select.select("descargá el archivo de esa url", CATALOGO, max_tools=16))
    assert "web_fetch" in elegidas

    elegidas = _nombres(tool_select.select("corré los tests del backend", CATALOGO, max_tools=16))
    assert "run_tests" in elegidas


def test_respeta_el_tope_y_el_orden_original():
    elegidas = tool_select.select("commitea, descargá una url y corré los tests", CATALOGO, max_tools=14)

    assert len(elegidas) <= 14
    # Mismo orden que el catalogo: el prefijo queda estable entre iteraciones.
    assert _nombres(elegidas) == [n for n in _nombres(CATALOGO) if n in set(_nombres(elegidas))]


def test_sin_coincidencias_queda_solo_el_nucleo():
    elegidas = _nombres(tool_select.select("contame un chiste", CATALOGO, max_tools=16))
    assert set(elegidas) <= set(tool_select.CORE)


def test_la_busqueda_recupera_lo_que_quedo_afuera():
    assert "git_commit" in _nombres(tool_select.search(CATALOGO, "commit", 3))
    assert "web_fetch" in _nombres(tool_select.search(CATALOGO, "descargar una url", 3))
    assert "mcp_call" in _nombres(tool_select.search(CATALOGO, "mcp", 3))
    assert tool_select.search(CATALOGO, "", 3) == []


def test_el_resumen_lleva_el_schema_completo():
    resumen = tool_select.resumen(_tool("git_commit", "crea un commit", {"mensaje": {"type": "string"}}))

    assert resumen["name"] == "git_commit"
    assert resumen["parameters"]["properties"]["mensaje"]["type"] == "string"


def test_el_loop_recorta_y_cuenta_lo_que_ahorra():
    loop = AgentLoop(provider_router=None, tool_registry=None, session_log=None,
                     config={"tools": {"selection": {"enabled": True, "max_tools": 12}}})
    salida = loop._seleccionar_tools("commitea los cambios", CATALOGO)

    assert len(salida) <= 12
    assert "git_commit" in _nombres(salida)
    assert loop._count_tools_chars(salida) < loop._count_tools_chars(CATALOGO)


def test_el_loop_no_toca_nada_si_esta_apagado():
    loop = AgentLoop(provider_router=None, tool_registry=None, session_log=None,
                     config={"tools": {"selection": {"enabled": False}}})
    assert loop._seleccionar_tools("commitea los cambios", CATALOGO) == CATALOGO


def test_tool_search_devuelve_json_llamable():
    """El resultado de `tool_search` tiene que alcanzar para armar la llamada."""
    halladas = tool_select.search(CATALOGO, "imagen", 2)
    payload = json.dumps([tool_select.resumen(t) for t in halladas], ensure_ascii=False)

    assert "image_generate" in payload
    assert json.loads(payload)[0]["parameters"]["type"] == "object"
