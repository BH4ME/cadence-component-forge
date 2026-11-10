# Windows Distribution

`build.ps1` creates a standard Inno Setup installer and a separately movable
payload ZIP. End users do not need Python, pip, Node.js, npm, or Codex. Cadence
Capture and Allegro must be installed separately; no Cadence files are bundled.

## Build

```powershell
.\packaging\windows\build.ps1
```

For a production HTTPS update feed, pass its manifest URL at build time. The
value is validated as an absolute `https://` URL and is stored in the installed
configuration; leaving it empty keeps the existing manual configuration
behavior.

```powershell
.\packaging\windows\build.ps1 `
  -UpdateManifestUrl 'https://updates.example.com/jlc2capture/manifest.json'
```

The build uses the official Python 3.11.9 embeddable x64 runtime (the last
3.11 release with official Windows binary installers), pinned Python packages,
and the JavaScript files named in the panel's package lock. Python's archive
hash, npm integrity hashes and Windows executable signatures are checked.
The official Inno Setup 6.4.3 compiler is installed under this directory's
`tools/inno-6.4.3`; the compiler itself is not in the distributable payload.
Its SHA-256 is pinned to the `.issig` published at
`https://files.jrsoftware.org/is/6/innosetup-6.4.3.exe.issig`.
Pass `-Iscc <path>` to use an already provisioned compiler.

Artifacts appear in a new `dist/build_<timestamp>/` directory:

- `JLC2Cadence-<version>-Setup.exe`: installer and registered uninstaller.
- `JLC2Cadence-<release-id>-payload.zip`: complete release contents.
- `payload/payload-manifest.json`: file hashes and runtime source provenance.
- `build-info.json`: the same release manifest for release publishing tools.
- `payload/licenses/`: third-party notices and a dependency version list.

There are no production signing keys, private keys, update addresses, or
developer credentials in the payload. The default update key file contains an
empty `keys` object. An administrator must provision the actual production
public keys and configure the HTTPS update feed before online updates work.

## Installation Contract

Normal installation requests administrator access to register the extension
inside selected Cadence roots. The wizard accepts both 17.2 and 17.4, locates
common installations, and checks the actual Capture executable's version
resource. Either version can be installed alone; leave the other root empty.
The path page provides a folder browser for each optional root and requires
at least one valid selection. It never launches or closes either Cadence application.

```text
<install-root>/
  bootstrap.tcl
  active-release.tcl
  install-state.json
  installer/manage.py
  installer/update-public-keys.json
  releases/<release-id>/
    runtime/python.exe
    runtime/python311._pth
    runtime/Lib/site-packages/
    app/src/jlc2cadence/
    app/capture_plugin/
    licenses/
    payload-manifest.json
```

The default install root is `Program Files/JLC2Cadence`. Persistent configuration
is `ProgramData/JLC2Cadence/config.json`. The default component library is
`Documents/JLC2Cadence/library`; the user can choose a different base directory.
The plugin isolates generated assets in `cadence-17.2` and `cadence-17.4`
subdirectories. Existing configuration fields and library paths survive upgrades.
Silent upgrades restore the previous Cadence roots, custom state directory and
installation privilege scope. An installation first made with `/CURRENTUSER`
remains in HKCU when the updater launches `/SILENT /NORESTART`.

Each selected root receives only
`tools/capture/tclscripts/capAutoLoad/jlc2capture_init.tcl`. Its first line records
this installation's owner identity. An existing initializer belonging to a
different installation is rejected before registration changes. A previous
development initializer must be explicitly migrated; it is never overwritten
silently or loaded alongside a second initializer.

The initializer sets `JLC2CAPTURE_CADENCE_ROOT` and
`JLC2CAPTURE_CADENCE_VERSION`. The active release pointer sets
`JLC2CAPTURE_CONFIG`, `JLC2CAPTURE_PYTHON`, and the installed fixed
`JLC2CAPTURE_TRUSTED_KEYS` file, then loads the matching plugin.
Tcllib base64 decoding keeps these generated scripts compatible with Tcl 8.5.
The embedded runtime uses relative search paths, independently of system
Python, `PYTHONPATH`, or the working directory.

## Upgrade And Uninstall

A new release is installed alongside the previous one. Registration first
checks every manifest file and tests the bundled runtime. Pointer, configuration,
state and owned initializer changes form one recoverable transaction. On a
normal write failure the old bytes are restored; a persisted transaction is
also available for an explicit administrator rollback. The previous release's
program files remain available. No running Capture process is killed; restarting
Capture activates the new release.

```powershell
# Silent deployment; provide real, existing Cadence roots.
.\JLC2Cadence-<version>-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES /NORESTART `
  /CADENCE172="C:\Cadence\SPB_17.2" /CADENCE174="C:\Cadence\SPB_17.4"

# Standard Windows uninstall, also available through Installed Apps.
& '<install-root>\unins000.exe' /VERYSILENT /SUPPRESSMSGBOXES /NORESTART

# Explicit rollback to the recorded previous registration/configuration.
& '<active-release>\runtime\python.exe' '<install-root>\installer\manage.py' `
  rollback --install-root '<install-root>'
```

Uninstall first removes only initializers whose owner matches this installation.
If another owner replaced an initializer, that file is retained. Inno then
removes its recorded application files. User libraries, all Cadence projects,
configuration, and rollback evidence remain. There is no recursive deletion of
user-selected library or Cadence directories.

## Isolated Acceptance

```powershell
.\.venv-capture\Scripts\python.exe -m unittest discover -s tests -p test_windows_install.py
$env:JLC_WINDOWS_SETUP = '<absolute path to Setup.exe>'
# Optionally point at an earlier built installer to test a real release upgrade.
$env:JLC_WINDOWS_PREVIOUS_SETUP = '<absolute path to previous Setup.exe>'
.\.venv-capture\Scripts\python.exe -m unittest discover -s tests -p test_windows_install_setup.py
```

The opt-in executable test compiles a clearly named `TEST-ONLY-Setup.exe` with
a unique directory, fake Cadence roots, a static separate registry identity,
and a separate state directory fixed at compile time. Production installers
use the static product GUID. The test creates dummy user project/library files
and tests install, a rejected foreign initializer, preserved active
state after a failed upgrade, a successful upgrade, the portable Python import
closure, and real uninstall. It also installs 17.2 alone and 17.4 alone with
the unused version's folder absent, rejects an empty selection, and repairs each
single-version installation using the updater's path-free command line.
It saves `acceptance.json` and installer logs.
It does not launch Cadence or touch any real Cadence initializer. Set
`JLC_WINDOWS_TEST_ROOT` to a dedicated temporary directory on another drive
when the workspace drive has little free space. The signed update integration
test uses `JLC_WINDOWS_OLD_BUILD` and `JLC_WINDOWS_NEW_BUILD` to compile two
isolated releases, publishes an ephemeral-key-signed loopback feed, and invokes
the shipped updater/runtime with fixed production installer arguments. It checks
the actual HKCU/HKLM scope, custom state/Cadence paths, previous release,
rollback and uninstall. The ephemeral private key remains in memory and never
enters an artifact. These fixtures
verify installer behavior; they do not claim native 17.2/17.4 library validation.
