; Inno Setup script for Spotify Ads Skipper.

#define MyAppName "Spotify Ads Skipper"
#define MyAppVersion "3.0.1"
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
; No elevation: the app writes inside %APPDATA%\Spotify, keeps its own state in
; %LOCALAPPDATA%, and installs its CA into the *user* certificate store. With
; "lowest", the auto* constants resolve to per-user locations, so the whole
; install stays in user space.
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
; Where a pre-existing PAC URL is parked while the app is running, so it can be
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

// The certificate notice lives here rather than popping up after the install,
// so the whole decision happens inside the one wizard the user is already
// clicking through. Setup records the answer, and the application finds it and
// starts working without asking again. Running from source still asks for
// itself, since there is no installer on that path.
// An information page, not a checkbox to tick. Continuing IS the agreement,
// which is how every other page in an installer already works - and a tick box
// would be one more thing to click for someone who came here to listen to
// music. It also removes the validation that has to be got right: an earlier
// version of this page could be walked past unticked on the second attempt.
procedure InitializeWizard();
begin
  CreateOutputMsgPage(
    wpSelectTasks,
    'Local certificate',
    'Spotify Ads Skipper installs one thing alongside itself.',
    'Spotify sends its ads down the same encrypted connection as your music, ' +
    'so telling them apart means inspecting that connection on this computer. ' +
    'That needs a certificate trusted by your Windows account, and Setup will ' +
    'add one.' + #13#10#13#10 +
    'It is created here and never leaves this computer. It can only vouch for ' +
    'spotify.com, scdn.co and spotifycdn.com, and only for websites - Windows ' +
    'rejects anything it signs for another site, for an IP address, or for ' +
    'e-mail and program signing.' + #13#10#13#10 +
    'While the app is running, traffic to those Spotify addresses is decrypted ' +
    'on this computer, a Spotify tab in your browser included. Nothing is ' +
    'stored and nothing is sent anywhere.' + #13#10#13#10 +
    'Uninstalling removes the certificate, and so does "Remove local ' +
    'certificate" in the tray menu.' + #13#10#13#10 +
    'Click Next to continue, or Cancel to install nothing.');
end;

procedure RecordConsent();
var
  Dir: String;
begin
  // Written where the application looks for it, so it starts blocking
  // immediately instead of asking the same question a second time.
  Dir := ExpandConstant('{localappdata}\SpotifyAdsSkipper');
  if not DirExists(Dir) then
    ForceDirectories(Dir);
  SaveStringToFile(Dir + '\settings.json', '{' + #13#10 +
                   '  "consented": true' + #13#10 + '}' + #13#10, False);
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
    // Both results were discarded here. Exec uses CreateProcess, so an
    // unelevated setup launching a requireAdministrator uninstaller can fail
    // with ERROR_ELEVATION_REQUIRED before any prompt is shown - the user sees
    // nothing happen and setup carried on as if the old copy were gone. It is
    // not: its HKLM Run value and its machine-wide hosts-file block survive,
    // and this per-user uninstaller can never remove either.
    if (not Exec(Uninstaller, '/VERYSILENT /SUPPRESSMSGBOXES /NORESTART', '',
                 SW_SHOW, ewWaitUntilTerminated, ResultCode))
       or (OldMachineWideUninstaller() <> '') then
    begin
      MsgBox('The older version could not be removed.' + #13#10#13#10 +
             'Remove "Spotify Ads Skipper" from Settings > Apps first, then '
             + 'run this installer again.', mbError, MB_OK);
      Result := False;
    end;
  end
  else
    Result := False;
end;

// Removing the CA depends on the application executable, which [UninstallRun]
// invokes just before deleting it. If that copy is missing - quarantined by an
// antivirus, deleted by hand, or simply failing - nothing else would take the
// certificate out of the store, and the very next step deletes the file that
// identifies it. This runs certutil directly, needs nothing of ours to survive,
// and is harmless when the earlier removal already succeeded.
procedure CurStepChanged(CurStep: TSetupStep);
begin
  // ssPostInstall, so the answer is only recorded once the files are actually
  // there. Cancelling anywhere earlier writes nothing.
  if CurStep = ssPostInstall then
    RecordConsent();
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  ResultCode: Integer;
begin
  if CurUninstallStep = usUninstall then
    Exec(ExpandConstant('{sys}\certutil.exe'),
         '-user -delstore Root "Spotify Ads Skipper Local CA"', '',
         SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;
