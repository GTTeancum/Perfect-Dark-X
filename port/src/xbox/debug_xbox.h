#pragma once
#ifdef PLATFORM_XBOX

#include <stdint.h>

// Maximum phase index.  Expand as needed.
#define DBG_PHASE_ENTRY       0   // void main() reached
#define DBG_PHASE_CRASH_INIT  1   // crashInit done
#define DBG_PHASE_SYS_INIT    2   // sysInit done
#define DBG_PHASE_FS_INIT     3   // fsInit done
#define DBG_PHASE_CFG_INIT    4   // configInit done
#define DBG_PHASE_VIDEO_INIT  5   // videoInit (pbkit + NV2A) done
#define DBG_PHASE_INPUT_INIT  6   // inputInit done
#define DBG_PHASE_AUDIO_INIT  7   // audioInit done
#define DBG_PHASE_ROM_LOAD    8   // romdataInit ROM found & validated
#define DBG_PHASE_GAME_INIT   9   // gameInit done, heap allocated
#define DBG_PHASE_SCHED       10  // bootCreateSched done
#define DBG_PHASE_MAIN_PROC   11  // mainProc() about to be called

// Call at each init boundary.  Before videoInit uses debugPrint (raw text
// mode); from videoInit onwards uses pb_print on the pbkit framebuffer.
// Always writes to the log file as well.
void dbgPhase(int phase, const char *msg);

// Show the current phase list on screen (pb_print + pb_draw_text_screen).
// Only valid after videoInit.
void dbgFlushScreen(void);

// Pause for approx `ms` milliseconds so XEMU screenshots can capture each
// phase.  Set DBG_PHASE_PAUSE_MS=0 in the build to skip all pauses.
void dbgPause(int ms);

// Set to 0 to disable inter-phase sleeps (avoids KeDelayExecutionThread issues
// in early-boot XEMU context; pauses can be re-enabled once the game is stable).
#ifndef DBG_PHASE_PAUSE_MS
#define DBG_PHASE_PAUSE_MS 0
#endif

// Called internally by gfx_xbox_wm after pb_init() succeeds.
// Switches dbgPhase() output from debugPrint to pb_print.
void dbgNotifyPbkitUp(void);

#endif // PLATFORM_XBOX
