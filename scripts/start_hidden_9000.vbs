' Lanza nexus-v3 oculto en un puerto fijo (:9000 por defecto).
' Ese es el puerto que consumen OpenCode (provider "nexus") y el plugin brain-context,
' heredado de SuperNEXUS v2. La raiz se deduce del script: es portable.
' Uso: wscript scripts\start_hidden_9000.vbs [puerto]
Option Explicit
Dim sh, fso, root, port
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

port = "9000"
If WScript.Arguments.Count > 0 Then port = WScript.Arguments(0)

' scripts\ -> raiz del proyecto
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root
' run_ft_logged.bat = run_ft.bat + redireccion a data\nexus-server.log (con rotacion).
' Sin esto la salida del server se perdia en la consola oculta.
sh.Run "cmd /c scripts\run_ft_logged.bat --port " & port, 0, False
Set sh = Nothing
