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
Name: "elevated"; Description: "Let AirShare control windows that run as administrator (starts AirShare with administrator rights at sign-in). Leave unticked unless you need it."; GroupDescription: "Advanced:"; Flags: unchecked

[Files]
; The whole compiled folder. Qt stays as separate, replaceable libraries (LGPL).
Source: "..\build\airshare.dist\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "THIRD-PARTY-NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\AirShare"; Filename: "{app}\airshare.exe"
Name: "{group}\Third-party notices"; Filename: "{app}\THIRD-PARTY-NOTICES.txt"
Name: "{userstartup}\AirShare"; Filename: "{app}\airshare.exe"; Tasks: startup and not elevated

[Run]
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare (TCP 47810)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare discovery (UDP 47812)"""; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""AirShare (TCP 47810)"" dir=in action=allow protocol=TCP localport=47810 profile=private,domain"; Flags: runhidden
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall add rule name=""AirShare discovery (UDP 47812)"" dir=in action=allow protocol=UDP localport=47812 profile=private,domain"; Flags: runhidden
; Elevated mode: a sign-in task with highest privileges (no UAC prompt each time).
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""AirShare"" /F"; Flags: runhidden
Filename: "{sys}\schtasks.exe"; Parameters: "/Create /F /TN ""AirShare"" /SC ONLOGON /RL HIGHEST /IT /RU ""{username}"" /TR ""\""{app}\airshare.exe\"""""; Flags: runhidden; Tasks: elevated
Filename: "{sys}\schtasks.exe"; Parameters: "/Run /TN ""AirShare"""; Description: "Start AirShare now"; Flags: runhidden nowait postinstall skipifsilent; Tasks: elevated
Filename: "{app}\airshare.exe"; Description: "Start AirShare now"; Flags: nowait postinstall skipifsilent runasoriginaluser; Tasks: not elevated

[UninstallRun]
Filename: "{app}\airshare.exe"; Parameters: "--quit"; Flags: runhidden; RunOnceId: "QuitAirShare"
Filename: "{sys}\schtasks.exe"; Parameters: "/Delete /TN ""AirShare"" /F"; Flags: runhidden; RunOnceId: "DelTask"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare (TCP 47810)"""; Flags: runhidden; RunOnceId: "DelTcp"
Filename: "{sys}\netsh.exe"; Parameters: "advfirewall firewall delete rule name=""AirShare discovery (UDP 47812)"""; Flags: runhidden; RunOnceId: "DelUdp"
