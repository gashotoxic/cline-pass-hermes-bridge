' ClinePass bridge watchdog - silent launcher (no console window ever)
Set sh = CreateObject("WScript.Shell")
sh.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Users\Gasho\cline-pass-hermes-bridge\watchdog.ps1", 0, False
