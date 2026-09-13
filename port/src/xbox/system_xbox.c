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
#include "pdx_version.h"
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
static FILE *logFile;
static u32 logLinesSinceFlush;
#if defined(PD_XBOX_ISSUE3_DIAGNOSTIC)
static unsigned logBytes;
static FILE *startupLog;
static unsigned startupBytes;
#endif

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
    logFile = fopen(logPath, "wb");
    if (!logFile) {
        // Fallback: T:\ is always writable
        snprintf(logPath, sizeof(logPath), "T:\\pd.log");
        logFile = fopen(logPath, "wb");
    }
    if (logFile) {
        // Keep the file open. Reopening and closing pd.log for every routine
        // asset-load message causes visible stalls during stage transitions.
        // A small full buffer coalesces those writes; critical renderer and
        // error messages are flushed immediately below.
        setvbuf(logFile, NULL, _IOFBF, 16 * 1024);
    }

#if defined(PD_XBOX_ISSUE3_DIAGNOSTIC)
    char startupPath[512];
    snprintf(startupPath, sizeof(startupPath), "%c:\\pd.startup.log", logPath[0]);
    startupLog = fopen(startupPath, "wb");
    sysLogPrintf(LOG_NOTE, "NV2A PERF DIAG1 release=" PDX_VERSION " original-presentation build=" __DATE__ " " __TIME__ " log=%s cap=8MiB+8MiB startup=128KiB; queue snapshots approximate", logPath);
#endif
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
    return logFile != NULL;
}

void sysLogPrintf(s32 level, const char *fmt, ...)
{
    static const char *prefix[3] = { "", "WARNING: ", "ERROR: " };

    char msg[1024];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(msg, sizeof(msg), fmt, ap);
    va_end(ap);

    const s32 urgent = level != LOG_NOTE
        || strstr(msg, "NV2A HWTRACE") != NULL
        || strstr(msg, "NV2A TRACE BUILD") != NULL
        || strstr(msg, "NV2A SHADER ABI") != NULL
        || strstr(msg, "NV2A TEXPROOF") != NULL
        || strstr(msg, "NV2A PERF") != NULL
        || strstr(msg, "NV2A TIMEOUT") != NULL
        || strstr(msg, "NV2A TEX INVALID") != NULL
        || strstr(msg, "PDTX HWTRACE") != NULL
        || strstr(msg, "PDTX PERF") != NULL
        || strstr(msg, "loading:") != NULL
        || strstr(msg, "PHASE") != NULL;

    if (logFile) {
#if defined(PD_XBOX_ISSUE3_DIAGNOSTIC)
        if (logBytes >= 8u * 1024u * 1024u) {
            char rolled[512];
            snprintf(rolled, sizeof(rolled), "%c:\\pd.previous.log", logPath[0]);
            fclose(logFile);
            remove(rolled);
            rename(logPath, rolled);
            logFile = fopen(logPath, "wb");
            logBytes = 0;
            if (logFile) setvbuf(logFile, NULL, _IOFBF, 16 * 1024);
        }
        if (logFile) {
            int written = fprintf(logFile, "[diag1 %llu ms] %s%s\n", sysGetMicroseconds()/1000, prefix[level], msg);
            if (written > 0) logBytes += written;
        }
        if (startupLog) {
            int written = fprintf(startupLog, "[diag1 %llu ms] %s%s\n", sysGetMicroseconds()/1000, prefix[level], msg);
            if (written > 0) startupBytes += written;
            if (urgent) fflush(startupLog);
            if (startupBytes >= 128u * 1024u) { fclose(startupLog); startupLog = NULL; }
        }
#else
        fprintf(logFile, "%s%s\n", prefix[level], msg);
#endif
        ++logLinesSinceFlush;
        if (urgent || logLinesSinceFlush >= 64) {
            if (logFile) fflush(logFile);
            logLinesSinceFlush = 0;
        }
    }

    // debugPrint writes into the HAL framebuffer and must not run after pbkit
    // owns the display. Keep only high-value messages on COM1; routine loose-
    // file chatter belongs in the buffered on-disk log.
    if (urgent) {
        serialPuts(prefix[level]);
        serialPuts(msg);
        serialPutc('\n');
    }
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
