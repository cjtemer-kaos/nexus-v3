' Lanza nexus-v3 (backend :8765) oculto, con Python free-threaded del venv.
' La raiz se deduce del propio script (portable: no depende de la ruta de instalacion).
Option Explicit
Dim sh, fso, root
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' scripts\ -> raiz del proyecto
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root
sh.Run "cmd /c scripts\run_ft.bat --port 8765", 0, False
Set sh = Nothing
