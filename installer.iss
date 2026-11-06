; Inno Setup Script for NavModel Manager 0.0.11 Windows x64
#define MyAppName "NavModel Manager"
#define MyAppVersion "0.0.11"
#define MyAppPublisher "Carlos Morais"
#define MyAppURL "https://github.com/Carlosgmorais/NavModel_Manager"
#define MyAppExeName "NavModel Manager.exe"

[Setup]
AppId={{5C16C4D3-E2D9-4B59-86A1-94D290F57102}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=release
OutputBaseFilename=NavModel_Manager_0.0.11_Setup_x64
SetupIconFile=resources\navmodel.ico
WizardImageFile=resources\installer_wizard.bmp
WizardSmallImageFile=resources\installer_banner.bmp
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible
ChangesAssociations=yes
PrivilegesRequired=lowest
UninstallDisplayIcon={app}\{#MyAppExeName}

[Languages]
Name: "brazilianportuguese"; MessagesFile: "compiler:Languages\BrazilianPortuguese.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\NavModel Manager\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; File association for .pvproj
Root: HKA; Subkey: "Software\Classes\.pvproj"; ValueType: string; ValueName: ""; ValueData: "PlantView.Project"; Flags: uninsdeletevalue
Root: HKA; Subkey: "Software\Classes\PlantView.Project"; ValueType: string; ValueName: ""; ValueData: "Projeto NavModel Manager"; Flags: uninsdeletekey
Root: HKA; Subkey: "Software\Classes\PlantView.Project\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"
Root: HKA; Subkey: "Software\Classes\PlantView.Project\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

