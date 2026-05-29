// Xbox entry point with phase-debug instrumentation for XEMU testing.
//
// Each major init step calls dbgPhase() which:
//   - writes to D:\pd.log (always)
//   - renders to screen via debugPrint (before pbkit) or pb_print (after)
//   - pauses DBG_PHASE_PAUSE_MS ms so screenshots can capture each step
//
// Post a screenshot per phase to report results; check pd.log for details.

#include <stdlib.h>
#include <stdio.h>
#include <PR/ultratypes.h>
#include <PR/ultrasched.h>
#include <PR/os_message.h>

#include <xboxkrnl/xboxkrnl.h>
#include <hal/xbox.h>
#include <hal/debug.h>
#include <hal/video.h>

#include "lib/main.h"
#include "bss.h"
#include "data.h"

#include "video.h"
#include "audio.h"
#include "input.h"
#include "fs.h"
#include "romdata.h"
#include "config.h"
#include "mod.h"
#include "system.h"
#include "utils.h"
#include "debug_xbox.h"

// ── Globals ───────────────────────────────────────────────────────────────────

u32 g_OsMemSize    = 0;
s32 g_OsMemSizeMb  = 16;
u8  g_Is4Mb        = 0;
s8  g_Resetting    = 0;

OSSched g_Sched;
OSMesgQueue g_MainMesgQueue;
OSMesg g_MainMesgBuf[32];

u8  *g_MempHeap     = NULL;
u32  g_MempHeapSize = 0;

u32 g_VmNumTlbMisses    = 0;
u32 g_VmNumPageMisses   = 0;
u32 g_VmNumPageReplaces = 0;
u8  g_VmShowStats       = 0;

s32 g_TickRateDiv    = 1;
s32 g_TickExtraSleep = 1;
s32 g_SkipIntro      = 0;
s32 g_FileAutoSelect = -1;

extern s32 g_StageNum;

// ── Scheduler bootstrap ───────────────────────────────────────────────────────

s32 bootGetMemSize(void)
{
    return (s32)g_OsMemSize;
}

void *bootAllocateStack(s32 threadid, s32 size)
{
    static u8 bruh[0x1000];
    (void)threadid; (void)size;
    return bruh;
}

void bootCreateSched(void)
{
    osCreateMesgQueue(&g_MainMesgQueue, g_MainMesgBuf, ARRAYCOUNT(g_MainMesgBuf));
    osCreateScheduler(&g_Sched, NULL, OS_VI_NTSC_LAN1, 1);
}

// ── Game init ─────────────────────────────────────────────────────────────────

static void gameInit(void)
{
    osMemSize = g_OsMemSizeMb * 1024 * 1024;

    for (s32 i = 0; i < MAX_PLAYERS; ++i) {
        struct extplayerconfig *cfg = g_PlayerExtCfg + i;
        cfg->fovzoommult = cfg->fovzoom ? cfg->fovy / 60.0f : 1.0f;
    }

    if (g_HudCenter == HUDCENTER_NORMAL) {
        g_HudAlignModeL = G_ASPECT_CENTER_EXT;
        g_HudAlignModeR = G_ASPECT_CENTER_EXT;
    } else if (g_HudCenter == HUDCENTER_WIDE) {
        g_HudAlignModeL = G_ASPECT_LEFT_EXT | G_ASPECT_WIDE_EXT;
        g_HudAlignModeR = G_ASPECT_RIGHT_EXT | G_ASPECT_WIDE_EXT;
    }
}

static void cleanup(void)
{
    sysLogPrintf(LOG_NOTE, "shutdown");
    inputSaveBinds();
    configSave(CONFIG_PATH);
    videoShutdown();
    crashShutdown();
}

// ── Xbox entry point ──────────────────────────────────────────────────────────
// NXDK's CRT provides the real PE entry (WinMainCRTStartup): it runs CRT
// initialisation and global constructors (.init_array / PD_CONSTRUCTOR) and
// then calls main().  We MUST NOT override /entry — doing so skips CRT init
// and global ctors, leaving uninitialised state that faults in kernel space.
// Match the signature nxdk's CRT calls (confirmed against a working nxdk app).

// One-line-at-a-time status helper: clears the screen and prints a fresh
// single status line so the framebuffer never accumulates.  The serial/UART
// channel (View->Debug->Serial in XEMU) receives all lines regardless.
#define BOOT_PRINT(msg) do { \
    debugClearScreen(); \
    debugPrint("PD-X: " msg "\n"); \
} while(0)

void __cdecl main(void)
{
    // Initialise a HAL framebuffer up front so debugPrint/BOOT_PRINT and any
    // early sysFatalError message are actually visible.  pbkit (videoInit)
    // reinitialises the display later; this is just for boot diagnostics.
    XVideoSetMode(640, 480, 32, REFRESH_DEFAULT);

    BOOT_PRINT("entry");

    BOOT_PRINT("sysInitArgs...");
    sysInitArgs(0, NULL);
    BOOT_PRINT("sysInitArgs OK");

    BOOT_PRINT("crashInit...");
    crashInit();
    BOOT_PRINT("crashInit OK");

    BOOT_PRINT("sysInit...");
    sysInit();
    BOOT_PRINT("sysInit OK");
    dbgPhase(DBG_PHASE_SYS_INIT, "sysInit OK - timer+log running");

    BOOT_PRINT("fsInit...");
    fsInit();
    BOOT_PRINT("fsInit OK");
    dbgPhase(DBG_PHASE_FS_INIT, "fsInit OK - D:\\ accessible");

    BOOT_PRINT("configInit...");
    configInit();
    BOOT_PRINT("configInit OK");
    dbgPhase(DBG_PHASE_CFG_INIT, "configInit OK");

    // ── Phase 5: video (pbkit + NV2A) ─────────────────────────────────────────
    // After this call pbkit is up; dbgPhase switches to pb_print output.
    BOOT_PRINT("videoInit...");
    videoInit();
    BOOT_PRINT("videoInit OK");
    dbgPhase(DBG_PHASE_VIDEO_INIT, "videoInit OK - NV2A online");

    BOOT_PRINT("inputInit...");
    inputInit();
    BOOT_PRINT("inputInit OK");
    dbgPhase(DBG_PHASE_INPUT_INIT, "inputInit OK - SDL gamepad ready");

    BOOT_PRINT("audioInit...");
    audioInit();
    BOOT_PRINT("audioInit OK");
    dbgPhase(DBG_PHASE_AUDIO_INIT, "audioInit OK - SDL audio device open");

    // ── Phase 8: ROM load ─────────────────────────────────────────────────────
    BOOT_PRINT("romdataInit...");
    romdataInit();
    BOOT_PRINT("romdataInit OK");
    dbgPhase(DBG_PHASE_ROM_LOAD, "romdataInit OK - ROM loaded");

    g_ValidGbcRomFound = romdataCheckGbcRom();

    BOOT_PRINT("gameInit...");
    gameInit();
    BOOT_PRINT("gameInit OK");

    if (fsGetModDir()) {
        modConfigLoad(MOD_CONFIG_FNAME);
    }

    atexit(cleanup);

    BOOT_PRINT("bootCreateSched...");
    bootCreateSched();
    BOOT_PRINT("bootCreateSched OK");

    g_OsMemSize    = osGetMemSize();
    g_MempHeapSize = g_OsMemSize;

    BOOT_PRINT("heap alloc...");
    g_MempHeap = sysMemZeroAlloc(g_MempHeapSize);
    if (!g_MempHeap) {
        sysFatalError("Could not alloc %u bytes for memp heap.\n"
                      "Xbox has 64 MB; reduce Game.MemorySize in pd.ini.", g_MempHeapSize);
    }
    BOOT_PRINT("heap OK");

    sysLogPrintf(LOG_NOTE, "memp heap at %p (%u MB)", g_MempHeap, g_MempHeapSize / (1024*1024));
    sysLogPrintf(LOG_NOTE, "rom  file at %p (%u MB)", g_RomFile,  g_RomFileSize  / (1024*1024));

    g_StageNum = STAGE_TITLE;

    dbgPhase(DBG_PHASE_GAME_INIT, "gameInit+heap OK");
    dbgPhase(DBG_PHASE_SCHED, "bootCreateSched OK");

    BOOT_PRINT("entering mainProc...");
    dbgPhase(DBG_PHASE_MAIN_PROC, "entering mainProc()");

    mainProc();

    XReboot();
}

#undef BOOT_PRINT

// ── Config registrations ──────────────────────────────────────────────────────

PD_CONSTRUCTOR static void gameConfigInit(void)
{
    configRegisterInt("Game.MemorySize",             &g_OsMemSizeMb,   4,   2048);
    configRegisterInt("Game.CenterHUD",              &g_HudCenter,     0,   2);
    configRegisterInt("Game.MenuMouseControl",        &g_MenuMouseControl, 0, 1);
    configRegisterFloat("Game.ScreenShakeIntensity", &g_ViShakeIntensityMult, 0.f, 10.f);
    configRegisterInt("Game.TickRateDivisor",        &g_TickRateDiv,   0,   10);
    configRegisterInt("Game.ExtraSleep",             &g_TickExtraSleep, 0,  1);
    configRegisterInt("Game.SkipIntro",              &g_SkipIntro,     0,   1);
    configRegisterInt("Game.DisableMpDeathMusic",    &g_MusicDisableMpDeath, 0, 1);
    configRegisterInt("Game.GEMuzzleFlashes",        &g_BgunGeMuzzleFlashes, 0, 1);
    configRegisterInt("Game.MaxExplosions",          &g_MaxExplosions, 6,   96);
    for (s32 j = 0; j < MAX_PLAYERS; ++j) {
        const s32 i = j + 1;
        configRegisterFloat(strFmt("Game.Player%d.FovY", i),              &g_PlayerExtCfg[j].fovy,              5.f,   175.f);
        configRegisterInt(  strFmt("Game.Player%d.FovAffectsZoom", i),    &g_PlayerExtCfg[j].fovzoom,           0,     1);
        configRegisterInt(  strFmt("Game.Player%d.MouseAimMode", i),      &g_PlayerExtCfg[j].mouseaimmode,      0,     1);
        configRegisterFloat(strFmt("Game.Player%d.MouseAimSpeedX", i),    &g_PlayerExtCfg[j].mouseaimspeedx,    0.f,   10.f);
        configRegisterFloat(strFmt("Game.Player%d.MouseAimSpeedY", i),    &g_PlayerExtCfg[j].mouseaimspeedy,    0.f,   10.f);
        configRegisterFloat(strFmt("Game.Player%d.RadialMenuSpeed", i),   &g_PlayerExtCfg[j].radialmenuspeed,   0.f,   10.f);
        configRegisterFloat(strFmt("Game.Player%d.CrosshairSway", i),     &g_PlayerExtCfg[j].crosshairsway,     0.f,   10.f);
        configRegisterFloat(strFmt("Game.Player%d.CrosshairEdgeBoundary", i), &g_PlayerExtCfg[j].crosshairedgeboundary, 0.f, 1.f);
        configRegisterInt(  strFmt("Game.Player%d.CrouchMode", i),        &g_PlayerExtCfg[j].crouchmode,        0,     CROUCHMODE_TOGGLE_ANALOG);
        configRegisterInt(  strFmt("Game.Player%d.ExtendedControls", i),  &g_PlayerExtCfg[j].extcontrols,       0,     1);
        configRegisterUInt( strFmt("Game.Player%d.CrosshairColour", i),   &g_PlayerExtCfg[j].crosshaircolour,   0,     0xFFFFFFFF);
        configRegisterUInt( strFmt("Game.Player%d.CrosshairSize", i),     &g_PlayerExtCfg[j].crosshairsize,     0,     4);
        configRegisterInt(  strFmt("Game.Player%d.CrosshairHealth", i),   &g_PlayerExtCfg[j].crosshairhealth,   0,     CROSSHAIR_HEALTH_ON_WHITE);
        configRegisterInt(  strFmt("Game.Player%d.UseKeyReloads", i),     &g_PlayerExtCfg[j].usereloads,        0,     0);
    }
}
