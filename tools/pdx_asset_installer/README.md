# Perfect Dark X Asset Installer

The release ZIP contains the Xbox executable, this Windows GUI, and the diffuse
texture pack. It contains no N64 ROM and no files extracted from one.

The installer performs three ordered stages:

1. Validate the user's legally owned NTSC-final/US v1.1 `.z64` ROM by size and
   MD5.
2. Extract `files/`, `segs/`, and `filenames.lst` locally and install the
   release `default.xbe` plus its dashboard/save metadata. Xbox defaults are
   compiled into the executable.
3. Validate the bundled `ext_tex.pak` and copy it unchanged into the generated
   Xbox folder.

Installation is transactional. Work is written to a temporary sibling folder,
and the selected output folder is replaced only after every stage succeeds.
Cancellation or failure removes the temporary folder. The selected output must
be new or empty.

## User workflow

1. Download and extract the Perfect Dark X release ZIP on a Windows PC.
2. Place your own NTSC-final/US v1.1 `.z64` ROM beside
   `PerfectDarkXAssetInstaller.exe`. The required MD5 is
   `e03b088b6ac9e0080440efed07c1e40f`.
3. Run `PerfectDarkXAssetInstaller.exe`. The GUI automatically detects the XBE,
   texture pack, and a single `.z64` file beside it.
4. Click **Install**. The default output is the new `Perfect Dark X`
   folder inside the extracted download folder. Enable the local-XISO checkbox
   only if XEMU or the target Xbox loader needs an image instead of a folder.
5. Wait for the progress bar to reach 100%. The source ROM remains untouched
   outside the generated Xbox folder and is never copied into it.
6. FTP the entire generated `Perfect Dark X` folder to the Xbox and
   launch `default.xbe`. If the
   checkbox was enabled, the installer also creates the asset-bearing XISO
   locally beside the folder; that generated image is never part of the clean
   release download.

The ROM and texture-pack paths are never copied into the installation manifest;
only checksums and installed file names are recorded.

## Running from source

Python 3 with Tk support is sufficient:

```powershell
python tools/pdx_asset_installer/installer.py
```

Check imports without opening a window:

```powershell
python tools/pdx_asset_installer/installer.py --self-test
```

## Building the Windows GUI

Install PyInstaller, then run:

```powershell
python -m pip install pyinstaller
powershell -ExecutionPolicy Bypass `
  -File tools/pdx_asset_installer/build_windows.ps1
```

The standalone executable is written to
`tools/pdx_asset_installer/dist/PerfectDarkXAssetInstaller.exe` by default.
Build output is ignored and must not be committed.

## Backend API

`backend.install_game` is independent of Tk and accepts a progress callback and
cancellation event. This allows the extraction and validation path to be tested
without opening or focusing the GUI.
