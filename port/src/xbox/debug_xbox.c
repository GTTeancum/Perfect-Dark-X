// Xbox phase-debug overlay for XEMU testing.
//
// Strategy
// ────────
// Before pbkit is up (phases 0-4) we use NXDK's debugPrint() which writes
// text into the boot framebuffer visible immediately in XEMU.
//
// After pb_init (phase 5+) the pbkit framebuffer takes over.  We switch to
// pb_print() / pb_draw_text_screen() which renders text via the NV2A GPU.
//
// Every phase is also written to D:\pd.log so the full sequence is preserved
// even if the screen is hard to read in a screenshot.

#ifdef PLATFORM_XBOX

#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <xboxkrnl/xboxkrnl.h>
#include <hal/debug.h>
#include <pbkit/pbkit.h>

#include "platform.h"
#include "system.h"
#include "debug_xbox.h"

// Set by video init once pbkit is running
static int g_pbkit_up = 0;

// Ring buffer of phase messages shown on the pbkit overlay
#define MAX_PHASE_LINES 20
static char g_phase_lines[MAX_PHASE_LINES][128];
static int  g_phase_count = 0;

void dbgPhase(int phase, const char *msg)
{
    char buf[128];
    snprintf(buf, sizeof(buf), "[%02d] %s", phase, msg);

    // Always log to file
    sysLogPrintf(LOG_NOTE, "PHASE %s", buf);

    if (!g_pbkit_up) {
        // Raw text mode — visible before pbkit init
        debugPrint("%s\n", buf);
    }

    // Store in ring buffer for pbkit overlay
    int idx = g_phase_count < MAX_PHASE_LINES ? g_phase_count : MAX_PHASE_LINES - 1;
    strncpy(g_phase_lines[idx], buf, sizeof(g_phase_lines[0]) - 1);
    g_phase_lines[idx][sizeof(g_phase_lines[0]) - 1] = '\0';
    if (g_phase_count < MAX_PHASE_LINES) ++g_phase_count;

    if (g_pbkit_up) {
        dbgFlushScreen();
    }
}

void dbgFlushScreen(void)
{
    if (!g_pbkit_up) return;

    // Clear the back buffer to dark blue so text is readable
    uint32_t *p = pb_begin();
    pb_push1(p, NV097_CLEAR_SURFACE, 0xF0); // clear colour
    pb_end(p);

    // Wait for the clear, then print text via pb_print
    pb_wait_for_vbl();

    pb_print("=== Perfect Dark X - Xbox Port ===\n");
    pb_print("Phase debug output\n\n");

    for (int i = 0; i < g_phase_count; ++i) {
        pb_print("%s\n", g_phase_lines[i]);
    }

    pb_print("\n(check D:\\pd.log for full output)\n");
    pb_draw_text_screen();
    pb_show_front_screen();
}

void dbgPause(int ms)
{
#if DBG_PHASE_PAUSE_MS > 0
    if (ms <= 0) ms = DBG_PHASE_PAUSE_MS;
    // KeDelayExecutionThread: 100-ns units, negative = relative
    LARGE_INTEGER interval;
    interval.QuadPart = -(LONGLONG)ms * 10000LL;
    KeDelayExecutionThread(UserMode, FALSE, &interval);
#else
    (void)ms;
#endif
}

// Called by xbox_wm_init() after pb_init() succeeds
void dbgNotifyPbkitUp(void)
{
    g_pbkit_up = 1;
}

#endif // PLATFORM_XBOX
