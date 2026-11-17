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
#include "game/training.h"
#include "issue_probe_xbox.h"
#include "serial_xbox.h"
#include <pbkit/pbkit.h>
#include "../../fast3d/gfx_nv2a.h"

extern struct menudialogdef g_CiControlOptionsMenuDialog;
extern char *frMenuTextTimeTakenValue(struct menuitem *item);
extern char *frMenuTextAccuracyValue(struct menuitem *item);
extern char *dtMenuTextTimeTakenValue(struct menuitem *item);
extern char *htMenuTextTimeTakenValue(struct menuitem *item);
static s32 scenario;
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

static void probeLog(const char *fmt, ...)
{
    char line[256];
    va_list args;
    va_start(args, fmt);
    vsnprintf(line, sizeof(line), fmt, args);
    va_end(args);
    serialPuts(line);
    serialPutc('\n');
}

s32 pdxIssueProbeInputEnabled(void)
{
    return scenario >= 5 && scenario <= 7;
}

/* Inject only into this Xbox process, before libultra's controller sample
 * queue. Normal edge detection, menu repeat and dialog logic consume it. */
s32 pdxIssueProbeReadPad(s32 idx, OSContPad *pad)
{
    if (idx != 0) return -1;
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
}

PD_CONSTRUCTOR static void issueProbeConfig(void)
{
    configRegisterInt("Game.IssueProbe", &scenario, 0, 7);
}

void pdxIssueProbeTick(void)
{
    if (!scenario || g_Vars.stagenum >= STAGE_TITLE) return;
    ++ticks;
    if (ticks == 120) {
        probeLog("PDX_ISSUE begin scenario=%d", scenario);
        if (pdxIssueProbeInputEnabled()) {
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
    if (pdxIssueProbeInputEnabled() && ticks == 180) {
        input_started = sysGetMicroseconds();
        probeLog("PDX_INPUT begin duration_ms=40000 phases=5 scenario=%d", scenario);
    }
    if (input_started && !input_finished && sysGetMicroseconds() - input_started >= 40000000) {
        input_finished = true;
        probeLog("PDX_INPUT phase-end phase=%d edges=%u reversals=%u swipes=%u",
                 input_phase, input_edges - phase_edges,
                 input_reversals - phase_reversals, menu_swipes - phase_swipes);
        probeLog("PDX_INPUT complete scenario=%d elapsed_ms=%u polls=%u edges=%u reversals=%u swipes=%u opening_swipes=%u max_poll_gap_us=%u",
                 scenario, (unsigned)((sysGetMicroseconds() - input_started) / 1000),
                 input_polls, input_edges, input_reversals, menu_swipes, opening_swipes,
                 (unsigned)maximum_poll_gap);
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
            probeLog("PDX_ISSUE eyespy active=%d deployed=%d held=%d startup=%d cutscene=%d",
                     spy->active, spy->deployed, spy->held, spy->startuptimer60, g_InCutscene);
        }
    }
}
#endif
