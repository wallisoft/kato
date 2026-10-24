; Kato Windows installer (built by the release workflow from the Nuitka output).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{093802F1-5FD7-4B25-9375-9D3FE2420975}
AppName=Kato
AppVersion={#AppVersion}
AppVerName=Kato {#AppVersion}
AppPublisher=Wallisoft
AppPublisherURL=https://kato.vml.onl
DefaultDirName={autopf}\Kato
DefaultGroupName=Kato
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Kato-Setup
SetupIconFile=kato.ico
UninstallDisplayIcon={app}\kato.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes

[Tasks]
Name: "startup"; Description: "Start Kato when I sign in"; GroupDescription: "Options:"

[Types]
Name: "standard"; Description: "Standard"
Name: "full"; Description: "With login and security (UAC) screen control"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app"; Description: "Kato"; Types: standard full custom; Flags: fixed
Name: "secure"; Description: "Control this PC at the Windows lock screen and at security (UAC) prompts.\nInstalls a background service (SYSTEM) that only the installed Kato can use. Off by default."; Types: full

[Files]
; The whole compiled folder. Qt stays as separate, replaceable libraries (LGPL).
Source: "..\build\kato.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\kato_service.dist\*"; DestDir: "{app}\service"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: secure

[Icons]
Name: "{group}\Kato"; Filename: "{app}\kato.exe"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{userstartup}\Kato"; Filename: "{app}\kato.exe"; Tasks: startup

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Kato (TCP 47810)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Kato discovery (UDP 47812)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Kato (TCP 47810)"" dir=in action=allow protocol=TCP localport=47810 profile=private,domain"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Kato discovery (UDP 47812)"" dir=in action=allow protocol=UDP localport=47812 profile=private,domain"; Flags: runhidden
Filename: "{app}\service\kato-service.exe"; Parameters: "install"; Flags: runhidden; Components: secure
Filename: "{app}\kato.exe"; Description: "Start Kato now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{app}\service\kato-service.exe"; Parameters: "remove"; Flags: runhidden; RunOnceId: "RemoveSvc1"; Check: FileExists(ExpandConstant('{app}\service\kato-service.exe'))
Filename: "{app}\kato.exe"; Parameters: "--quit"; Flags: runhidden; RunOnceId: "QuitKato"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Kato (TCP 47810)"""; Flags: runhidden; RunOnceId: "DelTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Kato discovery (UDP 47812)"""; Flags: runhidden; RunOnceId: "DelUdp"

[Code]
// Files that are in use can't be replaced: stop the input service and its agent before a new version is copied in.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  rc: Integer;
begin
  // Kato was called AirShare before 0.10. A copy still running would keep Kato's ports and Kato would quietly
  // step aside for it, so stop it (and its service) first. Harmless when there is nothing to stop.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM kato.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop AirShareInput', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM kato-service.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop KatoInput', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM kato-service.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(700);
  Result := '';
end;
