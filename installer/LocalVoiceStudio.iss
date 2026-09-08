#define MyAppName "本地声音工坊"
#define MyAppVersion "1.0.0"
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
#ifdef SignBuild
SignTool=voicestudio
SignedUninstaller=yes
#endif

[Files]
Source: "..\dist\LocalVoiceStudio\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务："; Flags: checkedonce

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

[Code]
var
  DeleteUserData: Boolean;

function InitializeUninstall: Boolean;
begin
  DeleteUserData := False;
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usUninstall then
  begin
    if (not UninstallSilent) and (MsgBox('是否删除默认 VoiceStudio 用户数据（项目、声音、录音与导出）？' + #13#10 +
              '将清理 LocalAppData 与 Documents\LocalVoiceStudio。' + #13#10 +
              '自定义保存到其他位置的工程不会自动删除。' + #13#10 +
              '选择“否”将保留默认数据。',
              mbConfirmation, MB_YESNO) = IDYES) then
      DeleteUserData := True;
  end
  else if (CurUninstallStep = usPostUninstall) and DeleteUserData then
    begin
      DelTree(ExpandConstant('{localappdata}\LocalVoiceStudio'), True, True, True);
      DelTree(ExpandConstant('{userprofile}\Documents\LocalVoiceStudio'), True, True, True);
    end;
end;

[UninstallDelete]
; 用户模型、项目、录音和输出均位于安装目录之外，卸载时故意保留。
Type: filesandordirs; Name: "{app}"
