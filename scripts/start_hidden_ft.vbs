' Lanza nexus-v3 (backend :8765) oculto, con Python free-threaded del venv.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "D:\ias\proyectos\nexus-v3"
sh.Run "cmd /c scripts\run_ft.bat 8765", 0, False
Set sh = Nothing
