' Nexus v3 - lanzador del escritorio.
' Abre la app Electron (UI nativa); la app levanta el backend Python (harness)
' en un puerto libre y sirve la UI ya compilada (ui/dist). Al cerrar la ventana,
' la app apaga su backend.
Option Explicit
Dim sh, fso, root, py, electron, mainJs, env
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' scripts\ -> raiz del proyecto
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root

py = root & "\.venv-ft314\Scripts\python.exe"
electron = root & "\node_modules\electron\dist\electron.exe"
mainJs = root & "\electron\dist\main.js"

If Not fso.FileExists(py) Then
  MsgBox "No se encontro el Python del venv: " & py & vbCrLf & _
         "Crea .venv-ft314 o ajusta el lanzador.", 48, "Nexus v3"
  WScript.Quit 1
End If
If Not fso.FileExists(electron) Then
  MsgBox "No se encontro Electron: " & electron & vbCrLf & _
         "Ejecuta 'npm install' en " & root, 48, "Nexus v3"
  WScript.Quit 1
End If
If Not fso.FileExists(mainJs) Then
  MsgBox "Falta electron\dist\main.js." & vbCrLf & _
         "Ejecuta 'npm run build:electron' en " & root, 48, "Nexus v3"
  WScript.Quit 1
End If

' Env para el proceso hijo (electron + backend heredan)
Set env = sh.Environment("PROCESS")
env("NEXUS_PYTHON") = py
env("ELECTRON_IS_DEV") = "0"

' 0 = ventana oculta para el lanzador; Electron abre su propia ventana.
sh.Run """" & electron & """ """ & mainJs & """", 0, False
