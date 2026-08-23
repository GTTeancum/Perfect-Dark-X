// Xbox window manager — implements GfxWindowManagerAPI for the original Xbox.
//
// The Xbox has no windowing system.  "Window management" here means:
//   • Initialising the NV2A GPU via pbkit
//   • Setting the video output mode via XVideoSetMode
//   • Timing (KeQueryPerformanceCounter for get_time / FPS limiter)
//   • Frame pacing (pb_show_front_screen / pb_wait_for_vbl)
//   • Polling the SDL event queue (for SDL_QuitEvent from the dashboard button)
//
// Supported video modes (Xbox hardware):
//   #0  640×480  480i   NTSC default
//   #1  640×480  480p   (HDTV cable required)
//   #2  720×480  480p   (widescreen)
//   #3  1280×720 720p   (Xbox HD AV pack required)

#ifdef PLATFORM_XBOX

#include <cstdint>
#include <cstdio>
#include <cstring>

extern "C" {
#include <xboxkrnl/xboxkrnl.h>
#include <hal/video.h>
#include <hal/debug.h>
#include <pbkit/pbkit.h>
#include <SDL.h>
#include "../src/xbox/debug_xbox.h"
}

#include "gfx_window_manager_api.h"
#include "gfx_screen_config.h"
#include "platform.h"
#include "system.h"
#include "gfx_xbox_wm.h"
#include "../src/xbox/serial_xbox.h"

// ── Supported display modes ───────────────────────────────────────────────────

struct XboxDisplayMode {
    int     width;
    int     height;
    int     refresh;    // Hz
    bool    progressive;
    bool    widescreen;
    VIDEO_MODE hal_mode;
};

static const XboxDisplayMode k_modes[] = {
    { 640,  480, 60, false, false, {640, 480, 32, 60} },   // 480i
    { 640,  480, 60, true,  false, {640, 480, 32, 60} },   // 480p
    { 720,  480, 60, true,  true,  {720, 480, 32, 60} },   // 480p wide
    { 1280, 720, 60, true,  false, {1280,720, 32, 60} },   // 720p
};
static const int k_num_modes = (int)(sizeof(k_modes) / sizeof(k_modes[0]));

static int g_current_mode_idx = 0;  // default 480i
static bool g_is_running = true;

static void (*g_on_fullscreen_changed)(bool) = nullptr;

// ── Timing ────────────────────────────────────────────────────────────────────

static ULONGLONG g_perf_freq  = 0;
static ULONGLONG g_frame_start = 0;
static int       g_target_fps  = 60;

static ULONGLONG qpc_now(void)
{
    // NXDK: KeQueryPerformanceCounter() returns ULONGLONG directly (no pointer arg)
    return KeQueryPerformanceCounter();
}

static double qpc_to_sec(ULONGLONG ticks)
{
    return (double)ticks / (double)g_perf_freq;
}

// ── Initialisation ────────────────────────────────────────────────────────────

static void xbox_wm_init(const struct GfxWindowInitSettings *settings)
{
    // Resolve performance frequency
    // NXDK: KeQueryPerformanceFrequency() returns ULONGLONG directly (no pointer arg)
    g_perf_freq = KeQueryPerformanceFrequency();

    // NXDK does not expose XGetVideoFlags() from the original XSDK.
    // Default to 480i for maximum compatibility.
    // TODO: detect HDTV capability via EEPROM or XVideoQueryAvailableModes()
    // once we have the port running.
    (void)settings;
    g_current_mode_idx = 0; // 480i

    const XboxDisplayMode &m = k_modes[g_current_mode_idx];
    sysLogPrintf(LOG_NOTE, "Xbox video mode: %dx%d %s",
        m.width, m.height, m.progressive ? "progressive" : "interlaced");

    XVideoSetMode(m.width, m.height, m.hal_mode.bpp, m.hal_mode.refresh);

    // The renderer uses one full-size, non-rotating surface as a scratch render
    // target.  Persistent game framebuffers are copied to/from their own
    // compact texture allocations, so a single extra pbkit surface is enough
    // for menu blur and any future offscreen pass without spending ~1.2 MiB on
    // each of the game's many 16x16 capture buffers.
    pb_extra_buffers(1);

    // Initialise pbkit (NV2A push-buffer engine)
    int pb_err = pb_init();
    if (pb_err != 0) {
        sysFatalError("pb_init() failed (err %d).  Cannot initialise GPU.", pb_err);
    }

    pb_show_front_screen();

    // Let the debug overlay know pbkit is running so it can switch
    // from debugPrint (raw text mode) to pb_print (GPU framebuffer)
    dbgNotifyPbkitUp();

    // Initialise SDL (audio + gamepad only — no video via SDL)
    if (SDL_Init(SDL_INIT_GAMECONTROLLER | SDL_INIT_AUDIO) != 0) {
        sysLogPrintf(LOG_ERROR, "SDL_Init error: %s", SDL_GetError());
    }

    g_frame_start = qpc_now();

    sysLogPrintf(LOG_NOTE, "Xbox window manager initialised");
}

static void xbox_wm_close(void)
{
    SDL_Quit();
    pb_kill();
}

// ── Display mode enumeration ──────────────────────────────────────────────────

static int xbox_wm_get_num_display_modes(void)
{
    return k_num_modes;
}

static int xbox_wm_get_display_mode(int modenum, int *out_w, int *out_h)
{
    if (modenum < 0 || modenum >= k_num_modes) return 0;
    *out_w = k_modes[modenum].width;
    *out_h = k_modes[modenum].height;
    return 1;
}

static int xbox_wm_get_current_display_mode(int *out_w, int *out_h)
{
    *out_w = k_modes[g_current_mode_idx].width;
    *out_h = k_modes[g_current_mode_idx].height;
    return 1;
}

// ── Fullscreen (always fullscreen on Xbox) ────────────────────────────────────

static int32_t xbox_wm_get_fullscreen_state(void)      { return 1; }
static int32_t xbox_wm_get_fullscreen_flag_mode(void)  { return 0; }
static int32_t xbox_wm_get_maximized_state(void)       { return 0; }

static void xbox_wm_set_fullscreen_changed_callback(void (*cb)(bool))
{
    g_on_fullscreen_changed = cb;
}

static void xbox_wm_set_fullscreen(bool enable)        { (void)enable; }
static void xbox_wm_set_fullscreen_exclusive(bool exc) { (void)exc; }
static void xbox_wm_set_fullscreen_flag(int32_t mode)  { (void)mode; }
static void xbox_wm_set_maximize(bool enable)          { (void)enable; }

// ── Window dimensions (fixed by video mode) ───────────────────────────────────

static void xbox_wm_get_dimensions(uint32_t *w, uint32_t *h,
                                    int32_t *posX, int32_t *posY)
{
    *w    = (uint32_t)k_modes[g_current_mode_idx].width;
    *h    = (uint32_t)k_modes[g_current_mode_idx].height;
    *posX = 0;
    *posY = 0;
}

static void xbox_wm_set_dimensions(uint32_t w, uint32_t h,
                                    int32_t posX, int32_t posY)
{
    (void)w; (void)h; (void)posX; (void)posY;
    // Cannot resize on Xbox — video mode is fixed at boot
}

static void xbox_wm_get_centered_positions(int32_t w, int32_t h,
                                            int32_t *posX, int32_t *posY)
{
    (void)w; (void)h;
    *posX = *posY = 0;
}

static void xbox_wm_set_closest_resolution(int32_t w, int32_t h, bool center)
{
    (void)w; (void)h; (void)center;
}

// ── Refresh rate ──────────────────────────────────────────────────────────────

static void xbox_wm_get_active_window_refresh_rate(uint32_t *rr)
{
    *rr = (uint32_t)k_modes[g_current_mode_idx].refresh;
}

// ── Cursor / title (no-ops on Xbox) ──────────────────────────────────────────

static void xbox_wm_set_cursor_visibility(bool visible) { (void)visible; }
static void xbox_wm_set_window_title(const char *title) { (void)title; }
static void *xbox_wm_get_window_handle(void) { return nullptr; }

// ── Event handling ────────────────────────────────────────────────────────────

static void xbox_wm_handle_events(void)
{
    SDL_Event ev;
    while (SDL_PollEvent(&ev)) {
        if (ev.type == SDL_QUIT) {
            g_is_running = false;
        }
        // All other events (controller connect/disconnect etc.) are handled
        // by the input layer.
    }
}

// ── Frame timing ──────────────────────────────────────────────────────────────

static bool xbox_wm_start_frame(void)
{
    return g_is_running;
}

static void xbox_wm_swap_buffers_begin(void)
{
    // pb_show_front_screen() is called in gfx_nv2a finish_render; nothing here.
}

static void xbox_wm_swap_buffers_end(void)
{
    // Frame heartbeat on the UART: the only way to tell "loop alive but
    // drawing nothing" from "loop blocked" without taking the window.
    {
        static unsigned frames = 0;
        static ULONGLONG last = 0;
        if ((++frames % 60u) == 0u) {
            const ULONGLONG now = qpc_now();
            char hb[96];
            if (last && now > last) {
                const ULONGLONG dt = now - last;
                const unsigned milli = (unsigned)((60ULL * g_perf_freq * 1000ULL) / dt);
                snprintf(hb, sizeof(hb), "frame %u  fps %u.%03u\n",
                         frames, milli / 1000u, milli % 1000u);
            } else {
                snprintf(hb, sizeof(hb), "frame %u  (first)\n", frames);
            }
            last = now;
            serialPuts(hb);
        }
    }

    if (g_target_fps > 0) {
        // Simple busy-wait FPS limiter
        const ULONGLONG frame_ns = g_perf_freq / (ULONGLONG)g_target_fps;
        while ((qpc_now() - g_frame_start) < frame_ns) {
            // spin
        }
    }
    g_frame_start = qpc_now();
}

static double xbox_wm_get_time(void)
{
    return qpc_to_sec(qpc_now());
}

// ── FPS target ────────────────────────────────────────────────────────────────

static int32_t xbox_wm_get_target_fps(void)
{
    return g_target_fps;
}

static void xbox_wm_set_target_fps(int fps)
{
    g_target_fps = fps;
}

// ── VSync (XBox is always locked to vblank via pbkit) ────────────────────────

static bool xbox_wm_can_disable_vsync(void)
{
    return false;  // pbkit always syncs to the display's vblank
}

static int xbox_wm_get_swap_interval(void)
{
    return 1;
}

static bool xbox_wm_set_swap_interval(int interval)
{
    (void)interval;
    return false; // always 1 on Xbox
}

// ── API struct ────────────────────────────────────────────────────────────────

struct GfxWindowManagerAPI gfx_xbox_wm = {
    xbox_wm_init,
    xbox_wm_close,
    xbox_wm_get_display_mode,
    xbox_wm_get_current_display_mode,
    xbox_wm_get_num_display_modes,
    xbox_wm_get_fullscreen_state,
    xbox_wm_set_fullscreen_changed_callback,
    xbox_wm_set_fullscreen,
    xbox_wm_set_fullscreen_exclusive,
    xbox_wm_set_fullscreen_flag,
    xbox_wm_get_fullscreen_flag_mode,
    xbox_wm_get_maximized_state,
    xbox_wm_set_maximize,
    xbox_wm_get_active_window_refresh_rate,
    xbox_wm_set_cursor_visibility,
    xbox_wm_set_closest_resolution,
    xbox_wm_set_dimensions,
    xbox_wm_get_dimensions,
    xbox_wm_get_centered_positions,
    xbox_wm_handle_events,
    xbox_wm_start_frame,
    xbox_wm_swap_buffers_begin,
    xbox_wm_swap_buffers_end,
    xbox_wm_get_time,
    xbox_wm_get_target_fps,
    xbox_wm_set_target_fps,
    xbox_wm_can_disable_vsync,
    xbox_wm_get_window_handle,
    xbox_wm_set_window_title,
    xbox_wm_get_swap_interval,
    xbox_wm_set_swap_interval,
};

#endif // PLATFORM_XBOX
