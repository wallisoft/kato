; AirShare Windows installer (built by the release workflow from the Nuitka output).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{9B41E7C2-3D58-4F0A-B6E1-7C2A5D9E4F13}
AppName=AirShare
AppVersion={#AppVersion}
AppVerName=AirShare {#AppVersion}
AppPublisher=Wallisoft
AppPublisherURL=https://airshare.eur.bz
DefaultDirName={autopf}\AirShare
DefaultGroupName=AirShare
DisableProgramGroupPage=yes
OutputDir=Output
OutputBaseFilename=AirShare-Setup
SetupIconFile=airshare.ico
UninstallDisplayIcon={app}\airshare.exe
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=admin
CloseApplications=yes

[Tasks]
Name: "startup"; Description: "Start AirShare when I sign in"; GroupDescription: "Options:"

[Types]
Name: "standard"; Description: "Standard"
Name: "full"; Description: "With login and security (UAC) screen control"
Name: "custom"; Description: "Custom"; Flags: iscustom

[Components]
Name: "app"; Description: "AirShare"; Types: standard full custom; Flags: fixed
Name: "secure"; Description: "Control this PC at the Windows login and security (UAC) screens.\nInstalls a background service (SYSTEM). For headless PCs with no keyboard or mouse of their own. Off by default."; Types: full

[Files]
; The whole compiled folder. Qt stays as separate, replaceable libraries (LGPL).
Source: "..\build\airshare.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\build\airshare-service.dist\*"; DestDir: "{app}\service"; Flags: ignoreversion recursesubdirs createallsubdirs; Components: secure

[Icons]
Name: "{group}\AirShare"; Filename: "{app}\airshare.exe"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{userstartup}\AirShare"; Filename: "{app}\airshare.exe"; Tasks: startup

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare (TCP 47810)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare discovery (UDP 47812)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""AirShare (TCP 47810)"" dir=in action=allow protocol=TCP localport=47810 profile=private,domain"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""AirShare discovery (UDP 47812)"" dir=in action=allow protocol=UDP localport=47812 profile=private,domain"; Flags: runhidden
Filename: "{app}\service\airshare-service.exe"; Parameters: "--startup=auto install"; Flags: runhidden; Components: secure
Filename: "{app}\service\airshare-service.exe"; Parameters: "start"; Flags: runhidden; Components: secure
Filename: "{app}\airshare.exe"; Description: "Start AirShare now"; Flags: nowait postinstall skipifsilent runasoriginaluser

[UninstallRun]
Filename: "{app}\service\airshare-service.exe"; Parameters: "stop"; Flags: runhidden; RunOnceId: "StopSvc"; Check: FileExists(ExpandConstant('{app}\service\airshare-service.exe'))
Filename: "{app}\service\airshare-service.exe"; Parameters: "remove"; Flags: runhidden; RunOnceId: "RemoveSvc"; Check: FileExists(ExpandConstant('{app}\service\airshare-service.exe'))
Filename: "{app}\airshare.exe"; Parameters: "--quit"; Flags: runhidden; RunOnceId: "QuitAirShare"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare (TCP 47810)"""; Flags: runhidden; RunOnceId: "DelTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare discovery (UDP 47812)"""; Flags: runhidden; RunOnceId: "DelUdp"
