---
name: rea-protocolo
description: Extrae un protocolo de red binario (P2P, juego, cliente-servidor) desde un ejecutable usando REA/Ghidra y lo valida contra la red real antes de reimplementarlo. Usala cuando haya que reescribir un cliente que habla con servidores vivos (eD2k/Kad, BitTorrent, Gnutella, protocolos de juegos), cuando el cliente nuevo "conecta pero no descarga", cuando una respuesta no llega, o cuando pidan superar en velocidad a un programa viejo y haya que copiarle el mecanismo. Tambien cuando digan "analiza el binario y hacele una version mejor".
---

# Extraer un protocolo de red de un binario y reimplementarlo mejor

Reimplementar un cliente sin leer su protocolo del binario es adivinar. Adivinar produce
clientes que conectan, buscan y **no descargan**. Esta es la secuencia que evita eso, y la
regla que decide casi todo: **la red es el juez, no el código**.

## 0. Antes de escribir una línea

- Un protocolo tiene cuatro cosas y hay que sacar las cuatro: **encuadre** (framing), **opcodes**,
  **formato de cada mensaje** y **máquina de estados** (quién habla primero, qué se espera a qué).
- Todo dato sacado del binario se cita con su origen (string, dirección, función). Un opcode sin
  cita es una suposición, y las suposiciones se prueban contra la red, no en el cliente nuevo.

## 1. Medir el binario

- `mcp_call(server="rea", tool="open_binary", arguments={"path": "C:\\ruta\\programa.exe"})`
- `binary_overview` → segmentos, cantidad de procedimientos y de strings. Si tarda minutos, subí
  `timeout` en `mcp_call` (hasta 1800 s); no lo abandones.
- Anotá sha256 y arquitectura: el análisis se cita con el hash del binario.

## 2. Sacar los opcodes (no las descripciones)

Buscá con `search_strings` lo que **el binario imprime cuando algo del protocolo sale mal**:

- `"Unknown packet"`, `"opcode=0x%02X"`, `"invalid protocol"`, `"too short"`, `"corrupt"`.
- Esquemas de URL y nombres de archivo propios (`ed2k://`, `server.met`, `nodes.dat`): dicen qué
  piezas existen y con qué formato.
- Nombres de comandos que el compilador dejó en el binario (`OP_`, `EC_`, `CMD_`).

Esa cadena de error es la mejor puerta de entrada: `xrefs` sobre su dirección lleva al
**switch de opcodes**, y ahí está la tabla completa. `procedure_assembly` / decompilación del
switch dan: valor del opcode, tamaño esperado del payload y qué función lo procesa.

## 3. Formato de encuadre y de cada mensaje

Confirmá en el binario, no por fama: byte de protocolo (0xE3, 0xC5, 0xD4, 0xE4…), tamaño
(little endian, ¿incluye el opcode?), orden de los campos y si el mismo comando existe en una
**variante ofuscada/compressionada con otro opcode**. Ignorar la variante es el error clásico:
se pide bien y se descarta la respuesta porque llega con otro número.

## 4. Validar contra la red con un cliente mínimo ANTES del cliente nuevo

Un script de 60 líneas, sin dependencias del proyecto: conectar, un mensaje, imprimir los bytes
crudos de la respuesta. Sirve para separar "el servidor no contesta" de "mi cliente está mal".
Checklist cuando algo no llega:

- ¿El login fue aceptado? (esperá la respuesta de ID/estado antes de pedir nada).
- ¿El opcode es el del binario o el que suena parecido? (0x19 ≠ 0xA1).
- ¿El tamaño del payload es el que espera? (hash + tamaño del archivo, no solo el hash).
- ¿La respuesta viaja por la **misma** conexión o por otra (UDP, otra sesión, callback)?
- ¿Hay que esperar antes de pedir? Los servidores descartan pedidos de clientes recién conectados.
- ¿El pedido se repite? Muchos servidores solo contestan al segundo intento o tras N segundos.

Si el par remoto **acepta el TCP y cierra al instante**, el handshake está mal: protocolo, orden
de campos o el mensaje de hello incompleto. Un cierre inmediato no es "no hay fuentes".

## 5. Recién ahora el cliente nuevo

- Copiá la máquina de estados del binario (quién inicia, qué se reintenta y cuándo).
- Escribí el decodificador con **los bytes crudos logueables** (`hex`) detrás de un flag: sin eso,
  el próximo "no llega nada" es indistinguible de "llega otra cosa".
- Cada comando nuevo: primero el cliente mínimo del paso 4, después el código de producción.

## 6. Superar al original (esto es lo que se pide)

Copiar el mecanismo es el piso. Una vez que baja, para ir más rápido que el programa viejo:

- **Más fuentes en paralelo**: pedir fuentes a varios servidores y por Kad a la vez; mantener el
  conjunto vivo (re-preguntar cada N minutos, descartar muertas, no bloquear en una sola).
- **Descarga multi-par**: N conexiones simultáneas con rangos disjuntos del archivo, no una cola
  secuencial. Cada par aporta su ancho de banda.
- **No esperar de a uno**: el handshake y el pedido de partes de todos los pares en paralelo.
- **Reintentar sin pausar para siempre**: una conexión que se cae vuelve a la cola; "pausado por
  error" mata la descarga y es la diferencia entre 0 B y terminar.
- **Medir**: anotar B/s por par y total. Sin número no hay mejora; con número se ve qué palanca
  funciona.

## 7. Antipatrones que ya costaron rondas

- Buscar strings y concluir ("encontré ed2k://, ya está"): los strings son el índice, no el protocolo.
- Asumir que la respuesta llega por el mismo socket sin verificarlo en el binario.
- Declarar "sin fuentes" cuando el problema es que el pedido está mal formado.
- Pausar el trabajo para siempre ante el primer `ConnectionError`.
- Probar el cliente nuevo antes de tener un cliente mínimo que pruebe el protocolo.

## 8. Cierre honesto

Una pieza que compila no es una pieza que funciona. El cierre es: un comando real contra la red
real, con bytes que suben y un resultado verificable por un tercero (hash, tamaño, tiempo). Si eso
no pasó, lo que hay que reportar es el estado y el próximo experimento, no "listo".
