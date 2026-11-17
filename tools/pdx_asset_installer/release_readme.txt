PERFECT DARK X v1.1.3 - FULL RELEASE

This normal release fixes SD Spy Cam corruption, rapid-menu GPU crashes,
and missing training statistics (issues #4, #5, and #6). EyeSpy speed/height
and mission-end accuracy formatting are also repaired. Menu input speed is
unchanged; no input throttle or extra GPU flush is added. Normal presentation
safeguards remain enabled, qualification input/tracing is disabled, and
standard pd.log output remains available. Version 1.1.3 appears on startup.

KNOWN LIMITATION

Some SD menu backgrounds remain black in hardware test fixtures even when
the foreground model is visible. This background-compositing defect remains
unresolved. Targeted XEMU and retail 64 MiB Xbox checks do not constitute
complete visual, audio, controller, save-persistence, or campaign testing.
See docs/open-issue-repairs.md in the source repository for the test report.

COMPLETE DISTRIBUTION

This ZIP includes the game executable, asset installer, texture pack,
dashboard artwork/metadata, instructions, license, and checksums. You do not
need an older release. Supply your own supported ROM as described below.
Existing v1.0, v1.1, v1.1.1, and v1.1.2 assets and saves remain compatible.

REPORTING A PROBLEM

If a problem occurs, open a new issue at:
https://github.com/GTTeancum/Perfect-Dark-X/issues/new

Include version 1.1.3, Xbox revision and RAM, BIOS/dashboard, video mode and
aspect ratio, cable/display, mission/location, and time since launch. Copy
the current pd.log before launching the game again and attach it to the issue.
Older pd.startup.log or pd.previous.log files left by v1.1.1 may belong to a
previous diagnostic run; this release does not update those diagnostic files.

PERFECT DARK X - INSTALLATION

1. Extract this ZIP to a folder on a Windows PC.
2. Place your legally owned Perfect Dark NTSC-final/US v1.1 .z64 ROM beside
   PerfectDarkXAssetInstaller.exe.
3. Run PerfectDarkXAssetInstaller.exe and click Install. The installer verifies
   the ROM and creates a complete "Perfect Dark X" folder. The source ROM is
   not copied into that folder.
4. FTP the entire generated "Perfect Dark X" folder to a games directory on
   your Xbox, then launch default.xbe. Configure widescreen and progressive-scan
   support in the Xbox dashboard before starting the game.

For XEMU or a loader that requires an image, enable the installer's local-XISO
option. The XISO is created only on your PC and is not part of this download.

PROJECT PAGE

https://github.com/GTTeancum/Perfect-Dark-X

CREDITS

Perfect Dark X contributors.
Perfect Dark PC port and Perfect Dark N64 decompilation contributors.
sm64-port and libultraship Fast3D contributors.
nxdk, pbkit, XboxDev, and XEMU contributors.

See LICENSE for software licensing. Perfect Dark and its game assets belong to
their respective rights holders. This project is not affiliated with or
endorsed by those rights holders.
