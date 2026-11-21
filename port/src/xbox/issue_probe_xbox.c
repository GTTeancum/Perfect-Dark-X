/* Deterministic in-game regression fixtures. Never enabled in release builds.
 * No OS input, save progression, or substitute gameplay is used as evidence.
 */
#ifdef PD_XBOX_QUALIFICATION_CONFIG
#include <ultra64.h>
#include <stdio.h>
#include <stdarg.h>
#include <string.h>
#include "platform.h"
#include "config.h"
#include "system.h"
#include "constants.h"
#include "data.h"
#include "types.h"
#include "bss.h"
#include "game/menu.h"
#include "game/mainmenu.h"
#include "game/bondgun.h"
#include "game/hudmsg.h"
#include "game/inv.h"
#include "game/options.h"
#include "game/mplayer/setup.h"
#include "lib/memp.h"
#include "game/training.h"
#include "issue_probe_xbox.h"
#include "serial_xbox.h"
#include <pbkit/pbkit.h>
#include <xboxkrnl/xboxkrnl.h>
#include "../../fast3d/gfx_nv2a.h"

extern struct menudialogdef g_MpPlayerStatsMenuDialog;
extern struct menudialogdef g_CiControlOptionsMenuDialog;
extern struct menudialogdef g_SoloMissionEndscreenFailedMenuDialog;
extern struct menudialogdef g_PakNotOriginalMenuDialog, g_FilemgrSaveElsewhereMenuDialog;
extern MenuItemHandlerResult filemgrReinsertedCancelMenuHandler(s32, struct menuitem *, union handlerdata *);
extern MenuItemHandlerResult filemgrCancelSave2MenuHandler(s32, struct menuitem *, union handlerdata *);
extern char *frMenuTextTimeTakenValue(struct menuitem *item);
extern char *frMenuTextAccuracyValue(struct menuitem *item);
extern char *dtMenuTextTimeTakenValue(struct menuitem *item);
extern char *htMenuTextTimeTakenValue(struct menuitem *item);
extern char *endscreenMenuTextAccuracy(struct menuitem *item);
static s32 scenario, reserve_mib, menu_probe_mib, fatal_after_lens;
static void *memory_reserve;
static bool menu_probe_done, blur_requested, zoom_requested;
static unsigned ticks;
static unsigned swipes;
static bool spy_requested;
static u64 input_started;
static s32 input_phase = -1;
static unsigned input_polls, input_edges, input_reversals, menu_swipes, opening_swipes;
static unsigned phase_swipes, phase_edges, phase_reversals;
static u64 previous_poll, maximum_poll_gap;
static s32 previous_direction;
static bool input_finished;
static unsigned menu_setup_tick;
static struct menudialogdef *expected_dialogs[8];
static unsigned expected_dialog_count, unexpected_swipes;
static s32 force_sd, force_wide, capture_after_ms;
static u64 capture_started;
static bool captured;
static u64 pause_input_started;
static bool pause_verified;
static unsigned pause_cycles, unpause_cycles;
static s32 last_pausemode = -1;
static u64 spy_ready_time;
extern volatile u32 g_SPXBFramebufferData, g_SPXBFramebufferPitch;
extern volatile u32 g_SPXBFramebufferWidth, g_SPXBFramebufferHeight;

static void probeLog(const char *fmt, ...)
{
    char line[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);
    /* Preserve qualification results on physical consoles without COM1. */
    sysLogPrintf(LOG_NOTE, "%s", line);
    serialPuts(line);
    serialPutc('\n');
}

s32 pdxIssueProbeInputEnabled(void)
{
    return (scenario >= 5 && scenario <= 7) || scenario == 9 || scenario == 10 || scenario == 14;
}

s32 pdxIssueProbeForceSd(void)
{
    return force_sd;
}

s32 pdxIssueProbeForceWide(void)
{
    return force_wide;
}

static void captureCompletedFrame(void)
{
    u32 width = g_SPXBFramebufferWidth, height = g_SPXBFramebufferHeight;
    u32 pitch = g_SPXBFramebufferPitch;
    const u8 *pixels = (const u8 *)g_SPXBFramebufferData;
    if (!pixels || width > 1920 || !width || !height || height > 1080 || pitch < width * 4) return;
    FILE *file = fopen("D:\\pdx-qa-frame.ppm", "wb");
    if (!file) return;
    bool ok = fprintf(file, "P6\n%u %u\n255\n", width, height) > 0;
    u8 row[1920 * 3];
    for (u32 y = 0; y < height && ok; ++y) {
        const u8 *src = pixels + y * pitch;
        for (u32 x = 0; x < width; ++x) {
            row[x * 3] = src[x * 4 + 2];
            row[x * 3 + 1] = src[x * 4 + 1];
            row[x * 3 + 2] = src[x * 4];
        }
        ok = fwrite(row, 3, width, file) == width;
    }
    ok = fclose(file) == 0 && ok;
    captured = true;
    probeLog("PDX_CAPTURE written=%d scenario=%d width=%u height=%u ticks=%u", ok, scenario, width, height, ticks);
}

/* Inject only into this Xbox process, before libultra's controller sample
 * queue. Normal edge detection, menu repeat and dialog logic consume it. */
s32 pdxIssueProbeReadPad(s32 idx, OSContPad *pad)
{
    if (idx != 0) return -1;
    if (scenario == 14) {
        if (zoom_requested) pad->button = R_TRIG;
        return 0;
    }
    if (scenario == 9 || scenario == 10) {
        if (scenario == 10 && pause_input_started) {
            u64 elapsed = sysGetMicroseconds() - pause_input_started;
            if (elapsed < 48100000 && elapsed % 6000000 < 100000)
                pad->button = START_BUTTON;
            return 0;
        }
        if (pause_input_started && sysGetMicroseconds() - pause_input_started < 100000)
            pad->button = START_BUTTON;
        return 0;
    }
    if (!input_started || input_finished) return 0;
    u64 now = sysGetMicroseconds();
    u32 elapsed = (u32)(now - input_started);
    s32 phase = elapsed / 8000000;
    if (phase >= 5) return 0;
    if (phase != input_phase) {
        if (input_phase >= 0) {
            probeLog("PDX_INPUT phase-end phase=%d edges=%u reversals=%u swipes=%u",
                     input_phase, input_edges - phase_edges,
                     input_reversals - phase_reversals, menu_swipes - phase_swipes);
        }
        input_phase = phase;
        phase_edges = input_edges;
        phase_reversals = input_reversals;
        phase_swipes = menu_swipes;
        probeLog("PDX_INPUT phase=%d elapsed_ms=%u", phase, elapsed / 1000);
    }
    if (previous_poll && now - previous_poll > maximum_poll_gap)
        maximum_poll_gap = now - previous_poll;
    previous_poll = now;
    ++input_polls;
    u32 local = elapsed % 8000000;
    s32 direction;
    if (phase <= 1) {
        /* 10 or 20 alternating taps/second, each 50% pressed, 50% neutral. */
        u32 period = phase == 0 ? 100000 : 50000;
        direction = local % period < period / 2 ? ((local / period) % 2 ? -1 : 1) : 0;
    } else if (phase == 2) {
        /* Full stick reversal every 50 ms, including normal analog repeat. */
        direction = (local / 50000) % 2 ? -1 : 1;
        pad->stick_x = direction * 80;
    } else if (phase == 3) {
        /* Fastest observable digital reversal: every controller poll. */
        direction = input_polls % 2 ? -1 : 1;
    } else {
        /* Two-second holds exercise both slow and accelerated key repeat. */
        direction = (local / 2000000) % 2 ? -1 : 1;
    }
    if (phase != 2 && direction)
        pad->button = direction < 0 ? L_CBUTTONS : R_CBUTTONS;
    if (direction && direction != previous_direction) ++input_edges;
    if (direction && previous_direction && direction != previous_direction) ++input_reversals;
    previous_direction = direction;
    return 0;
}

void pdxIssueProbeMenuSwipe(s32 direction, s32 outgoingstate)
{
    if (!pdxIssueProbeInputEnabled() || !input_started || input_finished) return;
    struct menudialogdef *actual = g_Menus[g_MpPlayerNum].curdialog->definition;
    bool expected = false;
    for (unsigned i = 0; i < expected_dialog_count; ++i)
        if (actual == expected_dialogs[i]) expected = true;
    if (!expected) {
        if (++unexpected_swipes == 1)
            probeLog("PDX_INPUT unexpected-dialog definition=%p", actual);
    }
    ++menu_swipes;
    if (outgoingstate < MENUDIALOGSTATE_POPULATED) ++opening_swipes;
    if (menu_swipes <= 8 || menu_swipes % 100 == 0)
        probeLog("PDX_INPUT swipe=%u direction=%d outgoing_state=%d elapsed_ms=%u",
                 menu_swipes, direction, outgoingstate,
                 (unsigned)((sysGetMicroseconds() - input_started) / 1000));
}

static unsigned checkText(const char *label, const char *actual, const char *expected)
{
    bool pass = strcmp(actual, expected) == 0;
    probeLog("PDX_ISSUE text %s %s actual=[%s] expected=[%s]",
             label, pass ? "PASS" : "FAIL", actual, expected);
    return !pass;
}

static void testTrainingText(void)
{
    struct frdata *fr = frGetData();
    unsigned failures = 0;
    char hud[128];
    failures += checkText("time", frMenuTextTimeTakenValue(NULL), PAL ? "10.26s\n" : "10.25s\n");
    failures += checkText("accuracy", frMenuTextAccuracyValue(NULL), "83.3%\n");
    frGetAccuracy(hud);
    failures += checkText("hud-accuracy", hud, "83.33%\n");
    fr->timetaken = 0;
    failures += checkText("zero-time", frMenuTextTimeTakenValue(NULL), "0.00s\n");
    fr->timetaken = PAL ? -25 : -30;
    failures += checkText("negative-time", frMenuTextTimeTakenValue(NULL), "-0.50s\n");
    fr->timetaken = PAL ? 3125 : 3750;
    failures += checkText("minute-time", frMenuTextTimeTakenValue(NULL), "1m  3s\n");
    fr->timetaken = (PAL ? 50 : 60) * 120 - 1;
    if (checkText("minute-rollover", frMenuTextTimeTakenValue(NULL), "2m  0s\n"))
        sysFatalError("Training minute rollover check failed");
    fr->timetaken = PAL ? 10000 : 12000;
    failures += checkText("time-limit", frMenuTextTimeTakenValue(NULL), "2m  0s\n");
    fr->timetaken = PAL ? 513 : 615;
    fr->numshots = 0;
    failures += checkText("zero-shots", frMenuTextAccuracyValue(NULL), "0.0%\n");
    fr->numshots = 1;
    failures += checkText("capped-accuracy", frMenuTextAccuracyValue(NULL), "100.0%\n");
    fr->numshots = 12;
    struct trainingdata *device = dtGetData();
    struct trainingdata *holo = getHoloTrainingData();
    s32 device_time = device->timetaken;
    s32 holo_time = holo->timetaken;
    device->timetaken = holo->timetaken = PAL ? 525 : 630;
    failures += checkText("device-time", dtMenuTextTimeTakenValue(NULL), "10.50s\n");
    failures += checkText("holo-time", htMenuTextTimeTakenValue(NULL), "10.50s\n");
    device->timetaken = device_time;
    holo->timetaken = holo_time;
    probeLog("PDX_ISSUE text-tests checks=11 failures=%u", failures);
    s32 saved_shots[7];
    memcpy(saved_shots, g_Vars.currentplayerstats->shotcount, sizeof(saved_shots));
    memset(g_Vars.currentplayerstats->shotcount, 0, sizeof(saved_shots));
    unsigned endscreen_failures = checkText("mission-zero-shots", endscreenMenuTextAccuracy(NULL), "0.0%");
    g_Vars.currentplayerstats->shotcount[SHOTREGION_TOTAL] = 12;
    g_Vars.currentplayerstats->shotcount[SHOTREGION_HEAD] = 10;
    endscreen_failures += checkText("mission-accuracy", endscreenMenuTextAccuracy(NULL), "83.3%");
    g_Vars.currentplayerstats->shotcount[SHOTREGION_TOTAL] = 1;
    endscreen_failures += checkText("mission-capped-accuracy", endscreenMenuTextAccuracy(NULL), "100.0%");
    memcpy(g_Vars.currentplayerstats->shotcount, saved_shots, sizeof(saved_shots));
    probeLog("PDX_ISSUE mission-text-tests checks=3 failures=%u", endscreen_failures);
}

PD_CONSTRUCTOR static void issueProbeConfig(void)
{
    configRegisterInt("Game.IssueFatalAfterLens", &fatal_after_lens, 0, 1);
    configRegisterInt("Game.IssueMenuProbeMiB", &menu_probe_mib, 0, 4);
    configRegisterInt("Game.IssueReserveMiB", &reserve_mib, 0, 4);
    configRegisterInt("Game.IssueProbe", &scenario, 0, 14);
    configRegisterInt("Game.IssueForceSD", &force_sd, 0, 1);
    configRegisterInt("Game.IssueForceWide", &force_wide, 0, 1);
    configRegisterInt("Game.IssueCaptureAfterMs", &capture_after_ms, 0, 150000);
}

void pdxIssueProbeTick(void)
{
    if (!capture_started) capture_started = sysGetMicroseconds();
    if (scenario == 11) {
        // Capture inside the original Horizon Scanner sequence in the Air Base intro.
        if (!captured && g_Vars.stagenum == STAGE_AIRBASE && g_InCutscene &&
                g_CutsceneAnimNum == ANIM_CUT_CAVE_INTRO_CAM &&
                g_CutsceneCurAnimFrame60 >= 960 && g_CutsceneCurAnimFrame60 < 1400) {
            probeLog("PDX_SCANNER intro frame=%d", g_CutsceneCurAnimFrame60);
            captureCompletedFrame();
        }
    } else if (capture_after_ms && !captured && sysGetMicroseconds() - capture_started >= (u64)capture_after_ms * 1000) {
        captureCompletedFrame();
    }
    if (!memory_reserve && reserve_mib && g_Vars.stagenum < STAGE_TITLE) {
        // Keep real memory occupied while the stage, lens and menu coexist.
        // The Xbox kernel reclaims this fixture allocation at title teardown.
        size_t bytes = (size_t)reserve_mib * 1024u * 1024u;
        memory_reserve = MmAllocateContiguousMemory(bytes);
        if (!memory_reserve) sysFatalError("Qualification reserve allocation failed");
        memset(memory_reserve, 0xa5, bytes);
        probeLog("PDX_RESERVE held bytes=%u", (unsigned)bytes);
    }
    if (!scenario || g_Vars.stagenum >= STAGE_TITLE) return;
    ++ticks;
    if ((scenario == 3 || scenario == 9 || scenario == 10 || scenario == 12 || scenario == 14) && g_Menus[g_MpPlayerNum].curdialog) {
        // Direct BootStage skips agent-file selection. Cancel only its known
        // missing-file/save-elsewhere prompts through the original handlers;
        // this fixture does not test saving or change a player's save files.
        struct menudialogdef *def = g_Menus[g_MpPlayerNum].curdialog->definition;
        if (def == &g_PakNotOriginalMenuDialog) {
            probeLog("PDX_ISSUE fixture cancel missing-agent-save");
            filemgrReinsertedCancelMenuHandler(MENUOP_SET, NULL, NULL);
        } else if (def == &g_FilemgrSaveElsewhereMenuDialog) {
            probeLog("PDX_ISSUE fixture cancel save-elsewhere");
            filemgrCancelSave2MenuHandler(MENUOP_SET, NULL, NULL);
        }
    }
    if (scenario == 14) {
        if (!zoom_requested && !g_InCutscene && ticks > 180 && !g_Menus[g_MpPlayerNum].curdialog) {
            invGiveSingleWeapon(WEAPON_SNIPERRIFLE);
            bgunEquipWeapon2(HAND_RIGHT, WEAPON_SNIPERRIFLE);
            optionsSetShowZoomRange(g_Vars.currentplayerstats->mpindex, true);
            zoom_requested = true;
        }
        if (!captured && zoom_requested && hudmsgIsZoomRangeVisible()) {
            static unsigned visible_ticks;
            if (++visible_ticks == 60) {
                captureCompletedFrame();
                probeLog("PDX_ISSUE zoom HUD visible");
            }
        }
        return;
    }
    if (scenario == 12) {
        if (!blur_requested && !g_InCutscene && ticks > 180 && !g_Menus[g_MpPlayerNum].curdialog
                && g_Vars.currentplayer->prop && g_Vars.currentplayer->prop->chr) {
            g_Vars.currentplayer->prop->chr->blurdrugamount = TICKS(3000);
            blur_requested = true;
            probeLog("PDX_ISSUE gameplay motion blur requested");
        }
        return;
    }
    if (scenario == 9 || scenario == 10) {
        if (scenario == 10 && !g_InCutscene && g_Vars.currentplayer->eyespy) {
            if (!spy_requested) {
                g_Vars.currentplayer->devicesactive |= DEVICE_EYESPY;
                spy_requested = true;
                probeLog("PDX_PAUSE eyespy requested before menu cycles");
            }
            if (!spy_ready_time && g_Vars.currentplayer->eyespy->active)
                spy_ready_time = sysGetMicroseconds();
        }
        // Exercise the ordinary Start -> playerPause -> menu path. Never push
        // a dialog or set pause state here; original game code must do both.
        if (!pause_input_started && ticks >= 600 && !g_InCutscene
                && !g_Menus[g_MpPlayerNum].curdialog
                && g_Vars.currentplayer->pausemode == PAUSEMODE_UNPAUSED
                && (scenario == 9 || (spy_ready_time && sysGetMicroseconds() - spy_ready_time >= 6000000))) {
            pause_input_started = sysGetMicroseconds();
            probeLog("PDX_PAUSE Start pulse begin");
        }
        if (pause_input_started && !pause_verified
                && sysGetMicroseconds() - pause_input_started >= 2000000
                && g_Vars.currentplayer->pausemode == PAUSEMODE_PAUSED
                && g_Menus[g_MpPlayerNum].curdialog && g_MenuData.bg == MENUBG_BLUR) {
            pause_verified = true;
            probeLog("PDX_PAUSE ordinary path complete root=%d bg=%d", g_MenuData.root, g_MenuData.bg);
        }
        if (scenario == 10 && pause_input_started) {
            s32 mode = g_Vars.currentplayer->pausemode;
            if (mode != last_pausemode) {
                if (mode == PAUSEMODE_PAUSED) ++pause_cycles;
                if (mode == PAUSEMODE_UNPAUSED && pause_cycles) ++unpause_cycles;
                last_pausemode = mode;
                probeLog("PDX_PAUSE cycles=%u resumes=%u mode=%d", pause_cycles, unpause_cycles, mode);
            }
        }
        if (pause_cycles == 5 && !menu_probe_done && menu_probe_mib &&
                sysGetMicroseconds() - pause_input_started > 52000000) {
            menu_probe_done = true;
            for (s32 mib = menu_probe_mib; mib > 0; --mib) {
                size_t bytes = (size_t)mib * 1024u * 1024u;
                void *block = MmAllocateContiguousMemory(bytes);
                probeLog("PDX_MENU_MEMORY contiguous_bytes=%u success=%d stage_free=%u",
                         (unsigned)bytes, block != NULL, mempGetStageFree());
                if (block) {
                    memset(block, 0x5a, bytes);
                    MmFreeContiguousMemory(block);
                    break;
                }
            }
        }
        return;
    }
    if (scenario == 13 && menu_setup_tick && !captured && ticks >= menu_setup_tick + 120
            && g_Menus[g_MpPlayerNum].curdialog
            && g_Menus[g_MpPlayerNum].curdialog->definition == &g_MpPlayerStatsMenuDialog) {
        captureCompletedFrame();
        probeLog("PDX_ISSUE multiplayer stats dialog visible");
    }
    if ((!pdxIssueProbeInputEnabled() && scenario != 13 && ticks == 120)
            || (scenario == 13 && !menu_setup_tick && ticks >= 600 && !g_InCutscene)
            // Let direct-stage startup and its initial file menus settle before
            // installing the specific dialog pair under test.
            || (pdxIssueProbeInputEnabled() && !menu_setup_tick && ticks >= 600 && !g_InCutscene)) {
        probeLog("PDX_ISSUE begin scenario=%d", scenario);
        if (pdxIssueProbeInputEnabled()) {
            menu_setup_tick = ticks;
            if (scenario == 5) {
                menuPushRootDialog(&g_CiControlOptionsMenuDialog, MENUROOT_MAINMENU);
            } else if (scenario == 6) {
                g_DtSlot = 0;
                dtGetData()->timetaken = PAL ? 525 : 630;
                menuPushRootDialog(&g_DtCompletedMenuDialog, MENUROOT_TRAINING);
            } else {
                var80088bb4 = 0;
                getHoloTrainingData()->timetaken = PAL ? 525 : 630;
                menuPushRootDialog(&g_HtCompletedMenuDialog, MENUROOT_TRAINING);
            }
            struct menu *menu = &g_Menus[g_MpPlayerNum];
            struct menulayer *layer = &menu->layers[menu->depth - 1];
            expected_dialog_count = layer->numsiblings;
            if (expected_dialog_count > 8) expected_dialog_count = 8;
            for (unsigned i = 0; i < expected_dialog_count; ++i)
                expected_dialogs[i] = layer->siblings[i]->definition;
            probeLog("PDX_INPUT setup scenario=%d siblings=%d", scenario,
                     menu->layers[menu->depth - 1].numsiblings);
        } else if (scenario == 4) {
            /* Submit the out-of-bounds clip from the hardware failure directly
             * to NV2A, then exercise the remaining edges and restore the view. */
            int width = pb_back_buffer_width(), height = pb_back_buffer_height();
            probeLog("PDX_ISSUE clip right begin");
            gfx_nv2a_api.set_scissor(width + 5, 10, 20, 20);
            pb_wait_until_gr_not_busy();
            probeLog("PDX_ISSUE clip right complete");
            gfx_nv2a_api.set_scissor(10, -25, 20, 20);
            pb_wait_until_gr_not_busy();
            gfx_nv2a_api.set_scissor(-25, 10, 20, 20);
            pb_wait_until_gr_not_busy();
            gfx_nv2a_api.set_scissor(10, height + 5, 20, 20);
            pb_wait_until_gr_not_busy();
            gfx_nv2a_api.set_scissor(0, 0, width, height);
            pb_wait_until_gr_not_busy();
            probeLog("PDX_ISSUE clip checks=4 complete");
        } else if (scenario == 1) {
            menuPushRootDialog(&g_CiControlOptionsMenuDialog, MENUROOT_MAINMENU);
        } else if (scenario == 13) {
            menu_setup_tick = ticks;
            // Display-only statistics, kept in this private title process.
            struct mpplayerconfig *player = &g_PlayerConfigsArray[g_MpPlayerNum];
            player->distance = 1234;
            player->accuracy = 875;
            player->gamesplayed = 12;
            player->damagedealt = 123450;
            player->painreceived = 127;
            unsigned failures = checkText("mp-distance", mpMenuTextDistance(NULL), "123.4km\n");
            failures += checkText("mp-accuracy", mpMenuTextAccuracy(NULL), "87.5%");
            const f32 damage[] = {12.7f, 1000, 12345, 123456, 1234567, 12345678};
            const char *expected[] = {"12.7", "1000", "12.3K", "123K", "1.2M", "12M"};
            char text[64];
            for (unsigned i = 0; i < ARRAYCOUNT(damage); ++i) {
                mpFormatDamageValue(text, damage[i]);
                failures += checkText("mp-damage", text, expected[i]);
            }
            struct menuitem slider = {0};
            union handlerdata data = {0};
            data.slider.label = text;
            player->handicap = 127;
            slider.param = g_MpPlayerNum;
            menuhandlerMpHandicapPlayer(MENUOP_GETSLIDERLABEL, &slider, &data);
            failures += checkText("mp-handicap", text, "100%\n");
            slider.param = PDMODEPROP_REACTION;
            g_MissionConfig.pdmodereaction = 0;
            menuhandlerPdModeSetting(MENUOP_GETSLIDERLABEL, &slider, &data);
            failures += checkText("mission-slider", text, "100%\n");
            probeLog("PDX_ISSUE multiplayer-text checks=10 failures=%u", failures);
            menuPushRootDialog(&g_MpPlayerStatsMenuDialog, MENUROOT_MAINMENU);
        } else if (scenario == 8) {
            // Display-only mission-results fixture; no mission is completed or
            // saved. Use the original dialog and its repaired accuracy callback.
            memset(g_Vars.currentplayerstats->shotcount, 0,
                   sizeof(g_Vars.currentplayerstats->shotcount));
            g_Vars.currentplayerstats->shotcount[SHOTREGION_TOTAL] = 12;
            g_Vars.currentplayerstats->shotcount[SHOTREGION_HEAD] = 10;
            menuPushRootDialog(&g_SoloMissionEndscreenFailedMenuDialog, MENUROOT_ENDSCREEN);
            probeLog("PDX_ISSUE mission-display-fixture accuracy=[%s]", endscreenMenuTextAccuracy(NULL));
        } else if (scenario == 2) {
            struct frdata *fr = frGetData();
            fr->timetaken = PAL ? 513 : 615;
            fr->timelimit = 120;
            fr->difficulty = 1;
            fr->score = 85;
            fr->targetsdestroyed = 1;
            fr->numshots = 12;
            fr->numhitsbullseye = 7;
            fr->numhitsring1 = 3;
            fr->numhitsring2 = 0;
            fr->numhitsring3 = 0;
            testTrainingText();
            menuPushRootDialog(&g_FrCompletedMenuDialog, MENUROOT_TRAINING);
            char time_text[104], accuracy_text[104];
            strcpy(time_text, frMenuTextTimeTakenValue(NULL));
            strcpy(accuracy_text, frMenuTextAccuracyValue(NULL));
            probeLog("PDX_ISSUE stats time=[%s] accuracy=[%s]",
                         time_text, accuracy_text);
        }
    }
    if (pdxIssueProbeInputEnabled() && menu_setup_tick && ticks == menu_setup_tick + 60) {
        input_started = sysGetMicroseconds();
        probeLog("PDX_INPUT begin duration_ms=40000 phases=5 scenario=%d", scenario);
    }
    if (input_started && !input_finished && sysGetMicroseconds() - input_started >= 40000000) {
        input_finished = true;
        probeLog("PDX_INPUT phase-end phase=%d edges=%u reversals=%u swipes=%u",
                 input_phase, input_edges - phase_edges,
                 input_reversals - phase_reversals, menu_swipes - phase_swipes);
        probeLog("PDX_INPUT complete scenario=%d elapsed_ms=%u polls=%u edges=%u reversals=%u swipes=%u opening_swipes=%u max_poll_gap_us=%u unexpected_swipes=%u",
                 scenario, (unsigned)((sysGetMicroseconds() - input_started) / 1000),
                 input_polls, input_edges, input_reversals, menu_swipes, opening_swipes,
                 (unsigned)maximum_poll_gap, unexpected_swipes);
    }
    if (scenario == 1 && ticks > 180 && ticks < 2400 && ticks % 3 == 0) {
        /* Direct transition stress: one request per three main ticks.
         * Wall-clock rate depends on performance; this bypasses pad input. */
        if (g_Menus[g_MpPlayerNum].curdialog && g_Menus[g_MpPlayerNum].depth > 0) {
            menuSwipe((ticks / 45) % 2 ? 1 : -1);
            ++swipes;
        }
    }
    if (scenario == 3 && ticks >= 180 && !spy_requested && !g_InCutscene
            && g_Vars.currentplayer->eyespy) {
        /* Activate the same device bit as the inventory. The original player
         * tick performs launch, collision checks, camera movement and HUD. */
        g_Vars.currentplayer->devicesactive |= DEVICE_EYESPY;
        spy_requested = true;
        probeLog("PDX_ISSUE eyespy activation requested");
    }
    if (ticks % 300 == 0) {
        probeLog("PDX_ISSUE progress scenario=%d ticks=%u swipes=%u",
                     scenario, ticks, swipes);
        if (scenario == 3 && g_Vars.currentplayer->eyespy) {
            struct eyespy *spy = g_Vars.currentplayer->eyespy;
            if (fatal_after_lens && ticks >= 1200 && spy->active && spy->deployed)
                sysFatalError("Qualification: lens error display");
            probeLog("PDX_ISSUE eyespy active=%d deployed=%d held=%d startup=%d cutscene=%d",
                     spy->active, spy->deployed, spy->held, spy->startuptimer60, g_InCutscene);
        }
    }
}
#endif
