' Launch the Camofox stealth-browser server fully hidden (no window),
' bound to 127.0.0.1 only. Started at login (Startup shortcut) and on demand.
CreateObject("WScript.Shell").Run "cmd /c ""D:\projects\camofox-server\run-camofox.cmd""", 0, False
