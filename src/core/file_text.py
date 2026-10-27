"""Lectura y escritura de texto detectando la codificacion del archivo.

En Windows hay archivos cp1252 por todos lados (Notepad, Excel, PowerShell 5.1,
programas viejos). Leerlos con `utf-8 + errors="replace"` mete un U+FFFD por
cada acento, y cualquier reescritura posterior deja el archivo destruido.

Verificado con un `.txt` en cp1252 (`resumen: cañón del sur` / `no tocar: ñandú`):
`read_file` devolvia `resumen: ca\ufffd\ufffdn del sur`, el modelo copiaba ese
fragmento como `old` del `edit`, el reemplazo encontraba el texto... y el archivo
quedaba en UTF-8 con la linea NO TOCADA convertida en `no tocar: \ufffdand\ufffd`.
Bytes que el modelo nunca miro, perdidos.

La regla: se lee con el encoding que decodifica y se escribe con ESE MISMO
encoding. Si el texto nuevo no se puede representar (un `→` en un archivo
cp1252), se falla con un mensaje claro en vez de escribir algo distinto.
"""
from __future__ import annotations

from pathlib import Path

# BOM primero: un archivo UTF-16 tiene NULs y ninguna otra heuristica sirve.
# El orden importa (UTF-32 LE empieza como UTF-16 LE).
BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xfe\x00\x00", "utf-32"),
    (b"\x00\x00\xfe\xff", "utf-32"),
    (b"\xff\xfe", "utf-16"),
    (b"\xfe\xff", "utf-16"),
    (b"\xef\xbb\xbf", "utf-8-sig"),
)

# Sin BOM: el primero que decodifique gana. `utf-8-sig` NO va aca aunque acepte
# texto sin BOM: al escribir volveria a ponerle un BOM que el archivo no tenia.
# `latin-1` decodifica cualquier byte, asi que va ultimo como piso.
ENCODINGS = ("utf-8", "cp1252", "latin-1")


def detectar_bom(cabeza: bytes) -> str:
    """Encoding que anuncia el BOM de estos primeros bytes, o '' si no hay."""
    for bom, enc in BOMS:
        if cabeza.startswith(bom):
            return enc
    return ""


def read_text_file(p: str | Path) -> tuple[str, str]:
    """Devuelve (texto, encoding). Con BOM, el texto no incluye el BOM."""
    raw = Path(p).read_bytes()
    enc_bom = detectar_bom(raw[:8])
    if enc_bom:
        return raw.decode(enc_bom, errors="replace"), enc_bom
    for enc in ENCODINGS:
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8"


def encode_text(text: str, enc: str) -> bytes:
    """Bytes del texto en `enc`, o ValueError si algun caracter no entra."""
    try:
        return text.encode(enc)
    except UnicodeEncodeError as e:
        raise ValueError(
            f"el texto nuevo tiene caracteres que {enc} no puede representar "
            f"(el archivo esta en {enc}): no se escribio nada para no romperlo. "
            f"Usa solo caracteres de esa codificacion, o converti el archivo a UTF-8 "
            f"con write_file y despues editalo."
        ) from e


def write_bytes_file(p: str | Path, data: bytes, intentos: int = 4) -> None:
    """Escribe bytes, reintentando un momento si Windows tiene el archivo tomado.

    Un antivirus (Defender) o un indexador pueden tener el archivo abierto un
    instante justo despues de crearlo: `open('wb')` devuelve PermissionError y la
    escritura se perdia. Medido en la suite (fallo intermitente escribiendo en
    %TEMP%). El reintento es corto (50+100+150 ms) para no tapar un permiso
    realmente denegado, que sigue fallando al cuarto intento.
    """
    path = Path(p)
    for i in range(max(1, intentos)):
        try:
            path.write_bytes(data)
            return
        except PermissionError:
            if i == intentos - 1:
                raise
            import time
            time.sleep(0.05 * (i + 1))


def write_text_file(p: str | Path, text: str, enc: str) -> None:
    """Escribe con la codificacion con la que se leyo (y falla si no entra)."""
    write_bytes_file(p, encode_text(text, enc))


def write_preserving_encoding(p: str | Path, text: str) -> tuple[str, bool]:
    """Guarda respetando el encoding que ya tenia el archivo.

    Devuelve (encoding_usado, convertido). Se usa en las escrituras COMPLETAS
    (write_file, PUT /api/fs/write): reescribir un cp1252 en utf-8 no corrompe
    nada, pero deja el archivo ilegible para la herramienta vieja que lo abria.
    Si el texto nuevo no entra en el encoding previo se convierte a utf-8 y se
    avisa (`convertido=True`); un archivo nuevo nace en utf-8.

    Se escribe con `write_bytes`, no con `write_text`: en Windows `write_text`
    abre en modo texto y **traduce cada `\\n` a `\\r\\n`**, asi que el modelo
    pedia LF y el archivo quedaba distinto (diffs de archivo completo, y un
    archivo mixto terminaba todo CRLF).
    """
    path = Path(p)
    previo = ""
    if path.is_file():
        try:
            _, previo = read_text_file(path)
        except OSError:
            previo = ""
    if previo in ("utf-32", "utf-16", "utf-8-sig"):
        write_bytes_file(path, text.encode(previo))
        return previo, False
    if previo == "cp1252":
        try:
            write_bytes_file(path, text.encode("cp1252"))
            return "cp1252", False
        except UnicodeEncodeError:
            pass
    write_bytes_file(path, text.encode("utf-8"))
    return "utf-8", previo == "cp1252"


def parece_binario(cabeza: bytes) -> bool:
    """Heuristica de la cabeza: NUL fuera de un BOM = binario."""
    return bool(cabeza) and b"\x00" in cabeza
