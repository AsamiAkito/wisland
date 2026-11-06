Set fso = CreateObject("Scripting.FileSystemObject")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
Set ws = CreateObject("Wscript.Shell")
ws.CurrentDirectory = dir
ws.Run """" & dir & "\.venv\Scripts\pythonw.exe"" """ & dir & "\run.pyw""", 0, False
