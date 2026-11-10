#ifndef PayloadDir
  #error PayloadDir must name a verified payload directory
#endif
#ifndef ReleaseId
  #error ReleaseId is required
#endif
#ifndef AppVersion
  #error AppVersion is required
#endif
#ifndef OutputDirectory
  #define OutputDirectory "dist"
#endif
#ifndef UpdateManifestUrl
  #define UpdateManifestUrl ""
#endif

[Setup]
#ifdef FixtureRoot
AppId={#FixtureId}
#else
AppId={{6B9BE619-F405-4F07-AF55-64AAEA389B46}
#endif
#ifdef FixtureRoot
AppName=JLC2Cadence TEST ONLY
#else
AppName=JLC2Cadence
#endif
AppVersion={#AppVersion}
AppPublisher=JLC2Cadence Project
#ifdef FixtureRoot
DefaultDirName={#FixtureRoot}\program
PrivilegesRequired=admin
DisableDirPage=yes
#else
DefaultDirName={autopf}\JLC2Cadence
PrivilegesRequired=admin
#endif
DefaultGroupName=JLC2Cadence
DisableProgramGroupPage=yes
PrivilegesRequiredOverridesAllowed=dialog commandline
UsePreviousPrivileges=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
OutputDir={#OutputDirectory}
#ifdef FixtureRoot
OutputBaseFilename=JLC2Cadence-{#AppVersion}-TEST-ONLY-Setup
#else
OutputBaseFilename=JLC2Cadence-{#AppVersion}-Setup
#endif
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
#ifdef FixtureRoot
UninstallDisplayName=JLC2Cadence TEST ONLY
#else
UninstallDisplayName=JLC2Cadence
#endif
CloseApplications=no
RestartApplications=no
SetupLogging=yes
UsePreviousAppDir=yes
UsePreviousLanguage=no

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}\releases\{#ReleaseId}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "manage.py"; DestDir: "{app}\installer"; Flags: ignoreversion
Source: "update-public-keys.json"; DestDir: "{app}\installer"; Flags: onlyifdoesntexist

[Dirs]
Name: "{code:GetStateRoot}"; Permissions: users-modify

[UninstallDelete]
Type: files; Name: "{app}\bootstrap.tcl"
Type: files; Name: "{app}\active-release.tcl"
Type: files; Name: "{app}\install-state.json"
Type: files; Name: "{app}\registration-result.json"

[Code]
var
  CadencePage: TInputQueryWizardPage;
  CadenceBrowseButtons: array[0..1] of TNewButton;
  InitialCadenceValues: array[0..1] of String;
  LibraryPage: TInputDirWizardPage;
  StateRoot: String;
  StateRootExplicit: Boolean;
  RegistrationFailed: Boolean;

function GetStateRoot(Param: String): String;
begin
  Result := StateRoot;
end;

function GuessRoot(Version: String): String;
var
  Candidate, EnvironmentValue, VersionFolder: String;
begin
  Result := '';
  EnvironmentValue := GetEnv('CDSROOT');
  if (Pos(Version, EnvironmentValue) > 0) and FileExists(EnvironmentValue + '\tools\bin\capture.exe') then
    Result := EnvironmentValue;
  Candidate := 'C:\Cadence\SPB_' + Version;
  if FileExists(Candidate + '\tools\bin\capture.exe') then Result := Candidate;
  VersionFolder := Version;
  StringChangeEx(VersionFolder, '.', '_', True);
  Candidate := 'D:\Cadence_' + VersionFolder + '\Cadence\SPB_' + Version;
  if FileExists(Candidate + '\tools\bin\capture.exe') then Result := Candidate;
end;

function CadenceValue(ParameterName, PreviousKey, DefaultValue: String): String;
var
  ParameterValue: String;
begin
  { A sentinel distinguishes an absent parameter from an explicitly empty one. }
  ParameterValue := ExpandConstant('{param:' + ParameterName + '|__JLC_PARAM_ABSENT__}');
  if ParameterValue <> '__JLC_PARAM_ABSENT__' then
    Result := ParameterValue
  else if WizardSilent then
    Result := GetPreviousData(PreviousKey, '')
  else
    Result := GetPreviousData(PreviousKey, DefaultValue);
end;

procedure BrowseCadenceRoot(Sender: TObject);
var
  Index: Integer;
  Directory: String;
begin
  if Sender = CadenceBrowseButtons[0] then Index := 0 else Index := 1;
  Directory := CadencePage.Values[Index];
  if BrowseForFolder('Select Cadence installation root', Directory, False) then
    CadencePage.Values[Index] := Directory;
end;

procedure InitializeWizard;
var
  Index: Integer;
begin
  CadencePage := CreateInputQueryPage(wpSelectDir, 'Cadence installations',
    'Select Capture / Allegro 17.2 and/or 17.4',
    'Leave an unused version empty. Existing projects and component libraries are preserved.');
  CadencePage.Add('Cadence 17.2 root:', False);
  CadencePage.Add('Cadence 17.4 root:', False);
  for Index := 0 to 1 do begin
    CadenceBrowseButtons[Index] := TNewButton.Create(CadencePage);
    CadenceBrowseButtons[Index].Parent := CadencePage.Surface;
    CadenceBrowseButtons[Index].Caption := 'Browse...';
    CadenceBrowseButtons[Index].Width := WizardForm.CalculateButtonWidth(['Browse...']);
    CadenceBrowseButtons[Index].Height := CadencePage.Edits[Index].Height;
    CadenceBrowseButtons[Index].Left := CadencePage.SurfaceWidth - CadenceBrowseButtons[Index].Width;
    CadenceBrowseButtons[Index].Top := CadencePage.Edits[Index].Top;
    CadenceBrowseButtons[Index].Anchors := [akTop, akRight];
    CadenceBrowseButtons[Index].OnClick := @BrowseCadenceRoot;
    CadencePage.Edits[Index].Width := CadenceBrowseButtons[Index].Left - ScaleX(8);
  end;
  CadencePage.Values[0] := CadenceValue('CADENCE172', 'Cadence172', GuessRoot('17.2'));
  CadencePage.Values[1] := CadenceValue('CADENCE174', 'Cadence174', GuessRoot('17.4'));
  LibraryPage := CreateInputDirPage(CadencePage.ID, 'Component library',
    'Choose a folder for imported components', 'This folder and your configuration remain after uninstall.', False, '');
  LibraryPage.Add('Library folder:');
  LibraryPage.Values[0] := ExpandConstant('{param:LIBRARYROOT|' + GetPreviousData('LibraryRoot', ExpandConstant('{userdocs}\JLC2Cadence\library')) + '}');
  StateRoot := ExpandConstant('{param:STATEROOT|}');
  StateRootExplicit := StateRoot <> '';
  if not StateRootExplicit then StateRoot := GetPreviousData('StateRoot', ExpandConstant('{commonappdata}\JLC2Cadence'));
#ifdef FixtureRoot
  CadencePage.Values[0] := CadenceValue('CADENCE172', 'Cadence172', '{#FixtureRoot}\Cadence 17.2');
  CadencePage.Values[1] := CadenceValue('CADENCE174', 'Cadence174', '{#FixtureRoot}\Cadence 17.4');
#ifdef FixtureCadenceBase
  CadencePage.Values[0] := '{#FixtureCadenceBase}\Cadence 17.2';
  CadencePage.Values[1] := '{#FixtureCadenceBase}\Cadence 17.4';
#endif
  LibraryPage.Values[0] := '{#FixtureRoot}\user library';
  if not StateRootExplicit then StateRoot := GetPreviousData('StateRoot', '{#FixtureRoot}\persistent');
#ifdef FixtureStateRoot
  StateRoot := '{#FixtureStateRoot}';
#endif
#endif
  InitialCadenceValues[0] := CadencePage.Values[0];
  InitialCadenceValues[1] := CadencePage.Values[1];
end;

procedure RegisterPreviousData(PreviousDataKey: Integer);
begin
  SetPreviousData(PreviousDataKey, 'StateRoot', StateRoot);
  SetPreviousData(PreviousDataKey, 'LibraryRoot', LibraryPage.Values[0]);
  SetPreviousData(PreviousDataKey, 'Cadence172', CadencePage.Values[0]);
  SetPreviousData(PreviousDataKey, 'Cadence174', CadencePage.Values[1]);
end;

function ValidCadenceRoot(Root: String): Boolean;
begin
  Result := (Root = '') or (FileExists(Root + '\tools\bin\capture.exe') and
    FileExists(Root + '\tools\bin\allegro.exe') and DirExists(Root + '\tools\capture\tclscripts\capAutoLoad'));
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = CadencePage.ID then begin
    CadencePage.Values[0] := Trim(CadencePage.Values[0]);
    CadencePage.Values[1] := Trim(CadencePage.Values[1]);
    if WizardSilent then
      Result := True
    else
      Result := ((CadencePage.Values[0] <> '') or (CadencePage.Values[1] <> '')) and
        ValidCadenceRoot(CadencePage.Values[0]) and ValidCadenceRoot(CadencePage.Values[1]);
    if not Result then SuppressibleMsgBox('Select at least one valid Cadence root containing Capture, Allegro and capAutoLoad.', mbError, MB_OK, IDOK);
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Arguments, PythonPath, ResultPath: String;
  ResultText: AnsiString;
  ResultCode: Integer;
begin
  if CurStep = ssPostInstall then begin
    PythonPath := ExpandConstant('{app}\releases\{#ReleaseId}\runtime\python.exe');
    Arguments := '"' + ExpandConstant('{app}\installer\manage.py') + '" install --install-root "' +
      ExpandConstant('{app}') + '" --release-id "{#ReleaseId}" --state-root "' + StateRoot +
      '" --library-root "' + LibraryPage.Values[0] + '"';
    ResultPath := ExpandConstant('{app}\registration-result.json');
    Arguments := Arguments + ' --output "' + ResultPath + '"';
    if not StateRootExplicit then Arguments := Arguments + ' --preserve-state-root';
    if (ExpandConstant('{param:CADENCE172|__JLC_PARAM_ABSENT__}') = '__JLC_PARAM_ABSENT__') and
       (CadencePage.Values[0] = InitialCadenceValues[0]) then
      Arguments := Arguments + ' --preserve-cadence172';
    if (ExpandConstant('{param:CADENCE174|__JLC_PARAM_ABSENT__}') = '__JLC_PARAM_ABSENT__') and
       (CadencePage.Values[1] = InitialCadenceValues[1]) then
      Arguments := Arguments + ' --preserve-cadence174';
    if CadencePage.Values[0] <> '' then Arguments := Arguments + ' --cadence172 "' + CadencePage.Values[0] + '"';
    if CadencePage.Values[1] <> '' then Arguments := Arguments + ' --cadence174 "' + CadencePage.Values[1] + '"';
#if UpdateManifestUrl != ""
    Arguments := Arguments + ' --update-manifest-url "{#UpdateManifestUrl}"';
#endif
#ifdef FixtureRoot
    Arguments := Arguments + ' --allow-fixture';
#endif
    RegistrationFailed := not Exec(PythonPath, Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0);
    if LoadStringFromFile(ResultPath, ResultText) then Log('Registration result: ' + String(ResultText));
    if RegistrationFailed then
      RaiseException('Registration failed. The previous active release and configuration have been preserved. See Setup log.');
  end;
end;

function GetCustomSetupExitCode(): Integer;
begin
  if RegistrationFailed then Result := 10 else Result := 0;
end;

function InitializeUninstall(): Boolean;
var
  ResultCode: Integer;
  Arguments: String;
begin
  Arguments := '"' + ExpandConstant('{app}\installer\manage.py') + '" uninstall --install-root "' + ExpandConstant('{app}') + '"';
  Result := Exec(ExpandConstant('{app}\releases\{#ReleaseId}\runtime\python.exe'), Arguments, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
  if not Result then MsgBox('Cannot safely unregister the owned Cadence initializer. Program files have been retained.', mbError, MB_OK);
end;

procedure DeinitializeSetup();
begin
#ifdef FixtureRoot
  if ExpandConstant('{log}') <> '' then
    FileCopy(ExpandConstant('{log}'), '{#FixtureRoot}\last-installer.log', False);
#endif
end;
