// Xbox system layer — replaces port/src/system.c when PLATFORM_XBOX is defined.
// Uses NXDK kernel APIs for timing, sleep, and path resolution.

#include <stdlib.h>
#include <stdarg.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

#include <xboxkrnl/xboxkrnl.h>
// mkdir for directory creation (pdclib may not have it; use XboxCreateDirectory stub if needed)
static inline void xboxMkdir(const char *path) { (void)path; }
#include <hal/debug.h>
#include <SDL.h>

#include <PR/ultratypes.h>
#include "platform.h"
#include "system.h"
#include "serial_xbox.h"

// ── Timing ───────────────────────────────────────────────────────────────────

#define USEC_IN_SEC 1000000ULL

static ULONGLONG perfFreq   = 0;
static ULONGLONG startTick  = 0;

static ULONGLONG qpc(void)
{
    // NXDK: KeQueryPerformanceCounter() returns ULONGLONG directly (no pointer arg)
    return KeQueryPerformanceCounter();
}

// ── Logging ──────────────────────────────────────────────────────────────────

#define LOG_FNAME "D:\\pd.log"

static char logPath[512];

// ── Arg stubs (no command-line on Xbox) ─────────────────────────────────────

void sysInitArgs(s32 argc, const char **argv)
{
    (void)argc;
    (void)argv;
}

s32 sysArgCheck(const char *arg)
{
    (void)arg;
    return 0;
}

const char *sysArgGetString(const char *arg)
{
    (void)arg;
    return NULL;
}

s32 sysArgGetInt(const char *arg, s32 defval)
{
    (void)arg;
    return defval;
}

// ── Init ─────────────────────────────────────────────────────────────────────

void sysInit(void)
{
    // NXDK: KeQueryPerformanceFrequency() returns ULONGLONG directly
    perfFreq  = KeQueryPerformanceFrequency();
    startTick = qpc();

    // Open log on D:\ (game media / USB drive)
    snprintf(logPath, sizeof(logPath), LOG_FNAME);
    FILE *f = fopen(logPath, "wb");
    if (f) {
        fclose(f);
    } else {
        // Fallback: T:\ is always writable
        snprintf(logPath, sizeof(logPath), "T:\\pd.log");
        f = fopen(logPath, "wb");
        if (f) fclose(f);
    }

    sysLogPrintf(LOG_NOTE, "Xbox system initialised");
    sysLogPrintf(LOG_NOTE, "perf counter frequency: %llu Hz", perfFreq);
}

// ── Time ─────────────────────────────────────────────────────────────────────

u64 sysGetMicroseconds(void)
{
    ULONGLONG elapsed = qpc() - startTick;
    return (u64)((elapsed * USEC_IN_SEC) / perfFreq);
}

// ── Logging ──────────────────────────────────────────────────────────────────

s32 sysLogIsOpen(void)
{
    return (logPath[0] != '\0');
}

void sysLogPrintf(s32 level, const char *fmt, ...)
{
    static const char *prefix[3] = { "", "WARNING: ", "ERROR: " };

    char msg[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);

    // Write to log file
    if (logPath[0]) {
        FILE *f = fopen(logPath, "ab");
        if (f) {
            fprintf(f, "%s%s\n", prefix[level], msg);
            fclose(f);
        }
    }

    // Also print to NXDK debug output (visible via serial / debugger)
    debugPrint("%s%s\n", prefix[level], msg);
    serialPuts(prefix[level]);
    serialPuts(msg);
    serialPutc('\n');
}

void sysFatalError(const char *fmt, ...)
{
    static s32 alreadyCrashed = 0;
    if (alreadyCrashed) {
        for (;;) {} // spin
    }
    alreadyCrashed = 1;

    char msg[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);

    sysLogPrintf(LOG_ERROR, "FATAL: %s", msg);

    // Display on screen via debugPrint (pbkit must already be initialised)
    debugClearScreen();
    debugPrint("FATAL ERROR:\n%s\n\nPress power to reboot.\n", msg);

    // Spin forever — let the user read the message
    for (;;) {}
}

// ── Paths ─────────────────────────────────────────────────────────────────────

// Game assets live alongside default.xbe on the game media (D:\).
void sysGetExecutablePath(char *outPath, const u32 outLen)
{
    strncpy(outPath, "D:", outLen - 1);
    outPath[outLen - 1] = '\0';
}

// Save data lives on the hard drive in TDATA (persistent across boots).
// E:\TDATA\<TitleID>\ is the correct location; we use a human-readable name
// since we control the directory.
void sysGetHomePath(char *outPath, const u32 outLen)
{
    strncpy(outPath, "E:\\TDATA\\PerfectDarkX", outLen - 1);
    outPath[outLen - 1] = '\0';

    xboxMkdir(outPath); // best-effort; save dir is created by NXDK at title launch
}

// ── Memory ────────────────────────────────────────────────────────────────────

void *sysMemAlloc(const u32 size)
{
    return malloc(size);
}

void *sysMemZeroAlloc(const u32 size)
{
    return calloc(1, size);
}

void *sysMemRealloc(void *ptr, const u32 newSize)
{
    return realloc(ptr, newSize);
}

void sysMemFree(void *ptr)
{
    free(ptr);
}

// ── Sleep / Yield ─────────────────────────────────────────────────────────────

void sysSleep(const s64 hns)
{
    // KeDelayExecutionThread takes 100-ns units, negative = relative
    LARGE_INTEGER interval;
    interval.QuadPart = -hns;
    KeDelayExecutionThread(UserMode, FALSE, &interval);
}

void sysCpuRelax(void)
{
    // Pentium III _mm_pause() equivalent via inline asm
    __asm__ volatile("pause" ::: "memory");
}
