// Minimal crash handler for Xbox — replaces port/src/crash.c.
// On Xbox there is no OS-level signal handling; we hook what we can via
// the NXDK exception mechanism and print to the debug output.

#include <PR/ultratypes.h>
#include <hal/debug.h>
#include "platform.h"
#include "system.h"

void crashInit(void)
{
    // No-op: NXDK does not expose a portable exception-hook API.
    // Unhandled exceptions will halt the CPU; sysFatalError() spins with a
    // screen message instead.
    sysLogPrintf(LOG_NOTE, "crash handler: xbox stub (no-op)");
}

void crashShutdown(void)
{
    // No-op
}

void crashCreateThread(void)
{
    // No-op: no crash thread on Xbox.
}

void crashSetMessage(char *string)
{
    (void)string;
    // No-op.
}

void crashReset(void)
{
    // No-op.
}

void crashAppendChar(char c)
{
    (void)c;
    // No-op.
}
