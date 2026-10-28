"""Tool calls escritas como texto: XML de invocación además de JSON."""

from __future__ import annotations

from src.core.agent_loop import AgentLoop


def _loop():
    loop = object.__new__(AgentLoop)
    loop.tools = None
    return loop


def test_xml_con_parameter():
    texto = (
        "\n\n<tool_calls>\n<invoke name=\"repl\">\n"
        "<parameter name=\"code\" string=\"true\">print(1 &lt; 2)\n</parameter>\n"
        "</invoke>\n</tool_calls>"
    )
    tcs = _loop()._extract_text_tool_calls(texto)
    assert len(tcs) == 1, tcs
    assert tcs[0].name == "repl"
    assert tcs[0].arguments["code"].startswith("print(1 < 2)")


def test_xml_variante_corta():
    texto = "<invoke name=\"repl\"><code>x = 1</code></invoke>"
    tcs = _loop()._extract_text_tool_calls(texto)
    assert len(tcs) == 1, tcs
    assert tcs[0].arguments["code"] == "x = 1"


def test_xml_varias_invocaciones():
    texto = (
        "<invoke name=\"exec\"><command>dir</command></invoke>\n"
        "<invoke name=\"read_file\"><path>a.txt</path></invoke>"
    )
    tcs = _loop()._extract_text_tool_calls(texto)
    assert [t.name for t in tcs] == ["exec", "read_file"], tcs


def test_xml_sin_invoke_no_inventa():
    assert _loop()._extract_text_tool_calls("<tool_calls>\n(nada)\n</tool_calls>") == []


def test_json_sigue_funcionando():
    tcs = _loop()._extract_text_tool_calls('{"tool": "exec", "arguments": {"command": "dir"}}')
    assert len(tcs) == 1 and tcs[0].name == "exec", tcs
