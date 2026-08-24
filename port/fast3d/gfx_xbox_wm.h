#pragma once
#ifdef PLATFORM_XBOX

#include "gfx_window_manager_api.h"

#ifdef __cplusplus
extern "C" {
#endif

extern struct GfxWindowManagerAPI gfx_xbox_wm;

float gfx_xbox_wm_get_display_aspect(void);
bool gfx_xbox_wm_is_widescreen(void);
bool gfx_xbox_wm_is_progressive(void);
bool gfx_xbox_wm_is_720p(void);

#ifdef __cplusplus
}
#endif

#endif // PLATFORM_XBOX
