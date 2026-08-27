#pragma once
#ifdef PLATFORM_XBOX

#include "gfx_rendering_api.h"

#ifdef __cplusplus
extern "C" {
#endif

extern struct GfxRenderingAPI gfx_nv2a_api;

// Draw a self-contained black loading frame through pbkit while the normal
// fast3d frame loop is suspended for a synchronous stage reset.
void gfx_xbox_loading_begin(int from_stage, int to_stage);
void gfx_xbox_loading_pulse(void);
void gfx_xbox_loading_end(int stage);

#ifdef __cplusplus
}
#endif

#endif // PLATFORM_XBOX
