# Release packaging

Always publish a full, normal GitHub release. Never publish a delta patch or
an executable-only update as the release package, including for small version
increments such as 1.1.2. Each release must support a fresh installation without
downloading an earlier release.

Use `tools/pdx_asset_installer/package_core.py` to package:

- `default.xbe`
- `PerfectDarkXAssetInstaller.exe`
- `ext_tex.pak`
- `TitleImage.xbx`, `SaveImage.xbx`, and `TitleMeta.xbx`
- `box art.png`
- `readme.txt`
- `LICENSE`
- `SHA256SUMS.txt`

The user supplies their own supported ROM to the included installer. Do not
include a ROM, extracted original game assets, a test XISO, or test logs in the
release ZIP. Validate the complete archive and checksums, exercise the installer
backend and the exact release XBE, and state any unverified behavior accurately.

Use the normal source build configuration unless a diagnostic release is
explicitly requested. A normal build has `PD_XBOX_ISSUE3_DIAGNOSTIC`,
`PD_XBOX_PRESENT_TRACE`, `PD_XBOX_QUALIFICATION_CONFIG`, and other test/stress
options disabled. Update `port/include/pdx_version.h`, the main README, and
`tools/pdx_asset_installer/release_readme.txt` for the new version.
