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
    configRegisterInt("Game.IssueProbe", &scenario, 0, 4);
}

void pdxIssueProbeTick(void)
{
    if (!scenario || g_Vars.stagenum >= STAGE_TITLE) return;
    ++ticks;
    if (ticks == 120) {
        probeLog("PDX_ISSUE begin scenario=%d", scenario);
        if (scenario == 4) {
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
    if (scenario == 1 && ticks > 180 && ticks < 2400 && ticks % 3 == 0) {
        /* Exercise the original swipe/transition path at 20 requests/second. */
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
