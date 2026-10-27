---
description: Revisa los cambios sin commitear y reporta riesgos
argument-hint: "[ruta opcional]"
---
Revisá los cambios pendientes del repo y decime, en este orden:

1. Qué archivos cambiaron y en qué dirección va el cambio (una línea por archivo).
2. Los riesgos concretos: datos que se pueden perder, compatibilidad rota,
   validación faltante, o código que quedó sin usar.
3. Un test que falta para cubrir el cambio más riesgoso.

Alcance: $ARGUMENTS

Usá `git_status` y `git_diff` (no `exec`). No commitees nada.
