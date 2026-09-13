PERFECT DARK X v1.1.1 - DIAGNOSTIC RELEASE

This build collects evidence for issue #3: the moving horizontal colored band,
confirmed in 480i. It is not a confirmed fix. PAL specificity is unconfirmed.

UPGRADING FROM v1.1

Back up your original default.xbe, then replace only that file with the one
from this ZIP. Keep your existing assets and saves. Restore the old executable
when you have finished testing if desired.

PLEASE UPLOAD LOGS

Use a writable hard-drive installation. Play normally, particularly scenes
where you previously saw the band. Note the approximate time since launch
when it appears and take a photo or video showing the whole screen.

Return to your dashboard and copy pd.log, pd.startup.log, and pd.previous.log
(if present) from the game folder BEFORE launching the game again. A new
launch overwrites logs. Zip them and attach them to a comment at:
https://github.com/GTTeancum/Perfect-Dark-X/issues/3

Include Xbox revision, RAM, BIOS/dashboard versions, video region and mode,
cable/adapter, display, mission/location, session duration, and approximate
time of the occurrence. Photos/video are especially useful. Clean runs are
useful too: state the mode and duration. After a freeze, collect logs from the
dashboard before relaunching; the last buffered records may be missing.

Logs retain approximately 16 MiB of recent history plus startup output.
If writing in the game folder fails, logging falls back to the title's T:
location. Prefer a writable hard-drive install for straightforward collection.
Logging can affect timing. This build preserves original presentation behavior;
it does not enable the experimental presentation fixes or automated input.

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
