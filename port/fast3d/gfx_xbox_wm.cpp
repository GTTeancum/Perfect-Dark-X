// Xbox window manager — implements GfxWindowManagerAPI for the original Xbox.
//
// The Xbox has no windowing system.  "Window management" here means:
//   • Initialising the NV2A GPU via pbkit
//   • Setting the video output mode via XVideoSetMode
//   • Timing (KeQueryPerformanceCounter for get_time / FPS limiter)
//   • Frame pacing (pb_show_front_screen / pb_wait_for_vbl)
//   • Polling the SDL event queue (for SDL_QuitEvent from the dashboard button)
//
// Aspect ratio and resolution come exclusively from the Xbox dashboard. A
// console with 720p enabled uses guarded native 1280x720; otherwise the game
// uses 640x480 as dashboard-selected 4:3 or anamorphic 16:9. The 720p path
// falls back to 480-line output if its mode or pbkit allocation cannot start.

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

static int g_output_width = 640;
static int g_output_height = 480;
static int g_refresh = 60;
static bool g_widescreen = false;
static bool g_progressive = false;
static bool g_can_480p = false;
static bool g_can_720p = false;
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

float gfx_xbox_wm_get_display_aspect(void)
{
    if (g_widescreen || (g_output_width == 1280 && g_output_height == 720)) {
        return 16.0f / 9.0f;
    }
    return (float)g_output_width / (float)g_output_height;
}

bool gfx_xbox_wm_is_widescreen(void)
{
    return g_widescreen;
}

bool gfx_xbox_wm_is_progressive(void)
{
    return g_progressive;
}

bool gfx_xbox_wm_is_720p(void)
{
    return g_output_width == 1280 && g_output_height == 720;
}

// ── Initialisation ────────────────────────────────────────────────────────────

static void xbox_wm_init(const struct GfxWindowInitSettings *settings)
{
    (void)settings;
    // Resolve performance frequency
    // NXDK: KeQueryPerformanceFrequency() returns ULONGLONG directly (no pointer arg)
    g_perf_freq = KeQueryPerformanceFrequency();

    // NXDK exposes the dashboard/AV-pack state through the HAL encoder flags.
    // Scan mode and aspect are independent: 480p may be enabled in 4:3, while
    // widescreen is a logical 16:9 presentation over the same 640x480 buffer.
    const DWORD encoder = XVideoGetEncoderSettings();
    const bool dashboard_wide = (encoder & VIDEO_WIDESCREEN) != 0;
    g_can_480p = (encoder & VIDEO_MODE_480P) != 0;
    g_can_720p = (encoder & VIDEO_MODE_720P) != 0;
    g_widescreen = dashboard_wide;

    g_output_width = 640;
    g_output_height = 480;
    g_progressive = g_can_480p;

    if (g_can_720p) {
        g_output_width = 1280;
        g_output_height = 720;
        g_widescreen = true;
        g_progressive = true;
    }

    bool mode_ok = XVideoSetMode(g_output_width, g_output_height, 32, g_refresh) != FALSE;
    if (!mode_ok && (g_output_width != 640 || g_output_height != 480)) {
        serialPuts("PD-X: 720p mode unavailable; falling back to 480-line output\n");
        g_output_width = 640;
        g_output_height = 480;
        g_widescreen = dashboard_wide;
        g_progressive = g_can_480p;
        mode_ok = XVideoSetMode(g_output_width, g_output_height, 32, g_refresh) != FALSE;
    }
    if (!mode_ok) {
        sysFatalError("No compatible 640x480 Xbox video mode.");
    }

    VIDEO_MODE active_mode = XVideoGetMode();
    if (active_mode.width != g_output_width || active_mode.height != g_output_height) {
        if (g_output_width == 1280 && g_output_height == 720) {
            serialPuts("PD-X: 720p raster mismatch; falling back to 640x480\n");
            g_output_width = 640;
            g_output_height = 480;
            g_widescreen = dashboard_wide;
            g_progressive = g_can_480p;
            mode_ok = XVideoSetMode(g_output_width, g_output_height, 32, g_refresh) != FALSE;
            active_mode = XVideoGetMode();
        }
        if (!mode_ok || active_mode.width != g_output_width
                || active_mode.height != g_output_height) {
            sysFatalError("Xbox video raster mismatch: requested %dx%d, active %dx%d.",
                          g_output_width, g_output_height,
                          active_mode.width, active_mode.height);
        }
    }

    {
        char line[192];
        snprintf(line, sizeof(line),
                 "PD-X: video encoder=0x%08lx dashWide=%d can480p=%d can720p=%d output=%dx%d aspect=%s scan=%s\n",
                 (unsigned long)encoder, dashboard_wide ? 1 : 0,
                 g_can_480p ? 1 : 0, g_can_720p ? 1 : 0,
                 g_output_width, g_output_height,
                 g_widescreen ? "16:9" : "4:3",
                 g_progressive ? "progressive" : "interlaced");
        serialPuts(line);
        sysLogPrintf(LOG_NOTE, "Xbox video: output=%dx%d aspect=%s scan=%s",
                     g_output_width, g_output_height,
                     g_widescreen ? "16:9" : "4:3",
                     g_progressive ? "progressive" : "interlaced");
    }

    // The renderer uses one full-size, non-rotating surface as a scratch render
    // target.  Persistent game framebuffers are copied to/from their own
    // compact texture allocations, so a single extra pbkit surface is enough
    // for menu blur and any future offscreen pass without spending ~1.2 MiB on
    // each of the game's many 16x16 capture buffers.
    // At 720p one scratch surface costs another 3.5 MiB. The optional mode
    // disables framebuffer effects and renders directly to the back buffer so
    // the 16 MiB game heap remains available.
    pb_extra_buffers(gfx_xbox_wm_is_720p() ? 0 : 1);

    // Initialise pbkit (NV2A push-buffer engine)
    int pb_err = pb_init();
    bool pb_geometry_ok = pb_err == 0
        && pb_back_buffer_width() == (DWORD)g_output_width
        && pb_back_buffer_height() == (DWORD)g_output_height;
    // pbkit returns -11 for framebuffer/depth/extra-surface allocation
    // failures and has already called pb_kill() before returning it. Retry
    // only that recoverable memory case, or a successful init whose raster is
    // wrong. Other errors are unrelated to 720p surface pressure and remain
    // fatal; calling pb_kill() again after them may double-tear-down pbkit.
    const bool retry_720p = g_output_width == 1280 && g_output_height == 720
        && (pb_err == -11 || (pb_err == 0 && !pb_geometry_ok));
    if (retry_720p) {
        // Native 720p costs roughly 8 MiB more than the qualified 480-line
        // path before game or replacement textures. Never strand the user at
        // boot if contiguous allocation cannot satisfy that optional tier.
        serialPuts(pb_err != 0
            ? "PD-X: 720p pbkit allocation failed; retrying 640x480\n"
            : "PD-X: 720p pbkit raster mismatch; retrying 640x480\n");
        if (pb_err == 0) {
            pb_kill();
        }
        g_output_width = 640;
        g_output_height = 480;
        g_widescreen = dashboard_wide;
        g_progressive = g_can_480p;
        if (!XVideoSetMode(g_output_width, g_output_height, 32, g_refresh)) {
            sysFatalError("720p fallback could not set 640x480 video mode.");
        }
        pb_extra_buffers(1);
        pb_err = pb_init();
        pb_geometry_ok = pb_err == 0
            && pb_back_buffer_width() == (DWORD)g_output_width
            && pb_back_buffer_height() == (DWORD)g_output_height;
    }
    if (pb_err != 0) {
        sysFatalError("pb_init() failed (err %d).  Cannot initialise GPU.", pb_err);
    }
    if (!pb_geometry_ok) {
        sysFatalError("pbkit raster mismatch: requested %dx%d, active %lux%lu.",
                      g_output_width, g_output_height,
                      (unsigned long)pb_back_buffer_width(),
                      (unsigned long)pb_back_buffer_height());
    }

    active_mode = XVideoGetMode();
    {
        char raster_line[144];
        snprintf(raster_line, sizeof(raster_line),
                 "PD-X: verified raster video=%dx%d pbkit=%lux%lu aspect=%s\n",
                 active_mode.width, active_mode.height,
                 (unsigned long)pb_back_buffer_width(),
                 (unsigned long)pb_back_buffer_height(),
                 g_widescreen ? "16:9" : "4:3");
        serialPuts(raster_line);
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
    // Xbox users cannot override the dashboard-selected mode in-game.
    return 1;
}

static int xbox_wm_get_display_mode(int modenum, int *out_w, int *out_h)
{
    if (modenum == 0) {
        *out_w = g_output_width;
        *out_h = g_output_height;
        return 1;
    }
    return 0;
}

static int xbox_wm_get_current_display_mode(int *out_w, int *out_h)
{
    *out_w = g_output_width;
    *out_h = g_output_height;
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
    *w    = (uint32_t)g_output_width;
    *h    = (uint32_t)g_output_height;
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
    *rr = (uint32_t)g_refresh;
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
