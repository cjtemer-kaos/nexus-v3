' Nexus v3 - lanzador del escritorio.
' Abre la app Electron (UI nativa); la app levanta el backend Python (harness)
' en un puerto libre y sirve la UI ya compilada (ui/dist). Al cerrar la ventana,
' la app apaga su backend.
' Si falta algo, compila lo que falte o indica ejecutar INSTALAR.bat.
Option Explicit
Dim sh, fso, root, py, electron, mainJs, uiIndex, env
Set sh = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")

' scripts\ -> raiz del proyecto
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
sh.CurrentDirectory = root

py = root & "\.venv-ft314\Scripts\python.exe"
electron = root & "\node_modules\electron\dist\electron.exe"
mainJs = root & "\electron\dist\main.js"
uiIndex = root & "\ui\dist\index.html"

If Not fso.FileExists(py) Then
  MsgBox "Falta el entorno de Python:" & vbCrLf & py & vbCrLf & vbCrLf & _
         "Ejecuta INSTALAR.bat en " & root & " y volve a intentar.", 48, "Nexus v3"
  WScript.Quit 1
End If
If Not fso.FileExists(electron) Then
  MsgBox "Falta Electron (dependencias de Node):" & vbCrLf & electron & vbCrLf & vbCrLf & _
         "Ejecuta INSTALAR.bat en " & root & " y volve a intentar.", 48, "Nexus v3"
  WScript.Quit 1
End If

' Compilar lo que falte (main de Electron y UI). Antes solo compilaba electron,
' por eso una instalacion nueva abria la ventana en blanco.
If Not fso.FileExists(mainJs) Then
  sh.Run "cmd /c npm run build:electron", 0, True
End If
If Not fso.FileExists(uiIndex) Then
  sh.Run "cmd /c npm run build:ui", 0, True
End If
If Not fso.FileExists(mainJs) Or Not fso.FileExists(uiIndex) Then
  MsgBox "No se pudo compilar la app (electron/main.js o ui/dist)." & vbCrLf & vbCrLf & _
         "Ejecuta INSTALAR.bat en " & root & " para reparar la instalacion.", 48, "Nexus v3"
  WScript.Quit 1
End If

' Env para el proceso hijo (electron + backend heredan)
Set env = sh.Environment("PROCESS")
env("NEXUS_PYTHON") = py
env("ELECTRON_IS_DEV") = "0"

' 0 = ventana oculta para el lanzador; Electron abre su propia ventana.
sh.Run """" & electron & """ """ & mainJs & """", 0, False
