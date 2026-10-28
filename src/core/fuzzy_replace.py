"""Reemplazo tolerante para `edit` (portado de opencode).

El modelo no falla al editar porque "no sepa editar": falla porque copió el
fragmento con un espacio de más, con tabs en vez de espacios, con comillas
tipográficas, con la indentación corrida o con escapes literales (`\\n`). Un
`str.replace` estricto lo rechaza y el agente entra en un bucle de "leé otra vez
y copiá exacto" que gasta rondas enteras en un problema de un carácter.

opencode resuelve esto con una cadena de estrategias que prueban la coincidencia
de más a menos estricta y solo aplican si el resultado es **único** (nunca
adivinan entre dos candidatos). Acá va lo mismo, sin dependencias:

    1. indentación flexible  (corrimiento uniforme del bloque)
    2. líneas recortadas     (espacios sobrantes al inicio/fin de cada línea)
    3. escapes normalizados  (`\\n`, `\\t`, `\\"` literales del JSON del modelo)
    4. espacios normalizados (cualquier corrida de espacios vale por uno)
    5. bloque anclado        (primera y última línea coinciden, >=3 líneas)

Y cuando ninguna aplica, `bloque_mas_parecido()` devuelve el fragmento REAL con
números de línea, para que el modelo corrija en un solo intento en vez de releer
el archivo entero.
"""
from __future__ import annotations

import difflib

__all__ = [
    "bloque_mas_parecido",
    "ocurrencias_lineas",
    "reemplazo_tolerante",
]

_MAX_LINEA = 120


def _partir(texto: str) -> list[str]:
    return texto.replace("\r\n", "\n").split("\n")


def _prefijo(linea: str) -> str:
    return linea[: len(linea) - len(linea.lstrip())]


def _prefijo_comun(lineas: list[str]) -> str:
    """Prefijo de indentación que comparten las líneas con contenido.

    Es un STRING, no una cantidad: un archivo con tabs tiene que seguir con tabs
    aunque el modelo haya mandado espacios.
    """
    vivas = [ln for ln in lineas if ln.strip()]
    if not vivas:
        return ""
    prefijo = _prefijo(vivas[0])
    for ln in vivas[1:]:
        otro = _prefijo(ln)
        i = 0
        while i < len(prefijo) and i < len(otro) and prefijo[i] == otro[i]:
            i += 1
        prefijo = prefijo[:i]
        if not prefijo:
            return ""
    return prefijo


def _reindentar(nuevas: list[str], destino: str, actual: str) -> list[str]:
    """Mueve el bloque nuevo a la indentación donde está el bloque del archivo."""
    if destino == actual:
        return list(nuevas)
    salida: list[str] = []
    for ln in nuevas:
        if not ln.strip():
            salida.append("")
            continue
        if actual and ln.startswith(actual):
            salida.append(destino + ln[len(actual):])
        else:
            salida.append(destino + ln.lstrip())
    return salida


def _colapsar(linea: str) -> str:
    return " ".join(linea.split())


def _candidatos(claves: list[str], buscadas: list[str]) -> list[int]:
    """Índices donde el bloque `buscadas` aparece contiguo en `claves`."""
    if not buscadas or len(buscadas) > len(claves):
        return []
    n = len(buscadas)
    return [i for i in range(len(claves) - n + 1) if claves[i:i + n] == buscadas]


def _reindentar_por_linea(nuevas: list[str], viejas: list[str],
                          archivo: list[str]) -> list[str]:
    """Ajusta la sangría de cada línea nueva usando su equivalente del archivo.

    Los modelos chicos copian el bloque y le comen la sangría a TODAS las líneas
    (o la dejan corrida). Comparar por línea permite devolver cada una a la
    columna real del archivo sin inventar estructura.
    """
    if not (len(nuevas) == len(viejas) == len(archivo)):
        return _reindentar(nuevas, _prefijo(archivo[0]) if archivo else "",
                           _prefijo(viejas[0]) if viejas else "")
    salida: list[str] = []
    for nueva, vieja, real in zip(nuevas, viejas, archivo):
        if not nueva.strip():
            salida.append("")
            continue
        p_vieja, p_real = _prefijo(vieja), _prefijo(real)
        if p_vieja and nueva.startswith(p_vieja):
            salida.append(p_real + nueva[len(p_vieja):])
        elif p_real and not nueva.startswith(p_real):
            salida.append(p_real + nueva.lstrip())
        else:
            salida.append(nueva)
    return salida


def _aplicar(lineas_texto: list[str], inicio: int, nuevas: list[str],
             viejas: list[str], archivo: list[str]) -> str:
    bloque = _reindentar_por_linea(nuevas, viejas, archivo)
    return "\n".join(lineas_texto[:inicio] + bloque + lineas_texto[inicio + len(archivo):])


def _indentacion_flexible(texto: str, viejo: str, nuevo: str) -> str | None:
    tl, ol, nl = _partir(texto), _partir(viejo), _partir(nuevo)
    base = _prefijo_comun(ol)
    buscadas = [ln[len(base):] if ln.strip() else "" for ln in ol]
    candidatos = []
    for i in range(len(tl) - len(ol) + 1):
        bloque = tl[i:i + len(ol)]
        comun = _prefijo_comun(bloque)
        if [ln[len(comun):] if ln.strip() else "" for ln in bloque] == buscadas:
            candidatos.append(i)
    if len(candidatos) != 1:
        return None
    i = candidatos[0]
    return _aplicar(tl, i, nl, ol, tl[i:i + len(ol)])


def _lineas_recortadas(texto: str, viejo: str, nuevo: str) -> str | None:
    tl, ol, nl = _partir(texto), _partir(viejo), _partir(nuevo)
    candidatos = _candidatos([ln.strip() for ln in tl], [ln.strip() for ln in ol])
    if len(candidatos) != 1:
        return None
    i = candidatos[0]
    return _aplicar(tl, i, nl, ol, tl[i:i + len(ol)])


def _sin_escapes(texto: str) -> str:
    return (texto.replace("\\r\\n", "\n").replace("\\n", "\n")
            .replace("\\t", "\t").replace('\\"', '"').replace("\\'", "'"))


def _escapes_normalizados(texto: str, viejo: str, nuevo: str) -> str | None:
    if "\\n" not in viejo and "\\t" not in viejo and '\\"' not in viejo:
        return None
    v = _sin_escapes(viejo)
    if not v or v == viejo or texto.count(v) != 1:
        return None
    return texto.replace(v, _sin_escapes(nuevo), 1)


def _espacios_normalizados(texto: str, viejo: str, nuevo: str) -> str | None:
    tl, ol, nl = _partir(texto), _partir(viejo), _partir(nuevo)
    candidatos = _candidatos([_colapsar(ln) for ln in tl], [_colapsar(ln) for ln in ol])
    if len(candidatos) != 1:
        return None
    i = candidatos[0]
    return _aplicar(tl, i, nl, ol, tl[i:i + len(ol)])


def _bloque_anclado(texto: str, viejo: str, nuevo: str) -> str | None:
    tl, ol, nl = _partir(texto), _partir(viejo), _partir(nuevo)
    if len(ol) < 3:
        return None
    primera, ultima = ol[0].strip(), ol[-1].strip()
    if not primera or not ultima:
        return None
    candidatos = [i for i in range(len(tl) - len(ol) + 1)
                  if tl[i].strip() == primera and tl[i + len(ol) - 1].strip() == ultima]
    if not candidatos:
        return None

    def _parecido(i: int) -> float:
        body = "\n".join(ln.strip() for ln in tl[i:i + len(ol)])
        patron = "\n".join(ln.strip() for ln in ol)
        return difflib.SequenceMatcher(None, body, patron).ratio()

    def _parecido_medio(i: int) -> float:
        """Solo el interior: dos funciones que empiezan y terminan igual pueden
        tener el medio completamente distinto (ahi no hay que adivinar)."""
        interior_real = [ln.strip() for ln in tl[i + 1:i + len(ol) - 1]]
        interior_buscado = [ln.strip() for ln in ol[1:-1]]
        if not interior_real and not interior_buscado:
            return 1.0
        return difflib.SequenceMatcher(None, "\n".join(interior_real),
                                       "\n".join(interior_buscado)).ratio()

    # El ancla sola no alcanza: si el cuerpo se parece muy poco, se trata como
    # "no está" en vez de sobrescribir un bloque que casualmente empieza y
    # termina igual.
    candidatos = [i for i in candidatos if _parecido(i) >= 0.55 and _parecido_medio(i) >= 0.6]
    if not candidatos:
        return None
    if len(candidatos) > 1:
        # Desempate por parecido del cuerpo: si hay un solo bloque claramente
        # mejor, se usa; si hay empate, se falla (nunca se adivina).
        puntajes = [_parecido(i) for i in candidatos]
        mejor = max(puntajes)
        ganadores = [c for c, p in zip(candidatos, puntajes) if p >= 0.8 and mejor - p < 1e-9]
        if len(ganadores) != 1:
            return None
        candidatos = ganadores
    i = candidatos[0]
    return _aplicar(tl, i, nl, ol, tl[i:i + len(ol)])


_ESTRATEGIAS = (
    ("indentación flexible", _indentacion_flexible),
    ("líneas recortadas", _lineas_recortadas),
    ("escapes normalizados", _escapes_normalizados),
    ("espacios normalizados", _espacios_normalizados),
    ("bloque anclado", _bloque_anclado),
)


def _sin_salto_final(texto: str) -> str:
    """Quita UN salto final: un `old` copiado con `\\n` al final agregaba una
    línea vacía al bloque y hacía fallar la comparación línea a línea."""
    if texto.endswith("\r\n"):
        return texto[:-2]
    if texto.endswith("\n"):
        return texto[:-1]
    return texto


def reemplazo_tolerante(texto: str, viejo: str, nuevo: str) -> tuple[str, str] | None:
    """Aplica `viejo`->`nuevo` con tolerancia acotada.

    Devuelve `(texto_nuevo, estrategia)` o `None` si ninguna estrategia encuentra
    una coincidencia **única**. Nunca aplica un reemplazo ambiguo.
    """
    viejo = _sin_salto_final(viejo)
    nuevo = _sin_salto_final(nuevo)
    if not viejo or not viejo.strip():
        return None
    for nombre, fn in _ESTRATEGIAS:
        try:
            resultado = fn(texto, viejo, nuevo)
        except Exception:
            resultado = None
        if resultado is not None and resultado != texto:
            return resultado, nombre
    return None


def ocurrencias_lineas(texto: str, viejo: str, tope: int = 8) -> list[int]:
    """Números de línea (1-based) donde aparece `viejo` (tolerante a CRLF)."""
    t = texto.replace("\r\n", "\n")
    v = viejo.replace("\r\n", "\n")
    if not v:
        return []
    salida: list[int] = []
    desde = 0
    while len(salida) < tope:
        i = t.find(v, desde)
        if i < 0:
            break
        salida.append(t.count("\n", 0, i) + 1)
        desde = i + max(1, len(v))
    return salida


def _ventana_parecida(claves: list[str], buscadas: list[str]) -> int | None:
    """Índice del bloque de igual largo más parecido (para cuando no hay anclas).

    Se compara en líneas colapsadas, así que "a + c" vs "a + b" cuenta como
    parecido; se usa solo para SUGERIR, nunca para escribir.
    """
    if not claves or not buscadas:
        return None
    n = len(buscadas)
    objetivo = "\n".join(_colapsar(ln) for ln in buscadas)
    if not objetivo:
        return None
    tope = len(claves) - n
    if tope < 0:
        return None
    # Archivos enormes: se muestrea para no gastar segundos en una sugerencia.
    paso = 1 if len(claves) <= 3000 else max(1, len(claves) // 600)
    mejor_i, mejor_r = None, 0.0
    for i in range(0, tope + 1, paso):
        cand = "\n".join(_colapsar(ln) for ln in claves[i:i + n])
        r = difflib.SequenceMatcher(None, cand, objetivo).ratio()
        if r > mejor_r:
            mejor_i, mejor_r = i, r
    if mejor_i is None or mejor_r < 0.4:
        return None
    return mejor_i


def bloque_mas_parecido(texto: str, viejo: str, contexto: int = 2,
                        max_lineas: int = 40) -> str:
    """Fragmento real del archivo más parecido a `viejo`, con números de línea.

    Devuelve "" si no hay nada que sugerir (archivo vacío o sin anclas).
    """
    tl, ol = _partir(texto), _partir(_sin_salto_final(viejo))
    if not tl or not ol or not viejo.strip():
        return ""
    claves = [ln.strip() for ln in tl]
    buscadas = [ln.strip() for ln in ol]
    matcher = difflib.SequenceMatcher(None, claves, buscadas, autojunk=False)
    bloques = [b for b in matcher.get_matching_blocks() if b.size > 0]
    if bloques:
        mejor = max(bloques, key=lambda b: (b.size, -b.a))
        desplazamiento = mejor.a - mejor.b
    else:
        i = _ventana_parecida(tl, ol)
        if i is None:
            return ""
        desplazamiento = i
    inicio = max(0, desplazamiento - contexto)
    fin = min(len(tl), desplazamiento + len(ol) + contexto)
    if inicio >= fin:
        return ""

    filas: list[str] = []
    diferencias: list[int] = []
    for n in range(inicio, fin):
        linea = tl[n]
        k = n - desplazamiento
        esperada = ol[k] if 0 <= k < len(ol) else None
        if esperada is not None and linea.strip() != esperada.strip():
            marca = ">"
            diferencias.append(n + 1)
        else:
            marca = " "
        vista = linea if len(linea) <= _MAX_LINEA else linea[:_MAX_LINEA] + "…"
        filas.append(f"{marca} {n + 1:>5} | {vista}")
        if esperada is not None and marca == ">" and len(filas) < max_lineas:
            queria = esperada if len(esperada) <= _MAX_LINEA else esperada[:_MAX_LINEA] + "…"
            filas.append(f"        |   (buscabas: {queria.strip()})")

    cabecera = f"El bloque más parecido del archivo está en las líneas {inicio + 1}-{fin}:"
    if diferencias:
        cual = ", ".join(str(n) for n in diferencias[:5])
        pie = (f"Las líneas marcadas con '>' no son iguales al texto que pasaste "
               f"(línea/s {cual}): copiá el texto tal como está arriba.")
    else:
        pie = ("Todas las líneas coinciden salvo espacios o el largo del bloque: "
               "copiá el fragmento exactamente como está arriba.")
    return "\n".join([cabecera, *filas[:max_lineas], pie])
