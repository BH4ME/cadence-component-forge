# Capture plugin

The plugin adds a `JLC/LCSC` menu to OrCAD Capture 17.2/17.4. With Capture's CEF bridge it creates a right-docked control bar; without that bridge it opens the Tcl/Tk fallback window.

## Install from a checkout

```powershell
.\capture_plugin\install.ps1 `
  -CadenceRoot 'C:\Cadence\SPB_17.4' `
  -Python 'C:\Python311\python.exe' `
  -LibraryRoot 'C:\Users\Public\JLC2Cadence\library'
```

Restart Capture, then choose `JLC/LCSC -> Search Components...`. The installer writes only the owned initializer under `tools/capture/tclscripts/capAutoLoad`; it does not alter project files.

The worker uses the paired `pythonw.exe` on Windows and communicates through temporary JSON response files. Search, preview and import therefore run asynchronously without a console window.

See the repository [README](../README.md) and [Windows guide](../docs/windows_user_guide_zh.md) for the complete workflow and source-data policy.
