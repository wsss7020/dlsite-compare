' DLsite compare tool hidden launcher. ASCII only.
' Run from the project directory. Requires pythonw.exe on PATH.
Dim ws, fso, pyw, dir
Set ws = CreateObject("Wscript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(dir & "\server.py") Then
    MsgBox "server.py not found. Keep this script in the project directory."
    WScript.Quit 1
End If
pyw = "pythonw.exe"
ws.CurrentDirectory = dir
ws.Run """" & pyw & """ server.py", 0, False
