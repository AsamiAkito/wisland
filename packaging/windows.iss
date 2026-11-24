[Setup]
AppId={{AC875173-B628-420D-9BD5-CE4CA81AF3F9}
AppName=Wisland
AppVersion={#AppVersion}
AppPublisher=Wisland
AppPublisherURL=https://github.com/AsamiAkito/wisland
DefaultDirName={localappdata}\Programs\Wisland
DefaultGroupName=Wisland
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
AllowNoIcons=yes
DisableProgramGroupPage=yes
Compression=lzma2/fast
SolidCompression=yes
RestartApplications=no
OutputDir={#OutputDir}
OutputBaseFilename=wisland-{#AppVersion}-windows-x64-setup
UninstallDisplayName=Wisland
WizardStyle=modern

[Files]
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\Wisland"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-B ""{app}\run.pyw"""; WorkingDir: "{app}"
Name: "{group}\Uninstall Wisland"; Filename: "{uninstallexe}"

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-B ""{app}\run.pyw"""; WorkingDir: "{app}"; Description: "Start Wisland"; Flags: postinstall nowait skipifsilent

[UninstallDelete]
#include CleanupRules

[Code]
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  RunKey, Command, Expected: String;
begin
  if CurUninstallStep = usUninstall then begin
    RunKey := 'Software\Microsoft\Windows\CurrentVersion\Run';
    Expected := ExpandConstant('"{app}\runtime\pythonw.exe" -B "{app}\run.pyw"');
    if RegQueryStringValue(HKCU, RunKey, 'wisland', Command) then
      if CompareText(Command, Expected) = 0 then
        RegDeleteValue(HKCU, RunKey, 'wisland');
  end;
end;
