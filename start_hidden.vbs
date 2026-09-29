Set WshShell = CreateObject("WScript.Shell")
WshShell.Run "python -m src.api.server --port 8765", 0
Set WshShell = Nothing
