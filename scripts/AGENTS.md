# AGENTS.md — scripts

Utilidades operativas. Reglas:

- **Arranque silencioso**: `start_hidden_ft.vbs` lanza `run_ft.bat --port 8765` vía
  `pythonw` + `cmd /c` (pythonw solo muere con uvicorn). `nexus-v3-launch.vbs` = shortcut de escritorio.
- **Verificación**: `e2e_diagnostic.py` (harness live; esperado `total=N pass=N fail=0`).
  Usá `--llm` para incluir chequeos que llaman al modelo.
- **Reinicio del backend** tras agregar endpoints:
  ```powershell
  $c=Get-NetTCPConnection -LocalPort 8765 -State Listen -EA SilentlyContinue
  if($c){ Stop-Process -Id $c.OwningProcess -Force; Stop-Process -Id (Get-CimInstance Win32_Process -Filter "ProcessId=$($c.OwningProcess)").ParentProcessId -Force -EA SilentlyContinue }
  Start-Process wscript.exe -ArgumentList '"scripts\start_hidden_ft.vbs"'
  ```
- Scripts `tmp_*.py` son descartables: borralos al terminar. No los dejes commiteados.
- Pitfalls Windows: consola cp1252 no imprime unicode → `sys.stdout.reconfigure(encoding="utf-8")`.
