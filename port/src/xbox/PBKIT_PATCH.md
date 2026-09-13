# pbkit core override

`pbkit_core.c` is nxdk `lib/pbkit/pbkit.c` from commit
`fb5a9a7a58a431e8d70a9e7da87898059df376c0`, with its MIT SPDX license and
copyright notices retained. The Xbox source glob links this object directly;
the linker then uses the installed `libpbkit.lib` for the remaining pbkit
objects, without extracting its original core object. No installed nxdk files
are modified. The normal nxdk pbkit headers are still required.
The MIT license text is included in `PBKIT_LICENSE.txt`.

The local change ensures `pb_vbl_handler()` clears a ready flag only when
`PCRTC_START` already selects that queued framebuffer. The ISR performs that
write early. However, the DPC loops with INTA disabled and can handle another
VBlank without a corresponding ISR scanout write. The original handler can
therefore consume a completed frame without displaying it. Eventually the
next render target can remain scanout with an empty queue.

Two 720p guest snapshots showed that state after adding a render-target
ownership guard. The guard must remain: this correction repairs queue
consumption, while the guard prevents rendering into a displayed surface.

When scanout does not match, the entry stays queued for the next ISR. This
preserves the early VBlank scanout write instead of introducing a late write
from the DPC. `g_SPXBPbkitDeferredSwaps` counts these deferrals. Read it from
normal thread context; do not log from the DPC.

When updating nxdk, compare this file with the new core and retain the queue
retirement invariant. Verify the linker map attributes core functions such
as `pb_finished` to `pbkit_core.c.obj`, not `libpbkit:pbkit.obj`.

## Diagnostic baseline switch

`PD_XBOX_ISSUE3_DIAGNOSTIC` deliberately restores original queue retirement
while counting scanout mismatches. It also exposes a read-only, non-atomic
queue snapshot to the renderer. In this mode the global deferral counter
counts mismatched retirements, not prevented swaps. The renderer guard and
other candidate presentation changes are disabled by the same switch.
