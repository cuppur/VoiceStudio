#define MyAppName "本地声音工坊"
#define MyAppVersion "0.2.0"
#define MyAppPublisher "LocalVoiceStudio"
#define MyAppExeName "LocalVoiceStudio.exe"

[Setup]
AppId={{6E65C236-CE89-43A3-BCC1-1ED9CC7F972A}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\LocalVoiceStudio
DefaultGroupName={#MyAppName}
PrivilegesRequired=lowest
OutputDir=..\dist\installer
OutputBaseFilename=LocalVoiceStudio-Setup-{#MyAppVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
UninstallDisplayIcon={app}\{#MyAppExeName}
CloseApplications=yes
RestartApplications=no

[Files]
Source: "..\dist\LocalVoiceStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; 用户模型、项目、录音和输出均位于安装目录之外，卸载时故意保留。
