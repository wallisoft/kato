; Juska Windows installer (built by the release workflow from the Nuitka output).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{4E7A1C93-8B26-4D5F-A0C7-2F6B9D31E8A4}
AppName=Juska
AppVersion={#AppVersion}
AppVerName=Juska {#AppVersion}
AppPublisher=Wallisoft
AppPublisherURL=https://juska.uk
DefaultDirName={autopf}\Juska
DefaultGroupName=Juska
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=Juska-Setup
SetupIconFile=juska.ico
UninstallDisplayIcon={app}\juska.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes

[Tasks]
Name: "startup"; Description: "Start Juska when I sign in"; GroupDescription: "Options:"

[Types]
Name: "standard"; Description: "Standard"
Name: "full"; Description: "With login and security (UAC) screen control"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app"; Description: "Juska"; Types: standard full custom; Flags: fixed
Name: "secure"; Description: "Control this PC at the Windows lock screen and at security (UAC) prompts.\nInstalls a background service (SYSTEM) that only the installed Juska can use. Off by default."; Types: full

[Files]
; The whole compiled folder. Qt stays as separate, replaceable libraries (LGPL).
Source: "..\build\juska.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\juska_service.dist\*"; DestDir: "{app}\service"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: secure

[Icons]
Name: "{group}\Juska"; Filename: "{app}\juska.exe"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{userstartup}\Juska"; Filename: "{app}\juska.exe"; Tasks: startup

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Juska (TCP 47810)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Juska discovery (UDP 47812)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Juska (TCP 47810)"" dir=in action=allow protocol=TCP localport=47810 profile=private,domain"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""Juska discovery (UDP 47812)"" dir=in action=allow protocol=UDP localport=47812 profile=private,domain"; Flags: runhidden
Filename: "{app}\service\juska-service.exe"; Parameters: "install"; Flags: runhidden; Components: secure
Filename: "{app}\juska.exe"; Description: "Start Juska now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{app}\service\juska-service.exe"; Parameters: "remove"; Flags: runhidden; RunOnceId: "RemoveSvc1"; Check: FileExists(ExpandConstant('{app}\service\juska-service.exe'))
Filename: "{app}\juska.exe"; Parameters: "--quit"; Flags: runhidden; RunOnceId: "QuitJuska"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Juska (TCP 47810)"""; Flags: runhidden; RunOnceId: "DelTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""Juska discovery (UDP 47812)"""; Flags: runhidden; RunOnceId: "DelUdp"

[Code]
// Files that are in use can't be replaced: stop the input service and its agent before a new version is copied in.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  rc: Integer;
begin
  Exec(ExpandConstant('{sys}\sc.exe'), 'stop JuskaInput', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/F /IM juska-service.exe', '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(700);
  Result := '';
end;
