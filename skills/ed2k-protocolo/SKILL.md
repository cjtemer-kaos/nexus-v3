---
name: ed2k-protocolo
description: Datos verificados del protocolo cliente-cliente eD2k/eMule (encuadre, protocolo por opcode, OP_REQUESTPARTS, OP_SENDINGPART, partes comprimidas, tiempos del slot de subida, baneos y hash ed2k) medidos contra un eMule 0.50a real y su fuente C++. Usala cuando un cliente eD2k/eMule "conecta pero no descarga", cuando el par cierra la conexion justo despues de pedir bloques, cuando la descarga se corta en un tamano raro, cuando el hash no coincide, o cuando haya que probar descargas sin depender de la red publica.
---

# Protocolo eD2k / eMule: lo que el cable exige

Reimplementar eMule sin respetar estos detalles produce clientes que hacen handshake,
piden el nombre del archivo y **no bajan un byte**. Todo lo de aca esta medido contra un
eMule 0.50a real y contrastado con su fuente C++ (`.ref/eMule/`), que es la autoridad.

## 1. Encuadre

```
[1] protocolo (0xE3 eDonkey | 0xC5 eMule | 0xD4 packed)
[4] tamano little-endian = largo(payload) + 1   (incluye el byte de opcode)
[1] opcode
[n] payload
```

El protocolo **no** lo elige el emisor por gusto: eMule despacha segun ese byte
(`ListenSocket.cpp:2212`):

- `0xE3` -> `ProcessPacket` (opcodes eD2k).
- `0xC5` -> `ProcessExtPacket` (opcodes extendidos de eMule).
- otro -> `Disconnect("Unknown protocol")`.

Consecuencia practica: mandar un opcode eD2k con `0xC5` (o al reves) **cierra la conexion
sin respuesta**. El sintoma es "el par corta justo cuando pido".

| Mensaje | Protocolo | Por que |
|---|---|---|
| `OP_HELLO` 0x01 / `OP_HELLOANSWER` 0x4C | `0xE3` | eD2k |
| `OP_REQUESTFILENAME` 0x58 / `OP_REQFILENAMEANSWER` 0x59 | `0xE3` | eD2k |
| `OP_STARTUPLOADREQ` 0x54 / `OP_ACCEPTUPLOADREQ` 0x55 | `0xE3` | eD2k |
| `OP_REQUESTPARTS` 0x47 / `OP_SENDINGPART` 0x46 | **`0xE3`** | `DownloadClient.cpp:1033` crea el paquete sin protocolo -> `OP_EDONKEYPROT` |
| `OP_COMPRESSEDPART` 0x40 | **`0xE3`** | idem, variante comprimida |
| `OP_EMULEINFO` 0x01 / `OP_EMULEINFOANSWER` 0x02 | `0xC5` | extendido |
| `OP_QUEUERANKING` 0x60, `OP_FILESTATUS` 0x50, AICH | `0xC5` | extendido |

`OP_REQUESTPARTS` es el error clasico: parece "de eMule" y va por eD2k.

## 2. Handshake

1. Cliente: `OP_HELLO` (`0xE3`) con hash de usuario de 16 B, IP, puerto, tags.
2. Par: `OP_HELLOANSWER` (`0xE3`).
3. Cliente: `OP_EMULEINFO` (`0xC5`) con las capacidades; el par contesta
   `OP_EMULEINFOANSWER` (`0xC5`).
4. A partir de aca se piden archivos.

Si se anuncia `ET_EXTENDEDREQUEST` (tag `0x25` >= 1), eMule llama a
`ProcessExtendedInfo` (`UploadClient.cpp:256`) y **lee `u16 partes` + `u16 fuentes
completas` del mismo paquete** donde viene el hash. Mandar solo los 16 B del hash hace
que la lectura reviente y eMule cierre sin contestar. Con `OP_REQUESTFILENAME` hay que
agregar `u16 0 + u16 0`.

## 3. Pedir y recibir bloques

```
OP_REQUESTPARTS (0xE3) = <HASH 16><inicio[3] u32><fin[3] u32>
```

- **Primero los tres comienzos y despues los tres finales.** Intercalarlos (inicio, fin,
  inicio, fin...) hace que eMule lea basura.
- **`fin` es EXCLUSIVO**: eMule manda `EndOffset + 1` (`DownloadClient.cpp:1055`).
  Pedir `inicio..fin-1` deja el ultimo byte sin pedir.
- Un bloque de tamano `fin - inicio`. eMule descarta en silencio bloques mayores a
  `EMBLOCKSIZE * 3` (`UploadClient.cpp:417`) y bloques fuera del archivo.
- `EMBLOCKSIZE = 184320` (`opcodes.h:133`).

```
OP_SENDINGPART (0xE3) = <HASH 16><inicio u32><fin u32><datos>
```

- `fin` tambien es exclusivo; el payload mide exactamente `fin - inicio`
  (`UploadDiskIOThread.cpp:548-565`). Un cliente que hace `datos[inicio:fin+1]` se corre
  un byte por paquete y el hash final no coincide nunca.
- eMule parte un bloque pedido en paquetes de ~10 KB; no hace falta pedirlos de a uno.

```
OP_COMPRESSEDPART (0xE3) = <HASH 16><inicio u32><tam_sin_comprimir u32><zlib...>
```

- eMule comprime cuando el cliente anuncio `ET_COMPRESSION` >= 1. Si se anuncia
  compresion hay que descomprimir; si no se quiere implementar, anunciar `0`.
- `fin` no viene en el paquete: es `inicio + tam_sin_comprimir` (`DownloadClient.cpp:1149`).

## 4. Tiempos y estados que parecen fallas y no lo son

- Despues de `OP_ACCEPTUPLOADREQ`, eMule **tarda ~45-60 s** en mandar el primer bloque
  (arranque del slot de subida). Un timeout de 10-30 s por bloque aborta descargas que
  estaban por empezar. Medido: primer `OP_SENDINGPART` a los 51-61 s.
- eMule **cierra la conexion al terminar los bloques pedidos**. No es un error: hay que
  pedir todo el archivo de una vez (hasta 3 bloques de `EMBLOCKSIZE` = 552960 B) o
  reconectar y volver a esperar el slot.
- `OP_OUTOFPARTREQS` (0x57) = "no tengo mas bloques para vos", distinto de un corte.

## 5. Baneos (el error que envenena las pruebas)

`CUpDownClient::CheckFailedFileIdReqs` (`BaseClient.cpp:2802`) cuenta pedidos de hash
desconocido; al sexto, `Ban("FileReq flood")` y `CLIENTBANTIME = 2 horas`
(`opcodes.h:121`). La lista vive en memoria (`CClientList::m_bannedList`), asi que:

- **Reiniciar eMule limpia el baneo** (no hay archivo que borrar).
- Un solo pedido con hash equivocado durante el desarrollo deja la IP baneada y despues
  *todo* parece roto, incluso `OP_REQUESTFILENAME`, con cierre inmediato. Si el sintoma
  empeora con cada corrida, es baneo, no protocolo.

## 6. Hash ed2k

- Partes de `PARTSIZE = 9728000` B; cada parte se hashea con MD4.
- Archivo de **una sola parte** (`size < PARTSIZE`): el hash ed2k es **`md4(archivo)`**,
  no `md4(md4(parte))` (`KnownFile.cpp:527`: `SetMD4Hash(lasthash)`). Doble hash da un
  valor que ningun par reconoce.
- Con 2+ partes: `md4(concatenacion de los md4 de cada parte)`.

## 7. Tags en el cable

`u8 tipo` + `u16 largo_del_nombre` + (`1 byte name_id` | nombre en texto) + valor.

- `TAGTYPE_HASH=0x01`, `STRING=0x02`, `UINT32=0x03`, `FLOAT32=0x04`, `BOOL=0x05`,
  `BLOB=0x07`, `UINT16=0x08`, `UINT8=0x09`, `UINT64=0x0B` (`opcodes.h:420-428`).
- Con name_id el `u16` va **siempre en 1**. Poner ahi el id hace que eMule cierre.
- `ET_COMPRESSION=0x20`, `ET_UDPPORT=0x21`, `ET_UDPVER=0x22`,
  `ET_SOURCEEXCHANGE=0x23`, `ET_COMMENTS=0x24`, `ET_EXTENDEDREQUEST=0x25`,
  `ET_FEATURES=0x27`.

## 8. Probar sin depender de la red publica

La red publica no es un juez: cambia, tiene pocas fuentes y los pares reales banean. Para
verificar de verdad se usa un **eMule aislado como oraculo** sirviendo un archivo conocido
en `127.0.0.1`:

1. Copiar `emule.exe` a una carpeta propia con su `config\` (perfil aislado).
2. En Windows Vista+ eMule usa `%LOCALAPPDATA%\eMule` salvo
   `HKCU\Software\eMule\UsePublicUserDirectories = 2` (directorio del ejecutable). Guardar
   el valor previo y restaurarlo al terminar: **nunca** tocar el perfil real del usuario.
3. `preferences.ini` propio: `Port`, `UDPPort`, `IncomingDir`, `TempDir`, `SeeShare=0`,
   `Language=1033` (con el locale en español el loader busca `es_ES.dll`, el paquete se
   llama `es_ES_T.dll`, y eMule abre un modal que **bloquea el arranque** sin abrir el
   puerto), `StartupMinimized=1`.
4. Compartir el fixture y confirmar con `OP_ASKSHAREDFILES` (0x4A/0x4B) que el hash y el
   tamano son los esperados.
5. Descargar desde el cliente nuevo con un link que traiga la fuente:
   `ed2k://|file|nombre|tamano|HASH|/|sources,127.0.0.1:PORT|/`. Un cliente que ignore
   `|sources,` no se puede probar de forma determinista (y eMule real los respeta).
6. Criterio de aceptacion: el archivo en disco tiene **exactamente** el tamano pedido y su
   hash ed2k es el del link. Nada de "bajaron bytes".

## 9. Sintomas y causa

| Sintoma | Causa mas probable |
|---|---|
| Cierra al pedir bloques, 0 B | `OP_REQUESTPARTS` en `0xC5` en vez de `0xE3`, o `inicio/fin` intercalados |
| Cierra al pedir el nombre | `ET_EXTENDEDREQUEST` anunciado y falta `u16 partes + u16 fuentes` |
| Baja y el hash no coincide | `fin` tratado como inclusivo en `OP_SENDINGPART` |
| Se corta siempre en el mismo tamano raro | bloques pedidos > `EMBLOCKSIZE*3`, o buffer de recepcion que pierde bytes entre ventanas |
| Cierra a los 45-60 s sin datos | se aborto por timeout antes de que arranque el slot |
| Todo cierra de golpe y no vuelve | IP baneada 2 h por pedidos de hash desconocido: reiniciar eMule |
| Descarga 1 bloque y se corta | eMule cierra al terminar lo pedido: pedir el archivo entero (<= 3 bloques) |
