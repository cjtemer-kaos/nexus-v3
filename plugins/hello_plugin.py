"""Plugin de ejemplo de nexus-v3.

Un plugin es un .py en ./plugins que expone `register(registry)`. Podés registrar tools
(como acá) usando la misma API del harness. Los errores se ignoran sin romper el arranque.
"""


def register(registry) -> None:
    def hello(name: str = "mundo") -> str:
        """Saluda (tool de ejemplo provista por un plugin de terceros)."""
        return f"hola {name} desde un plugin de nexus-v3"

    registry.register_function(
        hello, name="hello",
        description="Saludo de ejemplo (plugin de terceros).",
        category="plugin",
        parameters={"type": "object", "properties": {"name": {"type": "string"}},
                    "additionalProperties": False},
    )
