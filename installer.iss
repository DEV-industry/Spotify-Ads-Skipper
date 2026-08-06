; Inno Setup script for Spotify Ads Skipper.

#define MyAppName "Spotify Ads Skipper"
#define MyAppVersion "3.0"
#define MyAppPublisher "DEV Industry"
#define MyAppURL "https://github.com/DEV-industry/Spotify-Ads-Skipper"
#define MyAppExeName "Spotify-Ads-Skipper.exe"

[Setup]
; NOTE: The value of AppId uniquely identifies this application. Do not use the same AppId value in installers for other applications.
AppId={{158C9421-905F-4D04-96BE-794943697881}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}
AppUpdatesURL={#MyAppURL}
DefaultDirName={autopf}\{#MyAppName}
DisableProgramGroupPage=yes
; No elevation: the app writes only inside %APPDATA%\Spotify and changes
; per-application volume. With "lowest", the auto* constants resolve to
; per-user locations, so the whole install stays in user space.
PrivilegesRequired=lowest
OutputDir=installer_dist
OutputBaseFilename=SpotifyAdsSkipper_Setup
SetupIconFile=cat.ico
Compression=lzma
SolidCompression=yes
WizardStyle=modern
; Without this a running copy keeps the proxy and the PAC server alive while
; the uninstaller is already removing the CA underneath it.
AppMutex=Local\SpotifyAdsSkipper.SingleInstance
CloseApplications=yes
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "startup"; Description: "Automatically start with Windows"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "dist\Spotify-Ads-Skipper.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "cat.ico"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{autoprograms}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Registry]
; HKA follows PrivilegesRequired, so this lands in HKCU for a per-user install.
Root: HKA; Subkey: "SOFTWARE\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "{#MyAppName}"; ValueData: """{app}\{#MyAppExeName}"""; Flags: uninsdeletevalue; Tasks: startup
; Where a pre-existing PAC URL is parked while seamless mode is on, so it can be
; put back. Removed wholesale on uninstall.
Root: HKCU; Subkey: "Software\SpotifyAdsSkipper"; Flags: uninsdeletekeyifempty dontcreatekey

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

[UninstallRun]
; Restore Spotify's original UI bundle, remove the local CA and clear the proxy
; routing before the executable is removed. This is the only thing that can undo
; those changes, so it must run while the exe is still present.
Filename: "{app}\{#MyAppExeName}"; Parameters: "--cleanup"; RunOnceId: "RestoreSpotifyUI"; Flags: waituntilterminated

[UninstallDelete]
; Logs, settings and the CA live outside {app} and are created after install,
; so Inno knows nothing about them and would leave them behind.
Type: filesandordirs; Name: "{localappdata}\SpotifyAdsSkipper"
Type: filesandordirs; Name: "{userappdata}\SpotifyAdsSkipper"

[Code]
// Version 2 installed per-machine and elevated, so its uninstall entry lives in
// HKLM and this per-user installer cannot see or replace it. Left alone, the
// old copy stays in Program Files and its HKLM Run entry keeps starting the
// previous build at every logon - the one without any of the current
// hardening. Offer to remove it first.
function OldMachineWideUninstaller(): String;
var
  Key: String;
  Value: String;
begin
  Result := '';
  Key := 'Software\Microsoft\Windows\CurrentVersion\Uninstall\' +
         '{158C9421-905F-4D04-96BE-794943697881}_is1';
  if RegQueryStringValue(HKEY_LOCAL_MACHINE, Key, 'UninstallString', Value) then
    Result := RemoveQuotes(Value);
end;

function InitializeSetup(): Boolean;
var
  Uninstaller: String;
  ResultCode: Integer;
begin
  Result := True;
  Uninstaller := OldMachineWideUninstaller();
  if Uninstaller = '' then
    Exit;

  if MsgBox('An older system-wide version of Spotify Ads Skipper is installed.' + #13#10#13#10 +
            'It has to be removed first, otherwise it will keep starting itself ' +
            'at sign-in alongside this one.' + #13#10#13#10 +
            'Remove it now? (You may be asked for administrator permission.)',
            mbConfirmation, MB_YESNO) = IDYES then
  begin
    Exec(Uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
         SW_SHOW, ewWaitUntilTerminated, ResultCode);
  end
  else
    Result := False;
end;
