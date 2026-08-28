// NV2A rendering backend for Perfect Dark X (Original Xbox port).
//
// Implements GfxRenderingAPI using NXDK's pbkit push-buffer interface.
// The NV2A GPU in the Xbox is based on the NVidia NV20 (Kelvin) architecture
// and exposes DirectX 8-level fixed-function + programmable pipeline hardware.
//
// Strategy
// ────────
// fast3d feeds us N64 RDP commands that have already been decoded into a
// colour-combiner description (CCFeatures) and a packed float vertex buffer.
// We map those to NV2A hardware state:
//
//   N64 colour combiner  →  NV2A register combiners (RC0–RC7 + final combiner)
//   Textured triangles   →  contiguous vertex arrays + DRAW_ARRAYS
//   Texture objects      →  MmAllocateContiguousMemory VRAM uploads
//   Depth / blend modes  →  NV2A fixed-function state words
//
// CCFeatures is decoded into the exact (A-B)*C+D equation for both N64 cycles.

#ifdef PLATFORM_XBOX

#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cstdio>
#include <cassert>
#include <map>
#include <vector>
#include <utility>

extern "C" {
#include <xboxkrnl/xboxkrnl.h>
#include <hal/debug.h>
#include <pbkit/pbkit.h>
}

#include "gfx_rendering_api.h"
#include "gfx_cc.h"
#include "gfx_pc.h"
#include "gfx_nv2a.h"
#include "platform.h"
#include "system.h"

extern "C" {
#include "../src/xbox/ext_texture_xbox.h"
}

extern "C" void serialPuts(const char *s);

// ── NV097 indexed-register helpers ──────────────────────────────────────────
// In NXDK's nv_regs.h, NV097_SET_COMBINER_* and NV097_SET_TEXTURE_* are plain
// integer constants (base register for stage/tile 0).  Successive stages or
// texture units are at +4-byte or +0x40-byte strides respectively.
// We wrap them in our own macros so call-sites can remain readable.

// Register-combiner stage stride: 4 bytes between consecutive stages
#define NV097_COMBINER_COLOR_ICW(s)  (NV097_SET_COMBINER_COLOR_ICW  + (uint32_t)(s) * 4u)
#define NV097_COMBINER_COLOR_OCW(s)  (NV097_SET_COMBINER_COLOR_OCW  + (uint32_t)(s) * 4u)
#define NV097_COMBINER_ALPHA_ICW(s)  (NV097_SET_COMBINER_ALPHA_ICW  + (uint32_t)(s) * 4u)
#define NV097_COMBINER_ALPHA_OCW(s)  (NV097_SET_COMBINER_ALPHA_OCW  + (uint32_t)(s) * 4u)
#define NV097_COMBINER_FACTOR0(s)    (NV097_SET_COMBINER_FACTOR0     + (uint32_t)(s) * 4u)
#define NV097_COMBINER_FACTOR1(s)    (NV097_SET_COMBINER_FACTOR1     + (uint32_t)(s) * 4u)

// Texture-unit stride: 0x40 bytes between consecutive texture stages
#define NV097_TEXTURE_OFFSET(t)      (NV097_SET_TEXTURE_OFFSET      + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_FORMAT(t)      (NV097_SET_TEXTURE_FORMAT       + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_ADDRESS(t)     (NV097_SET_TEXTURE_ADDRESS      + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_CONTROL0(t)    (NV097_SET_TEXTURE_CONTROL0     + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_CONTROL1(t)    (NV097_SET_TEXTURE_CONTROL1     + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_FILTER(t)      (NV097_SET_TEXTURE_FILTER       + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_IMAGE_RECT(t)  (NV097_SET_TEXTURE_IMAGE_RECT   + (uint32_t)(t) * 0x40u)

// Depth-write enable: some NXDK versions call this NV097_SET_DEPTH_MASK
#ifndef NV097_SET_DEPTH_WRITE_ENABLE
#  ifdef  NV097_SET_DEPTH_MASK
#    define NV097_SET_DEPTH_WRITE_ENABLE NV097_SET_DEPTH_MASK
#  else
#    define NV097_SET_DEPTH_WRITE_ENABLE 0x00000354u  // NV2A depth-write mask register
#  endif
#endif

// Scissor — NV2A exposes per-screen-region window clip, not a dedicated scissor.
// nv_regs.h defines NV097_SET_WINDOW_CLIP_HORIZONTAL/VERTICAL (8 regions).
// We use region 0 for the scissor rectangle.
// The register pack format: bits [11:0]=min, bits [27:16]=max (exclusive).
#ifndef NV097_SET_WINDOW_CLIP_HORIZONTAL
// fallback: known NV2A method addresses
#  define NV097_SET_WINDOW_CLIP_HORIZONTAL 0x000002C0u
#  define NV097_SET_WINDOW_CLIP_VERTICAL   0x000002E0u
#endif
// Also enable scissor/clip type register
#ifndef NV097_SET_WINDOW_CLIP_TYPE
#  define NV097_SET_WINDOW_CLIP_TYPE       0x000002B4u
#endif

// Per-vertex color: nv_regs.h defines *4F (float) and *4I (int) forms.
// The 4UB (packed-byte) form doesn't exist in this NXDK version.
// We use NV097_SET_DIFFUSE_COLOR4F / NV097_SET_SPECULAR_COLOR4F instead.
// If this NXDK version also lacks those, provide numeric fallbacks.
#ifndef NV097_SET_DIFFUSE_COLOR4F
#  define NV097_SET_DIFFUSE_COLOR4F  0x00001550u
#  define NV097_SET_SPECULAR_COLOR4F 0x00001570u
#endif

// ── NV2A push-buffer helpers ─────────────────────────────────────────────────
// pbkit's pb_push* macros take a uint32_t* and advance it.

// Convert float to the bit representation pbkit expects for floats in the push buffer
static inline uint32_t f2u(float f)
{
    uint32_t u;
    memcpy(&u, &f, 4);
    return u;
}

static inline void nv2a_copy_pixels(uint32_t *dst, const uint32_t *src,
                                    uint32_t count)
{
    // pdclib's generic memcpy is byte-oriented. Xbox is always i686 and every
    // framebuffer row is DWORD aligned, so use the CPU's native bulk copy.
    __asm__ volatile ("rep movsl"
                      : "+D"(dst), "+S"(src), "+c"(count)
                      :
                      : "memory");
}

// ── Texture management ───────────────────────────────────────────────────────

// gfx_pc retains as many as 1024 cache entries before recycling an ID.
// Reserve ID zero as the invalid handle, so the backend needs one extra slot.
#define MAX_GAME_TEXTURES 1025
#define MAX_FRAMEBUFFERS  32
#define MAX_TEXTURES      (MAX_GAME_TEXTURES + MAX_FRAMEBUFFERS)
#define TEXTURE_TILE_COUNT 2
#define NV2A_TEXTURE_STAGE_COUNT 4

// pbkit's DMA context 2, 2D, one-level linear/NPOT A8R8G8B8 format. Every
// texture owned or aliased by this backend uses this single hardware layout.
// Keeping it canonical avoids submitting partially initialized cached state.
static const uint32_t NV2A_LINEAR_A8R8G8B8_FORMAT = 0x0001122au;
static const uint32_t NV2A_TEXTURE_PHYSICAL_MASK = 0x03ffffffu;
static const char NV2A_RUNTIME_BUILD_ID[] = "OG720-PDID-AUDIO-28";

struct NV2ATexture {
    bool        used;
    uint32_t   *vram;        // contiguous physical memory for the texture
    uint32_t   *alias_vram;  // non-owning reference to a completed pbkit buffer
    uint32_t    alias_pitch;
    uint32_t    width;
    uint32_t    height;
    uint32_t    storage_width;
    uint32_t    storage_height;
    uint32_t    pitch;
    uint32_t    fmt_word;    // NV097_SET_TEXTURE_FORMAT value cached
    bool        swizzled;
    bool        linear_filter;
    uint8_t     address_s;
    uint8_t     address_t;
    bool        mipmaps;
    uint32_t    content_version;
    bool        external;
    uint32_t    external_id;
};

static inline uint32_t *nv2a_texture_data(const NV2ATexture &texture)
{
    return texture.alias_vram ? texture.alias_vram : texture.vram;
}

static inline uint32_t nv2a_texture_pitch(const NV2ATexture &texture)
{
    return texture.alias_vram ? texture.alias_pitch : texture.pitch;
}

static NV2ATexture g_textures[MAX_TEXTURES];
static int         g_active_texture[TEXTURE_TILE_COUNT] = { -1, -1 };
static int         g_last_selected_tile = 0;

struct NV2AHardwareTextureState {
    bool valid;
    bool enabled;
    uint32_t words[7];
};

static NV2AHardwareTextureState g_hw_texture[NV2A_TEXTURE_STAGE_COUNT];
static bool g_texture_program_valid = false;
static uint32_t g_texture_program = 0;
static uint32_t g_draw_texture_width[TEXTURE_TILE_COUNT] = {};
static uint32_t g_draw_texture_height[TEXTURE_TILE_COUNT] = {};
static int g_draw_texture_id[TEXTURE_TILE_COUNT] = { -1, -1 };
static bool g_draw_texture_external[TEXTURE_TILE_COUNT] = {};
static uint32_t g_draw_texture_external_id[TEXTURE_TILE_COUNT] = {
    UINT32_MAX, UINT32_MAX
};

static NV2ATexture g_blur_texture = {};
static int g_blur_source_id = -1;
static uint32_t g_blur_source_version = 0;
static NV2ATexture g_noise_texture = {};

struct NV2AStreamVertex {
    float position[4];
    uint32_t diffuse;
    uint32_t specular;
    float tex0[2];
    float tex1[2];
};

#define NV2A_STREAM_VERTEX_CAPACITY 65536u
static NV2AStreamVertex *g_stream_vertices = nullptr;
static uint32_t g_stream_vertex_cursor = 0;
static bool g_stream_attributes_bound = false;

static inline bool nv2a_is_power_of_two(uint32_t value)
{
    return value && (value & (value - 1u)) == 0;
}

static uint32_t nv2a_log2_power_of_two(uint32_t value)
{
    uint32_t result = 0;
    while (value > 1u) {
        value >>= 1;
        ++result;
    }
    return result;
}

static uint32_t nv2a_swizzled_a8r8g8b8_format(uint32_t width,
                                               uint32_t height)
{
    // DMA selector 2 is pbkit's full 64 MiB DMA-B object. Swizzled 2D
    // A8R8G8B8 encodes its power-of-two dimensions in FORMAT rather than in
    // the LU_IMAGE rectangle alone.
    return 2u | (1u << 3) | (2u << 4) | (0x06u << 8) | (1u << 16) |
           (nv2a_log2_power_of_two(width) << 20) |
           (nv2a_log2_power_of_two(height) << 24);
}

static void nv2a_generate_swizzle_masks(uint32_t width, uint32_t height,
                                         uint32_t &mask_x, uint32_t &mask_y)
{
    mask_x = mask_y = 0;
    uint32_t dimension_bit = 1;
    uint32_t address_bit = 1;
    bool done;
    do {
        done = true;
        if (dimension_bit < width) {
            mask_x |= address_bit;
            address_bit <<= 1;
            done = false;
        }
        if (dimension_bit < height) {
            mask_y |= address_bit;
            address_bit <<= 1;
            done = false;
        }
        dimension_bit <<= 1;
    } while (!done);
}

static inline uint32_t nv2a_spread_swizzle_bits(uint32_t value,
                                                uint32_t mask)
{
    uint32_t result = 0;
    uint32_t source_bit = 1;
    for (uint32_t destination_bit = 1; destination_bit &&
         destination_bit <= mask; destination_bit <<= 1) {
        if (!(mask & destination_bit)) continue;
        if (value & source_bit) result |= destination_bit;
        source_bit <<= 1;
    }
    return result;
}

static inline uint32_t nv2a_swizzled_pixel_index(uint32_t x, uint32_t y,
                                                  uint32_t width,
                                                  uint32_t height)
{
    uint32_t mask_x, mask_y;
    nv2a_generate_swizzle_masks(width, height, mask_x, mask_y);
    return nv2a_spread_swizzle_bits(x, mask_x) |
           nv2a_spread_swizzle_bits(y, mask_y);
}

static uint32_t nv2a_alloc_texture_id(void)
{
    for (uint32_t i = 1; i < MAX_GAME_TEXTURES; ++i) {
        if (!g_textures[i].used) {
            g_textures[i].used = true;
            return i;
        }
    }
    sysLogPrintf(LOG_ERROR, "NV2A: texture pool exhausted");
    return 0;
}

// ── Shader / colour-combiner ─────────────────────────────────────────────────
// We store precomputed NV2A register-combiner state per colour-combiner key.

// NV2A register-combiner input register codes (NV097_SET_COMBINER_COLOR_ICW bits 4..7)
#define NV2A_REG_ZERO        0x00
#define NV2A_REG_FOG         0x03
#define NV2A_REG_DIFFUSE     0x04  // GL_PRIMARY_COLOR_NV (vertex diffuse)
#define NV2A_REG_SPECULAR    0x05  // GL_SECONDARY_COLOR_NV (fog / secondary)
#define NV2A_REG_TEX0        0x08
#define NV2A_REG_TEX1        0x09
#define NV2A_REG_TEX2        0x0A
#define NV2A_REG_CONST0      0x01  // GL_CONSTANT_COLOR0_NV (prim colour)
#define NV2A_REG_CONST1      0x02  // GL_CONSTANT_COLOR1_NV (env colour)
#define NV2A_REG_COMBINED    0x0C  // SPARE0, output of the previous stage
#define NV2A_REG_SPARE1      0x0D

// NV2A map (alpha modifier) codes used in ICW
#define NV2A_MAP_UNSIGNED_ID  0x0  // A → A
#define NV2A_MAP_UNSIGNED_INV 0x1  // A → 1 − A
#define NV2A_MAP_EXPAND_NORMAL 0x2  // A → 2A − 1
#define NV2A_MAP_SIGNED_NEG   0x7  // A → −A

// Build one ICW ABCD input block:  A_MAP, A_REG, B_MAP, B_REG, C_MAP, C_REG, D_MAP, D_REG
// Store AB+CD in SPARE0, the register carried between combiner stages.
#define NV2A_OCW_DEFAULT  0x00000C00  // write AB+CD to SPARE0

struct ShaderProgram {
    uint64_t        shader_id0;
    uint32_t        shader_id1;
    uint8_t         num_inputs;
    bool            used_textures[TEXTURE_TILE_COUNT];
    bool            clamp[TEXTURE_TILE_COUNT][2];
    bool            opt_fog;
    bool            opt_grayscale;
    bool            opt_alpha;
    uint8_t         num_floats;   // vertex stride in floats
    CCFeatures      cc;
};

static std::map<std::pair<uint64_t,uint32_t>, ShaderProgram> g_shader_pool;
static ShaderProgram *g_current_shader = nullptr;

// ── Render state ─────────────────────────────────────────────────────────────

static struct {
    int   vp_x, vp_y, vp_w, vp_h;
    int   sc_x, sc_y, sc_w, sc_h;
    bool  depth_test;
    bool  depth_write;
    float znear;
    float zfar;
    bool  use_alpha;
    bool  alpha_modulate;
} g_rs = { 0, 0, 640, 480, 0, 0, 640, 480, true, true, 0.0f, 1.0f, false, false };

static void nv2a_init_passthrough_shader(void);
static void nv2a_upload_viewport_constants(void);
static void nv2a_bind_texture(int tile, NV2ATexture &texture);
static NV2ATexture *nv2a_prepare_blur_texture(int source_id);

static void nv2a_init_noise_texture(void)
{
    const uint32_t width = 256;
    const uint32_t height = 256;
    const uint32_t pitch = width * 4u;
    g_noise_texture = {};
    g_noise_texture.vram = (uint32_t *)MmAllocateContiguousMemoryEx(
        pitch * height, 0, 0xFFFFFFFF, 0,
        PAGE_READWRITE | PAGE_WRITECOMBINE);
    if (!g_noise_texture.vram) {
        sysFatalError("NV2A: unable to allocate screen-noise texture");
    }

    uint32_t mask_x, mask_y;
    nv2a_generate_swizzle_masks(width, height, mask_x, mask_y);
    uint32_t offset_y = 0;
    for (uint32_t y = 0; y < height; ++y) {
        uint32_t offset_x = 0;
        for (uint32_t x = 0; x < width; ++x) {
            // Stable integer avalanche: all channels receive the same uniform
            // value, matching the scalar random() used by the PC fragment
            // shader for NOISE and alpha coverage.
            uint32_t hash = x * 0x9e3779b9u ^ y * 0x85ebca6bu ^ 0xc2b2ae35u;
            hash ^= hash >> 16;
            hash *= 0x7feb352du;
            hash ^= hash >> 15;
            hash *= 0x846ca68bu;
            hash ^= hash >> 16;
            const uint32_t value = hash & 255u;
            g_noise_texture.vram[offset_x + offset_y] =
                value * 0x01010101u;
            offset_x = (offset_x - mask_x) & mask_x;
        }
        offset_y = (offset_y - mask_y) & mask_y;
    }

    g_noise_texture.used = true;
    g_noise_texture.width = width;
    g_noise_texture.height = height;
    g_noise_texture.storage_width = width;
    g_noise_texture.storage_height = height;
    g_noise_texture.pitch = pitch;
    g_noise_texture.fmt_word =
        nv2a_swizzled_a8r8g8b8_format(width, height);
    g_noise_texture.swizzled = true;
    g_noise_texture.linear_filter = false;
    g_noise_texture.address_s = 1; // repeat
    g_noise_texture.address_t = 1;
    g_noise_texture.mipmaps = false;
    g_noise_texture.content_version = 1;
}

static void nv2a_program_blend_state(bool texture_edge)
{
    uint32_t *p = pb_begin();
    // The PC texture-edge fragment path discards alpha <= 0.19 and forces
    // every survivor to alpha 1. Fixed-function NV2A cannot alpha-test one
    // value and then replace it, so an opaque write is the exact RGB result.
    if (g_rs.use_alpha && !texture_edge) {
        p = pb_push1(p, NV097_SET_BLEND_ENABLE, 1);
        if (g_rs.alpha_modulate) {
            p = pb_push1(p, NV097_SET_BLEND_FUNC_SFACTOR,
                         NV097_SET_BLEND_FUNC_SFACTOR_V_DST_COLOR);
            p = pb_push1(p, NV097_SET_BLEND_FUNC_DFACTOR,
                         NV097_SET_BLEND_FUNC_DFACTOR_V_ZERO);
        } else {
            p = pb_push1(p, NV097_SET_BLEND_FUNC_SFACTOR,
                         NV097_SET_BLEND_FUNC_SFACTOR_V_SRC_ALPHA);
            p = pb_push1(p, NV097_SET_BLEND_FUNC_DFACTOR,
                         NV097_SET_BLEND_FUNC_DFACTOR_V_ONE_MINUS_SRC_ALPHA);
        }
        p = pb_push1(p, NV097_SET_BLEND_EQUATION, 0x8006); // GL_FUNC_ADD
    } else {
        p = pb_push1(p, NV097_SET_BLEND_ENABLE, 0);
    }
    pb_end(p);
}

// Real NV2A requires both perspective bits to remain enabled for the
// pre-transformed vertex stream used by this backend.  Xemu treats texture
// perspective as effectively always-on, which hid this state leak: writing
// zero here left hardware textured draws producing only their non-texture
// combiner inputs.  Stencil passes may toggle bit 0, but must preserve the
// perspective contract.
static const uint32_t NV2A_CONTROL0_BASE =
    NV097_SET_CONTROL0_Z_PERSPECTIVE_ENABLE |
    NV097_SET_CONTROL0_TEXTURE_PERSPECTIVE_ENABLE;

static inline uint32_t nv2a_control0(bool stencil_write)
{
    return NV2A_CONTROL0_BASE |
        (stencil_write ? NV097_SET_CONTROL0_STENCIL_WRITE_ENABLE : 0u);
}

static uint32_t g_frame_count = 0;
static bool g_logged_stock_texture_proof = false;
static bool g_logged_external_texture_proof = false;
static bool g_logged_shader_abi_upload = false;
static uint32_t g_shader_abi_upload_serial = 0;
static ULONGLONG g_perf_frame_started = 0;
static ULONGLONG g_perf_submit_ticks = 0;
static ULONGLONG g_perf_gpu_drain_ticks = 0;
static ULONGLONG g_perf_queue_ticks = 0;
static ULONGLONG g_perf_submit_max = 0;
static ULONGLONG g_perf_gpu_drain_max = 0;
static ULONGLONG g_perf_queue_max = 0;
static DWORD g_perf_previous_vbl = 0;
static DWORD g_perf_vbl_total = 0;
static DWORD g_perf_vbl_max = 0;
static unsigned g_perf_frame_samples = 0;

#if defined(PD_XBOX_GPU_SYNC_TRACE)
// The hardware trace is intentionally self-disabling. It isolates the first
// textured draw (the command that used to fault) and then lets the title run
// normally; tracing every pushbuffer submission makes a single boot frame take
// minutes on an Xbox and can be mistaken for a 720p performance problem.
static bool g_gpu_sync_trace_active = true;
#endif

static uint32_t nv2a_mmio_read(uint32_t offset)
{
    return *(volatile uint32_t *)(uintptr_t)(0xFD000000u + offset);
}

static void nv2a_dump_gpu_timeout(const char *where, uint32_t block,
                                  int source_line, const uint32_t *end)
{
    sysLogPrintf(LOG_ERROR,
                 "NV2A TIMEOUT where=%s block=%lu line=%d end=%p",
                 where, (unsigned long)block, source_line, end);
    sysLogPrintf(LOG_ERROR,
                 "NV2A MMIO pmc=%08lx pfifo_intr=%08lx pfifo_status=%08lx put=%08lx get=%08lx",
                 (unsigned long)nv2a_mmio_read(0x00000100u),
                 (unsigned long)nv2a_mmio_read(0x00002100u),
                 (unsigned long)nv2a_mmio_read(0x00003214u),
                 (unsigned long)nv2a_mmio_read(0x00003240u),
                 (unsigned long)nv2a_mmio_read(0x00003244u));
    sysLogPrintf(LOG_ERROR,
                 "NV2A MMIO pgraph_intr=%08lx nsource=%08lx status=%08lx trapped=%08lx data=%08lx:%08lx",
                 (unsigned long)nv2a_mmio_read(0x00400100u),
                 (unsigned long)nv2a_mmio_read(0x00400108u),
                 (unsigned long)nv2a_mmio_read(0x00400700u),
                 (unsigned long)nv2a_mmio_read(0x00400704u),
                 (unsigned long)nv2a_mmio_read(0x0040070Cu),
                 (unsigned long)nv2a_mmio_read(0x00400708u));

    if (end) {
        for (int i = -16; i < 0; i += 4) {
            sysLogPrintf(LOG_ERROR,
                         "NV2A PUSH %p: %08lx %08lx %08lx %08lx",
                         end + i,
                         (unsigned long)end[i + 0],
                         (unsigned long)end[i + 1],
                         (unsigned long)end[i + 2],
                         (unsigned long)end[i + 3]);
        }
    }
}

static bool nv2a_wait_for_idle_trace(const char *where, uint32_t block,
                                     int source_line, const uint32_t *end)
{
    const ULONGLONG start = KeQueryPerformanceCounter();
    const ULONGLONG timeout = KeQueryPerformanceFrequency() * 2u;

    while (pb_busy()) {
        if (KeQueryPerformanceCounter() - start >= timeout) {
            nv2a_dump_gpu_timeout(where, block, source_line, end);
            return false;
        }
    }
    return true;
}

#if defined(PD_XBOX_GPU_SYNC_TRACE)
static void nv2a_pb_end_sync_trace(uint32_t *end, int source_line)
{
    static uint32_t block = 0;
    const bool trace = g_frame_count <= 1 && g_gpu_sync_trace_active;
    const uint32_t current = ++block;
    if (trace) {
        sysLogPrintf(LOG_NOTE, "NV2A SYNC %lu line=%d begin",
                     (unsigned long)current, source_line);
    }
    pb_end(end);
    if (trace) {
        if (!nv2a_wait_for_idle_trace("pb_end", current, source_line, end)) {
            // Preserve the failed command stream and register state. Continuing
            // to submit methods after a GPU timeout destroys the useful fault.
            for (;;) {
            }
        }
        sysLogPrintf(LOG_NOTE, "NV2A SYNC %lu line=%d complete",
                     (unsigned long)current, source_line);
    }
}
#define pb_end(end) nv2a_pb_end_sync_trace((end), __LINE__)
#endif

struct NV2ACombinerCacheKey {
    uint64_t shader_id0;
    uint32_t shader_id1;
    uint8_t rgb_reg[7];
    uint8_t alpha_reg[7];
    uint32_t const_color[2];
    uint32_t fog_color;
    uint32_t grayscale_color;
    uint8_t flags;
    uint8_t alpha_mode;
    uint8_t alpha_ref;
};

static bool g_combiner_cache_valid = false;
static NV2ACombinerCacheKey g_combiner_cache = {};
static unsigned g_dbg_combiner_programs = 0;
static unsigned g_dbg_texture_binds = 0;
static unsigned g_dbg_fb_copy_calls = 0;
static unsigned g_dbg_fb_copy_fast = 0;
static unsigned g_dbg_fb_copy_alias = 0;
static unsigned g_dbg_fb_copy_scaled = 0;
static unsigned g_dbg_fb_copy_box = 0;
static uint64_t g_dbg_fb_copy_pixels = 0;
static unsigned g_dbg_noise_stencil_draws = 0;
static uint32_t g_noise_stencil_ref = 0;

// ── Framebuffer ───────────────────────────────────────────────────────────────
// pbkit owns the rotating display buffers. One non-rotating extra buffer is a
// scratch render target; each logical PC framebuffer has compact persistent
// texture storage so all 24 objects used by Perfect Dark remain independent.

#define FB_BACK_BUFFER 0

struct NV2AFramebuffer {
    bool used;
    bool invert_y;
    bool render_target;
    bool has_depth;
    uint32_t width;
    uint32_t height;
    uint32_t texture_id;
};

static NV2AFramebuffer g_framebuffers[MAX_FRAMEBUFFERS];
static int g_framebuffer_count = 1;
static int g_current_framebuffer = FB_BACK_BUFFER;
static uint32_t g_target_width = 640;
static uint32_t g_target_height = 480;
static float g_noise_scale = 1.0f;

static FilteringMode       g_filter_mode = FILTER_LINEAR;
static MipmapFilteringMode g_mipmap_mode = MIPMAP_DISABLED;
static int                 g_anisotropy = 1;
static bool                g_clip_invert_y = false;

// ── GfxRenderingAPI implementation ───────────────────────────────────────────

static const char *nv2a_get_name(void)
{
    return "NV2A (pbkit)";
}

static int nv2a_get_max_texture_size(void)
{
    // NV2A supports up to 4096, but on the 64 MB Xbox this value also sizes
    // gfx_pc's tex_upload_buffer (max_tex_size^2 * 4 bytes): 2048 -> 16 MB,
    // 1024 -> 4 MB. PD's N64 textures are TMEM-bound (<=4 KB); the only large
    // case is framebuffer-as-texture at 640x480, which fits comfortably in 1024.
    return 1024;
}

static struct GfxClipParameters nv2a_get_clip_parameters(void)
{
    // NV2A uses [0,1] for Z (after projection) and Y is top-down
    return { true, g_clip_invert_y };
}

// ── Shader API ────────────────────────────────────────────────────────────────

static void nv2a_unload_shader(struct ShaderProgram *old_prg)
{
    (void)old_prg;
    // No per-draw allocation to free
}

static void nv2a_load_shader(struct ShaderProgram *new_prg)
{
    g_current_shader = new_prg;
}

static struct ShaderProgram *nv2a_create_and_load_new_shader(uint64_t id0, uint32_t id1)
{
    CCFeatures cc;
    gfx_cc_get_features(id0, id1, &cc);

    // Shader programs are cached by gfx_pc, so this emits exactly one line for
    // each distinct RDP combiner seen.  Keep the packed sources in the log: the
    // release renderer must cover the modes the game actually submits rather
    // than silently falling back to texture * diffuse.
    sysLogPrintf(LOG_NOTE,
                 "NV2A_CC %08lx%08lx opt=%08lx inputs=%d tex=%d%d "
                 "rgb0=%x%x%x%x a0=%x%x%x%x rgb1=%x%x%x%x a1=%x%x%x%x",
                 (unsigned long)(id0 >> 32), (unsigned long)id0,
                 (unsigned long)id1, cc.num_inputs,
                 cc.used_textures[0] ? 1 : 0, cc.used_textures[1] ? 1 : 0,
                 cc.c[0][0][0], cc.c[0][0][1], cc.c[0][0][2], cc.c[0][0][3],
                 cc.c[0][1][0], cc.c[0][1][1], cc.c[0][1][2], cc.c[0][1][3],
                 cc.c[1][0][0], cc.c[1][0][1], cc.c[1][0][2], cc.c[1][0][3],
                 cc.c[1][1][0], cc.c[1][1][1], cc.c[1][1][2], cc.c[1][1][3]);

    ShaderProgram prg = {};
    prg.shader_id0 = id0;
    prg.shader_id1 = id1;
    prg.used_textures[0] = cc.used_textures[0];
    prg.used_textures[1] = cc.used_textures[1];
    memcpy(prg.clamp, cc.clamp, sizeof(prg.clamp));
    prg.opt_fog = cc.opt_fog;
    prg.opt_grayscale = cc.opt_grayscale;
    prg.opt_alpha = cc.opt_alpha;
    prg.num_inputs = cc.num_inputs;
    prg.cc = cc;

    // Calculate vertex stride:
    // [x,y,z,w] always present (4 floats)
    // [s,t] per used texture (2 floats each)
    // [r,g,b] or [r,g,b,a] per combiner input
    // [fog r,g,b,a] if fog enabled (4 floats)
    uint8_t nf = 4; // position
    for (int t = 0; t < TEXTURE_TILE_COUNT; ++t) {
        if (cc.used_textures[t]) {
            nf += 2;
            nf += cc.clamp[t][0] ? 1 : 0;
            nf += cc.clamp[t][1] ? 1 : 0;
        }
    }
    nf += (uint8_t)(cc.num_inputs * (cc.opt_alpha ? 4 : 3));
    if (cc.opt_fog)       nf += 4;
    if (cc.opt_grayscale) nf += 4;
    prg.num_floats = nf;

    auto key = std::make_pair(id0, id1);
    g_shader_pool[key] = prg;
    g_current_shader = &g_shader_pool[key];
    return g_current_shader;
}

static struct ShaderProgram *nv2a_lookup_shader(uint64_t id0, uint32_t id1)
{
    auto it = g_shader_pool.find(std::make_pair(id0, id1));
    if (it != g_shader_pool.end()) {
        return &it->second;
    }
    return nullptr;
}

static void nv2a_shader_get_info(struct ShaderProgram *prg,
                                  uint8_t *num_inputs, bool used_textures[2])
{
    *num_inputs       = prg->num_inputs;
    used_textures[0]  = prg->used_textures[0];
    used_textures[1]  = prg->used_textures[1];
}

static void nv2a_clear_shaders(void)
{
    g_shader_pool.clear();
    g_current_shader = nullptr;
}

// ── Texture API ───────────────────────────────────────────────────────────────

static uint32_t nv2a_new_texture(void)
{
    return nv2a_alloc_texture_id();
}

static uint32_t nv2a_filter_word(bool source_linear)
{
    // NV097 encodes minification in bits 16..23 and magnification in 24..27:
    //   1 = box/nearest, 2 = tent/linear.
    // Real NV2A also requires the normal filter-mode field at bit 13. XEMU
    // accepted zero there, but hardware rejects the entire 0x1B14 method as
    // invalid data. The separate convolution state used by pbkit's AA sample
    // is 0x04074000 and visibly smears these textures; 0x?0?2000 is the
    // hardware-safe ordinary box/tent state. LU_IMAGE textures have one
    // physical level, so LOD0 nearest/bilinear is the complete state.
    // FILTER_THREE_POINT normally performs its third tap in a fragment shader;
    // bilinear is the closest fixed-function reconstruction on this backend.
    const bool linear = source_linear && g_filter_mode != FILTER_NONE;
    return linear ? 0x02022000u : 0x01012000u;
}

static void nv2a_disable_texture(int tile)
{
    NV2AHardwareTextureState &hw = g_hw_texture[tile];
    if (hw.valid && !hw.enabled) return;

    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_TEXTURE_FORMAT(tile),
                 NV2A_LINEAR_A8R8G8B8_FORMAT);
    p = pb_push1(p, NV097_TEXTURE_CONTROL0(tile), 0);
    pb_end(p);
    hw = {};
    hw.valid = true;
}

static void nv2a_bind_texture(int tile, NV2ATexture &t)
{
    uint32_t *data = nv2a_texture_data(t);
    const uint32_t pitch = nv2a_texture_pitch(t);
    const uint32_t storage_width = t.storage_width
        ? t.storage_width : t.width;
    const uint32_t storage_height = t.storage_height
        ? t.storage_height : t.height;
    const uint32_t offset = (uint32_t)(uintptr_t)data &
                            NV2A_TEXTURE_PHYSICAL_MASK;
    const uint64_t byte_count =
        (uint64_t)pitch * (uint64_t)storage_height;
    const uint32_t expected_format = t.swizzled
        ? nv2a_swizzled_a8r8g8b8_format(storage_width, storage_height)
        : NV2A_LINEAR_A8R8G8B8_FORMAT;

    // LU_IMAGE state is unusually strict on real NV2A hardware. Reject a bad
    // object before its FORMAT method can halt PGRAPH; XEMU is more forgiving
    // of several of these invalid combinations.
    const bool common_valid = data && t.width && t.height &&
        t.width <= 4096u && t.height <= 4096u &&
        storage_width && storage_height &&
        storage_width <= 4096u && storage_height <= 4096u &&
        pitch >= storage_width * 4u && pitch <= 0xffffu &&
        (offset & 63u) == 0u &&
        byte_count <= 0x04000000ull &&
        (uint64_t)offset + byte_count <= 0x04000000ull;
    const bool layout_valid = t.swizzled
        ? nv2a_is_power_of_two(storage_width) &&
          nv2a_is_power_of_two(storage_height)
        : (pitch & 63u) == 0u;
    const bool valid = common_valid && layout_valid;
    if (!valid) {
        sysLogPrintf(LOG_ERROR,
                     "NV2A TEX INVALID tile=%d ptr=%p off=%08lx fmt=%08lx pitch=%lu size=%lux%lu storage=%lux%lu swz=%u bytes=%llu",
                     tile, data, (unsigned long)offset,
                     (unsigned long)t.fmt_word, (unsigned long)pitch,
                     (unsigned long)t.width, (unsigned long)t.height,
                     (unsigned long)storage_width,
                     (unsigned long)storage_height,
                     t.swizzled ? 1u : 0u,
                     (unsigned long long)byte_count);
        nv2a_disable_texture(tile);
        return;
    }

    if (t.fmt_word != expected_format) {
        sysLogPrintf(LOG_ERROR,
                     "NV2A TEX FORMAT corrected tile=%d old=%08lx new=%08lx",
                     tile, (unsigned long)t.fmt_word,
                     (unsigned long)expected_format);
        t.fmt_word = expected_format;
    }

    uint32_t words[7];
    words[0] = offset;
    words[1] = expected_format;
    words[2] = pitch << 16;
    words[3] = (storage_width << 16) | storage_height;
    // NV2A LU_IMAGE textures are unnormalized NPOT surfaces and only support
    // clamp addressing. Repeating/mirrored N64 textures use the swizzled path.
    const uint8_t address_s = t.swizzled ? t.address_s : 3u;
    const uint8_t address_t = t.swizzled ? t.address_t : 3u;
    words[4] = ((uint32_t)address_s << 0) |
               ((uint32_t)address_t << 8) | (3u << 16);
    words[5] = nv2a_filter_word(t.linear_filter);
    words[6] = 0x4003ffc0u;
    if (g_anisotropy > 1 && t.mipmaps) words[6] |= 0x10u;

    NV2AHardwareTextureState &hw = g_hw_texture[tile];
    if (hw.valid && hw.enabled &&
        memcmp(hw.words, words, sizeof(words)) == 0) {
        return;
    }

#if defined(PD_XBOX_GPU_SYNC_TRACE)
    if (g_frame_count <= 1 && g_gpu_sync_trace_active) {
        sysLogPrintf(LOG_NOTE,
                     "NV2A TEXBIND tile=%d ptr=%p off=%08lx fmt=%08lx pitch=%lu size=%lux%lu storage=%lux%lu swz=%u addr=%08lx filter=%08lx ctl=%08lx",
                     tile, data, (unsigned long)words[0],
                     (unsigned long)words[1], (unsigned long)pitch,
                     (unsigned long)t.width, (unsigned long)t.height,
                     (unsigned long)storage_width,
                     (unsigned long)storage_height,
                     t.swizzled ? 1u : 0u,
                     (unsigned long)words[4], (unsigned long)words[5],
                     (unsigned long)words[6]);

        // OFFSET and FORMAT are one incrementing packet in pbkit's known-good
        // hardware sequence. Split every remaining method so a hardware log
        // identifies the exact rejected word without another broad iteration.
        uint32_t *p = pb_begin();
        p = pb_push2(p, NV097_TEXTURE_OFFSET(tile), words[0], words[1]);
        pb_end(p);

        sysLogPrintf(LOG_NOTE, "NV2A TEXMETHOD CONTROL1=%08lx",
                     (unsigned long)words[2]);
        p = pb_begin();
        p = pb_push1(p, NV097_TEXTURE_CONTROL1(tile), words[2]);
        pb_end(p);

        sysLogPrintf(LOG_NOTE, "NV2A TEXMETHOD RECT=%08lx",
                     (unsigned long)words[3]);
        p = pb_begin();
        p = pb_push1(p, NV097_TEXTURE_IMAGE_RECT(tile), words[3]);
        pb_end(p);

        sysLogPrintf(LOG_NOTE, "NV2A TEXMETHOD ADDRESS=%08lx",
                     (unsigned long)words[4]);
        p = pb_begin();
        p = pb_push1(p, NV097_TEXTURE_ADDRESS(tile), words[4]);
        pb_end(p);

        sysLogPrintf(LOG_NOTE, "NV2A TEXMETHOD CONTROL0=%08lx",
                     (unsigned long)words[6]);
        p = pb_begin();
        p = pb_push1(p, NV097_TEXTURE_CONTROL0(tile), words[6]);
        pb_end(p);

        sysLogPrintf(LOG_NOTE, "NV2A TEXMETHOD FILTER=%08lx",
                     (unsigned long)words[5]);
        p = pb_begin();
        p = pb_push1(p, NV097_TEXTURE_FILTER(tile), words[5]);
        pb_end(p);
    } else
#endif
    {
        // Match pbkit's hardware-tested mesh sample: OFFSET+FORMAT are a
        // single incrementing packet, followed by NPOT geometry, wrapping,
        // enable, then filtering.
        uint32_t *p = pb_begin();
        p = pb_push2(p, NV097_TEXTURE_OFFSET(tile), words[0], words[1]);
        p = pb_push1(p, NV097_TEXTURE_CONTROL1(tile), words[2]);
        p = pb_push1(p, NV097_TEXTURE_IMAGE_RECT(tile), words[3]);
        p = pb_push1(p, NV097_TEXTURE_ADDRESS(tile), words[4]);
        p = pb_push1(p, NV097_TEXTURE_CONTROL0(tile), words[6]);
        p = pb_push1(p, NV097_TEXTURE_FILTER(tile), words[5]);
        pb_end(p);
    }
    ++g_dbg_texture_binds;
    hw.valid = true;
    hw.enabled = true;
    memcpy(hw.words, words, sizeof(words));
}

static void nv2a_select_texture(int tile, uint32_t texture_id, bool linear_filter)
{
    if (tile < 0 || tile >= TEXTURE_TILE_COUNT) return;
    g_active_texture[tile] = (int)texture_id;
    g_last_selected_tile = tile;

    if (texture_id == 0 || texture_id >= MAX_TEXTURES ||
        !g_textures[texture_id].used || !g_textures[texture_id].vram) {
        nv2a_disable_texture(tile);
        return;
    }

    NV2ATexture &t = g_textures[texture_id];
    t.linear_filter = linear_filter;
    nv2a_bind_texture(tile, t);
}

static void nv2a_upload_texture(const uint8_t *rgba32_buf,
                                 uint32_t width, uint32_t height,
                                 bool gen_mipmaps)
{
    static bool traced_first_upload = false;
    const bool trace_upload = !traced_first_upload || g_XboxExtTextureUploadTrace;
    if (trace_upload) {
        const char *kind = g_XboxExtTextureUploadTrace ? "external" : "first";
        traced_first_upload = true;
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE 1 %s upload begin %ux%u", kind, width, height);
    }
    // gfx_pc selects a tile immediately before uploading into it.
    const int tile = g_last_selected_tile;
    int tex_id = g_active_texture[tile];
    if (tex_id <= 0 || tex_id >= MAX_TEXTURES) return;

    NV2ATexture &t = g_textures[tex_id];
    t.alias_vram = nullptr;
    t.alias_pitch = 0;
    t.external = g_XboxExtTextureUploadActive != 0;
    t.external_id = t.external ? g_XboxExtTextureUploadId : UINT32_MAX;

    // Swizzled textures are the NV2A-native path for wrap/mirror sampling.
    // N64 TMEM images are normally power-of-two; keep genuinely NPOT images
    // linear and hardware-clamped. One-pixel dimensions are duplicated to a
    // 2D swizzled surface so the 2D projective stage remains valid.
    const bool swizzled = nv2a_is_power_of_two(width) &&
                          nv2a_is_power_of_two(height);
    const uint32_t storage_width = swizzled && width < 2u ? 2u : width;
    const uint32_t storage_height = swizzled && height < 2u ? 2u : height;
    const uint32_t pitch = swizzled
        ? storage_width * 4u
        : (width * 4u + 63u) & ~63u;
    const uint32_t bytes = pitch * storage_height;
    if (trace_upload) {
        sysLogPrintf(LOG_NOTE,
                     "NV2A HWTRACE 2 allocation bytes=%u pitch=%u storage=%ux%u swz=%u",
                     bytes, pitch, storage_width, storage_height,
                     swizzled ? 1u : 0u);
    }

    // Free old allocation if size changed
    if (t.vram) {
        MmFreeContiguousMemory(t.vram);
        t.vram = nullptr;
    }

    // Allocate contiguous physical memory for the texture
    // NV2A DMA requires physically contiguous pages
    t.vram = (uint32_t *)MmAllocateContiguousMemoryEx(
        bytes,
        0,            // lowest acceptable physical address
        0xFFFFFFFF,   // highest acceptable
        0,            // no alignment requirement beyond page
        PAGE_READWRITE | PAGE_WRITECOMBINE);
    if (trace_upload) {
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE 3 allocation=%p", t.vram);
    }

    if (!t.vram) {
        sysLogPrintf(LOG_ERROR, "NV2A: MmAllocateContiguousMemory failed (%ux%u)", width, height);
        t.used = false;
        t.width = 0;
        t.height = 0;
        t.pitch = 0;
        t.fmt_word = 0;
        g_active_texture[tile] = -1;
        nv2a_disable_texture(tile);
        g_texture_program_valid = false;
        return;
    }

    // Convert RGBA to the A8R8G8B8 byte layout used by NV2A. Native textures
    // are Morton/Z ordered; linear NPOT surfaces retain their padded pitch.
    if (swizzled) {
        uint32_t mask_x, mask_y;
        nv2a_generate_swizzle_masks(storage_width, storage_height,
                                    mask_x, mask_y);
        uint32_t offset_y = 0;
        for (uint32_t y = 0; y < storage_height; ++y) {
            const uint32_t source_y = y < height ? y : height - 1u;
            uint32_t offset_x = 0;
            for (uint32_t x = 0; x < storage_width; ++x) {
                const uint32_t source_x = x < width ? x : width - 1u;
                const uint8_t *src = rgba32_buf +
                    (source_y * width + source_x) * 4u;
                uint8_t *dst = (uint8_t *)t.vram +
                    (offset_x + offset_y) * 4u;
                dst[0] = src[2];
                dst[1] = src[1];
                dst[2] = src[0];
                dst[3] = src[3];
                offset_x = (offset_x - mask_x) & mask_x;
            }
            offset_y = (offset_y - mask_y) & mask_y;
        }
    } else {
        for (uint32_t y = 0; y < height; ++y) {
            const uint8_t *src = rgba32_buf + y * width * 4u;
            uint8_t *dst = (uint8_t *)t.vram + y * pitch;
            for (uint32_t x = 0; x < width; ++x) {
                const uint8_t r = src[0], g = src[1];
                const uint8_t b = src[2], a = src[3];
                dst[0] = b;
                dst[1] = g;
                dst[2] = r;
                dst[3] = a;
                src += 4;
                dst += 4;
            }
            memset(dst, 0, pitch - width * 4u);
        }
    }
    if (trace_upload) {
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE 4 conversion complete");
    }

    t.width  = width;
    t.height = height;
    t.storage_width = storage_width;
    t.storage_height = storage_height;
    t.pitch  = pitch;
    t.swizzled = swizzled;
    if (!t.address_s) t.address_s = 3;
    if (!t.address_t) t.address_t = 3;
    // Linear NPOT textures cannot carry a conventional mip chain. Remember
    // the request so anisotropy/filter state remains coherent while sampling
    // the base level, which is the only level N64 TMEM uploads require here.
    t.mipmaps = gen_mipmaps;
    ++t.content_version;

    t.fmt_word = swizzled
        ? nv2a_swizzled_a8r8g8b8_format(storage_width, storage_height)
        : NV2A_LINEAR_A8R8G8B8_FORMAT;

    // The initial select happens before the allocation exists and therefore
    // disables the unit. Rebind now that the texture has valid storage/state.
    nv2a_select_texture(tile, (uint32_t)tex_id, t.linear_filter);
    if (trace_upload) {
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE 5 texture rebound");
    }
}

static void nv2a_prepare_textures_for_draw(void)
{
    uint32_t texture_program = 0;

    for (int tile = 0; tile < TEXTURE_TILE_COUNT; ++tile) {
        g_draw_texture_id[tile] = -1;
        g_draw_texture_external[tile] = false;
        g_draw_texture_external_id[tile] = UINT32_MAX;
        g_draw_texture_width[tile] = 0;
        g_draw_texture_height[tile] = 0;
    }

    for (int tile = 0; tile < TEXTURE_TILE_COUNT; ++tile) {
        if (!g_current_shader->used_textures[tile]) continue;

        const int texture_id = g_active_texture[tile];
        if (texture_id <= 0 || texture_id >= MAX_TEXTURES ||
            !g_textures[texture_id].used || !g_textures[texture_id].vram) {
            continue;
        }

        NV2ATexture *draw_texture = &g_textures[texture_id];
        if (tile == 0 && g_current_shader->cc.opt_blur) {
            NV2ATexture *blurred = nv2a_prepare_blur_texture(texture_id);
            if (blurred) draw_texture = blurred;
        }
        nv2a_bind_texture(tile, *draw_texture);
        g_draw_texture_id[tile] = texture_id;
        g_draw_texture_external[tile] = draw_texture->external;
        g_draw_texture_external_id[tile] = draw_texture->external_id;
        g_draw_texture_width[tile] = draw_texture->width;
        g_draw_texture_height[tile] = draw_texture->height;
        if (tile == 0) {
            texture_program |= NV097_SET_SHADER_STAGE_PROGRAM_STAGE0_2D_PROJECTIVE;
        } else {
            texture_program |= NV097_SET_SHADER_STAGE_PROGRAM_STAGE1_2D_PROJECTIVE << 5;
        }
    }

    if (g_current_shader->cc.opt_noise) {
        nv2a_bind_texture(2, g_noise_texture);
        texture_program |=
            NV097_SET_SHADER_STAGE_PROGRAM_STAGE2_2D_PROJECTIVE << 10;
    }

    // Set the texture program only after all format registers have been
    // submitted. This keeps XEMU and hardware from compiling a sampler with
    // an uninitialized dimensionality.
    if (!g_texture_program_valid || g_texture_program != texture_program) {
        uint32_t *p = pb_begin();
        p = pb_push1(p, NV097_SET_SHADER_STAGE_PROGRAM, texture_program);
        pb_end(p);
        g_texture_program = texture_program;
        g_texture_program_valid = true;
    }
}

// A general NV2A combiner stage evaluates A*B + C*D.  Two stages therefore
// represent the N64 equation exactly:
//
//   stage N:     A*C + (-B)*C
//   stage N + 1: SPARE0*1 + D*1
//
// Two N64 cycles consume four of the NV2A's eight available stages.  Input
// slots are packed into the two interpolated colour registers and the two
// constant registers per draw; RGB and alpha channels are allocated
// independently because gfx_pc also packs their semantics independently.
struct NV2AInputRef {
    uint8_t reg;
    uint8_t map;
    bool alpha;
};

struct NV2ADrawBindings {
    uint8_t rgb_reg[7];
    uint8_t alpha_reg[7];
    int diffuse_rgb;
    int specular_rgb;
    int diffuse_alpha;
    int specular_alpha;
    int const_rgb[2];
    int const_alpha[2];
    uint32_t const_color[2];
};

static NV2AInputRef nv2a_ref(uint8_t reg, bool alpha = false,
                             uint8_t map = NV2A_MAP_UNSIGNED_ID)
{
    NV2AInputRef r = { reg, map, alpha };
    return r;
}

static uint32_t nv2a_icw(const NV2AInputRef &a, const NV2AInputRef &b,
                         const NV2AInputRef &c, const NV2AInputRef &d)
{
    return ((uint32_t)(a.map & 7) << 29) | ((uint32_t)a.alpha << 28) |
           ((uint32_t)(a.reg & 15) << 24) |
           ((uint32_t)(b.map & 7) << 21) | ((uint32_t)b.alpha << 20) |
           ((uint32_t)(b.reg & 15) << 16) |
           ((uint32_t)(c.map & 7) << 13) | ((uint32_t)c.alpha << 12) |
           ((uint32_t)(c.reg & 15) << 8) |
           ((uint32_t)(d.map & 7) << 5) | ((uint32_t)d.alpha << 4) |
           ((uint32_t)(d.reg & 15));
}

static NV2AInputRef nv2a_negate(NV2AInputRef value)
{
    if (value.reg == NV2A_REG_ZERO && value.map == NV2A_MAP_UNSIGNED_INV) {
        // EXPAND_NORMAL(0) is -1, the negation of the synthetic ONE source.
        value.map = NV2A_MAP_EXPAND_NORMAL;
    } else if (value.reg == NV2A_REG_ZERO) {
        value.map = NV2A_MAP_UNSIGNED_ID;
    } else {
        value.map = NV2A_MAP_SIGNED_NEG;
    }
    return value;
}

static NV2AInputRef nv2a_invert(NV2AInputRef value)
{
    if (value.map == NV2A_MAP_UNSIGNED_ID) {
        value.map = NV2A_MAP_UNSIGNED_INV;
    } else if (value.map == NV2A_MAP_UNSIGNED_INV) {
        value.map = NV2A_MAP_UNSIGNED_ID;
    }
    return value;
}

static uint8_t nv2a_u8(float value)
{
    if (value <= 0.0f) return 0;
    if (value >= 1.0f) return 255;
    return (uint8_t)(value * 255.0f + 0.5f);
}

static uint32_t nv2a_pack_argb(float r, float g, float b, float a)
{
    return ((uint32_t)nv2a_u8(a) << 24) |
           ((uint32_t)nv2a_u8(r) << 16) |
           ((uint32_t)nv2a_u8(g) << 8) |
           (uint32_t)nv2a_u8(b);
}

static void nv2a_mark_formula_inputs(const CCFeatures &cc, int cycle,
                                     int portion, bool live[7])
{
    const uint8_t *c = cc.c[cycle][portion];
    auto mark = [live](uint8_t src) {
        if (src >= SHADER_INPUT_1 && src <= SHADER_INPUT_7) {
            live[src - SHADER_INPUT_1] = true;
        }
    };

    if (cc.do_single[cycle][portion]) {
        mark(c[3]);
    } else if (cc.do_multiply[cycle][portion]) {
        mark(c[0]);
        mark(c[2]);
    } else if (cc.do_mix[cycle][portion]) {
        mark(c[0]);
        mark(c[1]);
        mark(c[2]);
    } else {
        mark(c[0]);
        mark(c[1]);
        mark(c[2]);
        mark(c[3]);
    }
}

static NV2AInputRef nv2a_source_ref(uint8_t src, bool alpha_portion,
                                    const NV2ADrawBindings &bindings)
{
    if (src >= SHADER_INPUT_1 && src <= SHADER_INPUT_7) {
        const int slot = src - SHADER_INPUT_1;
        return nv2a_ref(alpha_portion ? bindings.alpha_reg[slot]
                                      : bindings.rgb_reg[slot],
                        alpha_portion);
    }

    switch (src) {
        case SHADER_TEXEL0:
            return nv2a_ref(NV2A_REG_TEX0, alpha_portion);
        case SHADER_TEXEL0A:
            return nv2a_ref(NV2A_REG_TEX0, true);
        case SHADER_TEXEL1:
            return nv2a_ref(NV2A_REG_TEX1, alpha_portion);
        case SHADER_TEXEL1A:
            return nv2a_ref(NV2A_REG_TEX1, true);
        case SHADER_COMBINED:
            return nv2a_ref(NV2A_REG_COMBINED, alpha_portion);
        case SHADER_NOISE:
            return nv2a_ref(NV2A_REG_TEX2, alpha_portion);
        case SHADER_1:
            return nv2a_ref(NV2A_REG_ZERO, false, NV2A_MAP_UNSIGNED_INV);
        case SHADER_0:
        default:
            return nv2a_ref(NV2A_REG_ZERO);
    }
}

static NV2ADrawBindings nv2a_allocate_draw_bindings(const ShaderProgram &prg,
                                                      const float *buf,
                                                      size_t stride,
                                                      size_t nverts,
                                                      size_t input_base)
{
    NV2ADrawBindings b = {};
    b.diffuse_rgb = b.specular_rgb = -1;
    b.diffuse_alpha = b.specular_alpha = -1;
    b.const_rgb[0] = b.const_rgb[1] = -1;
    b.const_alpha[0] = b.const_alpha[1] = -1;

    bool live_rgb[7] = {};
    bool live_alpha[7] = {};
    const int cycles = prg.cc.opt_2cyc ? 2 : 1;
    for (int cycle = 0; cycle < cycles; ++cycle) {
        nv2a_mark_formula_inputs(prg.cc, cycle, 0, live_rgb);
        if (prg.cc.opt_alpha) {
            nv2a_mark_formula_inputs(prg.cc, cycle, 1, live_alpha);
        }
    }

    const size_t input_width = prg.cc.opt_alpha ? 4 : 3;
    bool varies_rgb[7] = {};
    bool varies_alpha[7] = {};
    const float *first_inputs = buf + input_base;
    for (size_t vertex = 1; vertex < nverts; ++vertex) {
        const float *inputs = buf + vertex * stride + input_base;
        for (int slot = 0; slot < prg.num_inputs; ++slot) {
            const float *first = first_inputs + slot * input_width;
            const float *value = inputs + slot * input_width;
            if (live_rgb[slot] && !varies_rgb[slot] &&
                (value[0] != first[0] || value[1] != first[1] ||
                 value[2] != first[2])) {
                varies_rgb[slot] = true;
            }
            if (live_alpha[slot] && !varies_alpha[slot] &&
                value[3] != first[3]) {
                varies_alpha[slot] = true;
            }
        }
    }

    int varying_rgb[7], constant_rgb[7], nrgbv = 0, nrgbc = 0;
    int varying_alpha[7], constant_alpha[7], nav = 0, nac = 0;
    for (int i = 0; i < prg.num_inputs; ++i) {
        if (live_rgb[i]) {
            if (varies_rgb[i])
                varying_rgb[nrgbv++] = i;
            else
                constant_rgb[nrgbc++] = i;
        }
        if (prg.cc.opt_alpha && live_alpha[i]) {
            if (varies_alpha[i])
                varying_alpha[nav++] = i;
            else
                constant_alpha[nac++] = i;
        }
    }

    int rgb_vertex_regs[2] = { NV2A_REG_DIFFUSE, NV2A_REG_SPECULAR };
    int rgb_vertex_slots[2] = { -1, -1 };
    int rgbv_used = 0, rgbc_used = 0;
    for (int n = 0; n < nrgbv; ++n) {
        const int slot = varying_rgb[n];
        if (rgbv_used < 2) {
            b.rgb_reg[slot] = rgb_vertex_regs[rgbv_used];
            rgb_vertex_slots[rgbv_used++] = slot;
        } else {
            sysLogPrintf(LOG_ERROR, "NV2A: too many varying RGB combiner inputs (%d)", nrgbv);
            b.rgb_reg[slot] = NV2A_REG_DIFFUSE;
        }
    }
    for (int n = 0; n < nrgbc; ++n) {
        const int slot = constant_rgb[n];
        if (rgbv_used < 2) {
            b.rgb_reg[slot] = rgb_vertex_regs[rgbv_used];
            rgb_vertex_slots[rgbv_used++] = slot;
        } else if (rgbc_used < 2) {
            b.rgb_reg[slot] = rgbc_used ? NV2A_REG_CONST1 : NV2A_REG_CONST0;
            b.const_rgb[rgbc_used++] = slot;
        } else {
            sysLogPrintf(LOG_ERROR, "NV2A: too many constant RGB combiner inputs (%d)", nrgbc);
            b.rgb_reg[slot] = NV2A_REG_CONST0;
        }
    }
    b.diffuse_rgb = rgb_vertex_slots[0];
    b.specular_rgb = rgb_vertex_slots[1];

    int alpha_vertex_regs[2] = { NV2A_REG_DIFFUSE, NV2A_REG_SPECULAR };
    const int alpha_vertex_capacity = prg.opt_fog ? 1 : 2;
    int alpha_vertex_slots[2] = { -1, -1 };
    int av_used = 0, ac_used = 0;
    for (int n = 0; n < nav; ++n) {
        const int slot = varying_alpha[n];
        if (av_used < alpha_vertex_capacity) {
            b.alpha_reg[slot] = alpha_vertex_regs[av_used];
            alpha_vertex_slots[av_used++] = slot;
        } else {
            sysLogPrintf(LOG_ERROR, "NV2A: too many varying alpha combiner inputs (%d, fog=%d)",
                         nav, prg.opt_fog ? 1 : 0);
            b.alpha_reg[slot] = NV2A_REG_DIFFUSE;
        }
    }
    for (int n = 0; n < nac; ++n) {
        const int slot = constant_alpha[n];
        if (av_used < alpha_vertex_capacity) {
            b.alpha_reg[slot] = alpha_vertex_regs[av_used];
            alpha_vertex_slots[av_used++] = slot;
        } else if (ac_used < 2) {
            b.alpha_reg[slot] = ac_used ? NV2A_REG_CONST1 : NV2A_REG_CONST0;
            b.const_alpha[ac_used++] = slot;
        } else {
            sysLogPrintf(LOG_ERROR, "NV2A: too many constant alpha combiner inputs (%d)", nac);
            b.alpha_reg[slot] = NV2A_REG_CONST0;
        }
    }
    b.diffuse_alpha = alpha_vertex_slots[0];
    b.specular_alpha = alpha_vertex_slots[1];

    for (int ci = 0; ci < 2; ++ci) {
        float r = 0.0f, g = 0.0f, blue = 0.0f, a = 0.0f;
        if (b.const_rgb[ci] >= 0) {
            const float *v = buf + input_base + b.const_rgb[ci] * input_width;
            r = v[0]; g = v[1]; blue = v[2];
        }
        if (b.const_alpha[ci] >= 0) {
            a = buf[input_base + b.const_alpha[ci] * input_width + 3];
        }
        b.const_color[ci] = nv2a_pack_argb(r, g, blue, a);
    }
    return b;
}

static bool nv2a_formula_is_general(const CCFeatures &cc, int cycle,
                                    int portion)
{
    return !cc.do_single[cycle][portion] &&
           !cc.do_multiply[cycle][portion] &&
           !cc.do_mix[cycle][portion];
}

static uint32_t nv2a_formula_first_stage(const CCFeatures &cc, int cycle,
                                         int portion,
                                         const NV2ADrawBindings &bindings)
{
    const bool alpha = portion != 0;
    const uint8_t *c = cc.c[cycle][portion];
    const NV2AInputRef zero = nv2a_ref(NV2A_REG_ZERO);
    const NV2AInputRef one = nv2a_ref(NV2A_REG_ZERO, false,
                                      NV2A_MAP_UNSIGNED_INV);
    const NV2AInputRef a = nv2a_source_ref(c[0], alpha, bindings);
    const NV2AInputRef b = nv2a_source_ref(c[1], alpha, bindings);
    const NV2AInputRef factor = nv2a_source_ref(c[2], alpha, bindings);
    const NV2AInputRef d = nv2a_source_ref(c[3], alpha, bindings);

    if (cc.do_single[cycle][portion]) {
        return nv2a_icw(d, one, zero, zero);
    }
    if (cc.do_multiply[cycle][portion]) {
        return nv2a_icw(a, factor, zero, zero);
    }
    if (cc.do_mix[cycle][portion]) {
        return nv2a_icw(a, factor, b, nv2a_invert(factor));
    }
    return nv2a_icw(a, factor, nv2a_negate(b), factor);
}

static uint32_t nv2a_formula_second_stage(const CCFeatures &cc, int cycle,
                                          int portion,
                                          const NV2ADrawBindings &bindings)
{
    const bool alpha = portion != 0;
    const NV2AInputRef zero = nv2a_ref(NV2A_REG_ZERO);
    const NV2AInputRef one = nv2a_ref(NV2A_REG_ZERO, false,
                                      NV2A_MAP_UNSIGNED_INV);
    if (!nv2a_formula_is_general(cc, cycle, portion)) {
        return nv2a_icw(nv2a_ref(NV2A_REG_COMBINED, alpha), one,
                         zero, zero);
    }
    const NV2AInputRef d = nv2a_source_ref(cc.c[cycle][portion][3],
                                           alpha, bindings);
    return nv2a_icw(nv2a_ref(NV2A_REG_COMBINED, alpha), one, d, one);
}

static void nv2a_program_exact_combiner(const ShaderProgram &prg,
                                         const NV2ADrawBindings &bindings,
                                         const float *fog,
                                         const float *grayscale,
                                         bool noise_mask)
{
    const NV2AInputRef zero = nv2a_ref(NV2A_REG_ZERO);
    const NV2AInputRef one = nv2a_ref(NV2A_REG_ZERO, false,
                                      NV2A_MAP_UNSIGNED_INV);
    const int cycles = prg.cc.opt_2cyc ? 2 : 1;
    int stages = 0;
    for (int cycle = 0; cycle < cycles; ++cycle) {
        const bool rgb_general = nv2a_formula_is_general(prg.cc, cycle, 0);
        const bool alpha_general = prg.cc.opt_alpha &&
            nv2a_formula_is_general(prg.cc, cycle, 1);
        stages += (rgb_general || alpha_general) ? 2 : 1;
    }
    const int noise_stage = stages;
    if (noise_mask) ++stages;
    const int grayscale_stage = stages;
    if (prg.opt_grayscale && grayscale) stages += 2;

    NV2ACombinerCacheKey cache_key;
    memset(&cache_key, 0, sizeof(cache_key));
    cache_key.shader_id0 = prg.shader_id0;
    cache_key.shader_id1 = prg.shader_id1;
    memcpy(cache_key.rgb_reg, bindings.rgb_reg, sizeof(cache_key.rgb_reg));
    memcpy(cache_key.alpha_reg, bindings.alpha_reg, sizeof(cache_key.alpha_reg));
    memcpy(cache_key.const_color, bindings.const_color,
           sizeof(cache_key.const_color));
    if (prg.opt_fog && fog) {
        cache_key.flags |= 1u;
        cache_key.fog_color = nv2a_pack_argb(fog[0], fog[1], fog[2], 1.0f);
    }
    if (prg.opt_grayscale && grayscale) {
        cache_key.flags |= 2u;
        cache_key.grayscale_color = nv2a_pack_argb(
            grayscale[0], grayscale[1], grayscale[2], grayscale[3]);
    }
    if (prg.cc.opt_invisible) cache_key.flags |= 4u;
    if (noise_mask) cache_key.flags |= 8u;
    if (prg.cc.opt_texture_edge) {
        cache_key.alpha_mode = 1;
        cache_key.alpha_ref = 49u;
    } else if (noise_mask) {
        cache_key.alpha_mode = 2;
        cache_key.alpha_ref = 255u;
    } else if (prg.cc.opt_alpha_threshold) {
        cache_key.alpha_mode = 3;
        cache_key.alpha_ref = 8u;
    }

    if (g_combiner_cache_valid &&
        memcmp(&g_combiner_cache, &cache_key, sizeof(cache_key)) == 0) {
        return;
    }
    ++g_dbg_combiner_programs;

    // Stage-local constants let grayscale use its own colour and 1/3 vector
    // without stealing the two constants allocated to RDP inputs.
    uint32_t control = (uint32_t)stages;
    if (prg.opt_grayscale && grayscale) control |= (1u << 12) | (1u << 16);
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_COMBINER_CONTROL, control);
    pb_end(p);

    int stage = 0;
    for (int cycle = 0; cycle < cycles; ++cycle) {
        const bool rgb_general = nv2a_formula_is_general(prg.cc, cycle, 0);
        const bool alpha_general = prg.cc.opt_alpha &&
            nv2a_formula_is_general(prg.cc, cycle, 1);
        const bool second = rgb_general || alpha_general;
        uint32_t color0 = nv2a_formula_first_stage(prg.cc, cycle, 0,
                                                   bindings);
        uint32_t alpha0 = prg.cc.opt_alpha
            ? nv2a_formula_first_stage(prg.cc, cycle, 1, bindings)
            : nv2a_icw(one, one, zero, zero);
        p = pb_begin();
        p = pb_push1(p, NV097_COMBINER_COLOR_ICW(stage), color0);
        p = pb_push1(p, NV097_COMBINER_COLOR_OCW(stage), NV2A_OCW_DEFAULT);
        p = pb_push1(p, NV097_COMBINER_ALPHA_ICW(stage), alpha0);
        p = pb_push1(p, NV097_COMBINER_ALPHA_OCW(stage), NV2A_OCW_DEFAULT);
        p = pb_push1(p, NV097_COMBINER_FACTOR0(stage), bindings.const_color[0]);
        p = pb_push1(p, NV097_COMBINER_FACTOR1(stage), bindings.const_color[1]);
        pb_end(p);
        ++stage;

        if (second) {
            uint32_t color1 = nv2a_formula_second_stage(prg.cc, cycle, 0,
                                                        bindings);
            uint32_t alpha1 = prg.cc.opt_alpha
                ? nv2a_formula_second_stage(prg.cc, cycle, 1, bindings)
                : nv2a_icw(nv2a_ref(NV2A_REG_COMBINED, true), one,
                            zero, zero);
            p = pb_begin();
            p = pb_push1(p, NV097_COMBINER_COLOR_ICW(stage), color1);
            p = pb_push1(p, NV097_COMBINER_COLOR_OCW(stage), NV2A_OCW_DEFAULT);
            p = pb_push1(p, NV097_COMBINER_ALPHA_ICW(stage), alpha1);
            p = pb_push1(p, NV097_COMBINER_ALPHA_OCW(stage), NV2A_OCW_DEFAULT);
            p = pb_push1(p, NV097_COMBINER_FACTOR0(stage), bindings.const_color[0]);
            p = pb_push1(p, NV097_COMBINER_FACTOR1(stage), bindings.const_color[1]);
            pb_end(p);
            ++stage;
        }
    }

    if (noise_mask) {
        // The PC fragment shader keeps a fragment when random + alpha >= 1.
        // Compute that scalar into SPARE0 alpha for the stencil-only pass;
        // unsigned combiner saturation turns every survivor into exactly 1.
        p = pb_begin();
        p = pb_push1(p, NV097_COMBINER_COLOR_ICW(noise_stage),
                     nv2a_icw(nv2a_ref(NV2A_REG_COMBINED), one,
                               zero, zero));
        p = pb_push1(p, NV097_COMBINER_COLOR_OCW(noise_stage),
                     NV2A_OCW_DEFAULT);
        p = pb_push1(p, NV097_COMBINER_ALPHA_ICW(noise_stage),
                     nv2a_icw(nv2a_ref(NV2A_REG_COMBINED, true), one,
                               nv2a_ref(NV2A_REG_TEX2, true), one));
        p = pb_push1(p, NV097_COMBINER_ALPHA_OCW(noise_stage),
                     NV2A_OCW_DEFAULT);
        p = pb_push1(p, NV097_COMBINER_FACTOR0(noise_stage), 0);
        p = pb_push1(p, NV097_COMBINER_FACTOR1(noise_stage), 0);
        pb_end(p);
    }

    if (prg.opt_grayscale && grayscale) {
        const uint32_t gray = nv2a_pack_argb(grayscale[0], grayscale[1],
                                             grayscale[2], grayscale[3]);
        const uint32_t third = nv2a_pack_argb(1.0f / 3.0f, 1.0f / 3.0f,
                                              1.0f / 3.0f, 1.0f);
        // Dot(base.rgb, 1/3) -> SPARE1; carry base alpha alongside it.
        p = pb_begin();
        p = pb_push1(p, NV097_COMBINER_COLOR_ICW(grayscale_stage),
                     nv2a_icw(nv2a_ref(NV2A_REG_COMBINED),
                               nv2a_ref(NV2A_REG_CONST1), zero, zero));
        p = pb_push1(p, NV097_COMBINER_COLOR_OCW(grayscale_stage),
                     (1u << 13) | (NV2A_REG_SPARE1 << 4));
        p = pb_push1(p, NV097_COMBINER_ALPHA_ICW(grayscale_stage),
                     nv2a_icw(nv2a_ref(NV2A_REG_COMBINED, true), one,
                               zero, zero));
        p = pb_push1(p, NV097_COMBINER_ALPHA_OCW(grayscale_stage),
                     (NV2A_REG_SPARE1 << 4));
        p = pb_push1(p, NV097_COMBINER_FACTOR0(grayscale_stage), gray);
        p = pb_push1(p, NV097_COMBINER_FACTOR1(grayscale_stage), third);
        pb_end(p);

        // base*(1-gray.a) + gray.rgb*intensity -> SPARE0.
        const NV2AInputRef gray_alpha = nv2a_ref(NV2A_REG_CONST0, true,
                                                 NV2A_MAP_UNSIGNED_INV);
        p = pb_begin();
        p = pb_push1(p, NV097_COMBINER_COLOR_ICW(grayscale_stage + 1),
                     nv2a_icw(nv2a_ref(NV2A_REG_COMBINED), gray_alpha,
                               nv2a_ref(NV2A_REG_SPARE1),
                               nv2a_ref(NV2A_REG_CONST0)));
        p = pb_push1(p, NV097_COMBINER_COLOR_OCW(grayscale_stage + 1),
                     NV2A_OCW_DEFAULT);
        p = pb_push1(p, NV097_COMBINER_ALPHA_ICW(grayscale_stage + 1),
                     nv2a_icw(nv2a_ref(NV2A_REG_SPARE1, true), one,
                               zero, zero));
        p = pb_push1(p, NV097_COMBINER_ALPHA_OCW(grayscale_stage + 1),
                     NV2A_OCW_DEFAULT);
        p = pb_push1(p, NV097_COMBINER_FACTOR0(grayscale_stage + 1), gray);
        p = pb_push1(p, NV097_COMBINER_FACTOR1(grayscale_stage + 1), third);
        pb_end(p);
    }

    p = pb_begin();
    if (prg.opt_fog && fog) {
        p = pb_push1(p, NV097_SET_FOG_COLOR, cache_key.fog_color);
        const uint32_t fog_alpha = NV2A_REG_SPECULAR | 0x10u;
        p = pb_push1(p, NV097_SET_COMBINER_SPECULAR_FOG_CW0,
                     (fog_alpha << 24) | (NV2A_REG_FOG << 16) |
                     (NV2A_REG_COMBINED << 8));
    } else {
        p = pb_push1(p, NV097_SET_COMBINER_SPECULAR_FOG_CW0,
                     (NV2A_REG_COMBINED << 8));
    }
    const uint32_t final_alpha = prg.cc.opt_invisible
        ? (NV2A_REG_ZERO << 8)
        : ((NV2A_REG_COMBINED | 0x10u) << 8);
    p = pb_push1(p, NV097_SET_COMBINER_SPECULAR_FOG_CW1, final_alpha);

    if (prg.cc.opt_texture_edge) {
        p = pb_push1(p, NV097_SET_ALPHA_TEST_ENABLE, 1);
        p = pb_push1(p, NV097_SET_ALPHA_FUNC, NV097_SET_ALPHA_FUNC_V_GREATER);
        p = pb_push1(p, NV097_SET_ALPHA_REF, 49u);
    } else if (noise_mask) {
        p = pb_push1(p, NV097_SET_ALPHA_TEST_ENABLE, 1);
        p = pb_push1(p, NV097_SET_ALPHA_FUNC, NV097_SET_ALPHA_FUNC_V_GEQUAL);
        p = pb_push1(p, NV097_SET_ALPHA_REF, 255u);
    } else if (prg.cc.opt_alpha_threshold) {
        p = pb_push1(p, NV097_SET_ALPHA_TEST_ENABLE, 1);
        p = pb_push1(p, NV097_SET_ALPHA_FUNC, NV097_SET_ALPHA_FUNC_V_GEQUAL);
        p = pb_push1(p, NV097_SET_ALPHA_REF, 8u);
    } else {
        p = pb_push1(p, NV097_SET_ALPHA_TEST_ENABLE, 0);
    }
    pb_end(p);
    // Shader changes do not necessarily cause gfx_pc to restate use_alpha.
    // Restore blending here as well so entering/leaving a texture-edge shader
    // cannot leak its opaque-survivor behavior into the next draw.
    nv2a_program_blend_state(prg.cc.opt_texture_edge);
    g_combiner_cache = cache_key;
    g_combiner_cache_valid = true;
}

static void nv2a_clear_noise_stencil(void)
{
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_ZSTENCIL_CLEAR_VALUE, 0xFFFFFF00u);
    p = pb_push1(p, NV097_SET_CLEAR_RECT_HORIZONTAL,
                 (g_target_width - 1u) << 16);
    p = pb_push1(p, NV097_SET_CLEAR_RECT_VERTICAL,
                 (g_target_height - 1u) << 16);
    p = pb_push1(p, NV097_CLEAR_SURFACE,
                 NV097_CLEAR_SURFACE_STENCIL);
    pb_end(p);
    g_noise_stencil_ref = 0;
}

static uint8_t nv2a_next_noise_stencil_ref(void)
{
    if (g_noise_stencil_ref >= 255u) {
        nv2a_clear_noise_stencil();
    }
    return (uint8_t)++g_noise_stencil_ref;
}

static void nv2a_begin_noise_mask(uint8_t ref)
{
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_CONTROL0, nv2a_control0(true));
    p = pb_push1(p, NV097_SET_STENCIL_TEST_ENABLE, 1);
    p = pb_push1(p, NV097_SET_STENCIL_MASK, 0xFFu);
    p = pb_push1(p, NV097_SET_STENCIL_FUNC,
                 NV097_SET_ALPHA_FUNC_V_ALWAYS);
    p = pb_push1(p, NV097_SET_STENCIL_FUNC_REF, ref);
    p = pb_push1(p, NV097_SET_STENCIL_FUNC_MASK, 0xFFu);
    p = pb_push1(p, NV097_SET_STENCIL_OP_FAIL,
                 NV097_SET_STENCIL_OP_V_KEEP);
    p = pb_push1(p, NV097_SET_STENCIL_OP_ZFAIL,
                 NV097_SET_STENCIL_OP_V_KEEP);
    p = pb_push1(p, NV097_SET_STENCIL_OP_ZPASS,
                 NV097_SET_STENCIL_OP_V_REPLACE);
    p = pb_push1(p, NV097_SET_COLOR_MASK, 0);
    p = pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE, 0);
    p = pb_push1(p, NV097_SET_BLEND_ENABLE, 0);
    pb_end(p);
}

static void nv2a_begin_noise_color(uint8_t ref)
{
    const uint32_t rgba_mask =
        NV097_SET_COLOR_MASK_BLUE_WRITE_ENABLE |
        NV097_SET_COLOR_MASK_GREEN_WRITE_ENABLE |
        NV097_SET_COLOR_MASK_RED_WRITE_ENABLE |
        NV097_SET_COLOR_MASK_ALPHA_WRITE_ENABLE;
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_CONTROL0, nv2a_control0(false));
    p = pb_push1(p, NV097_SET_STENCIL_TEST_ENABLE, 1);
    p = pb_push1(p, NV097_SET_STENCIL_MASK, 0);
    p = pb_push1(p, NV097_SET_STENCIL_FUNC,
                 NV097_SET_ALPHA_FUNC_V_EQUAL);
    p = pb_push1(p, NV097_SET_STENCIL_FUNC_REF, ref);
    p = pb_push1(p, NV097_SET_STENCIL_FUNC_MASK, 0xFFu);
    p = pb_push1(p, NV097_SET_STENCIL_OP_FAIL,
                 NV097_SET_STENCIL_OP_V_KEEP);
    p = pb_push1(p, NV097_SET_STENCIL_OP_ZFAIL,
                 NV097_SET_STENCIL_OP_V_KEEP);
    p = pb_push1(p, NV097_SET_STENCIL_OP_ZPASS,
                 NV097_SET_STENCIL_OP_V_KEEP);
    p = pb_push1(p, NV097_SET_COLOR_MASK, rgba_mask);
    p = pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE,
                 g_rs.depth_write ? 1u : 0u);
    pb_end(p);
}

static void nv2a_end_noise_stencil(void)
{
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_STENCIL_TEST_ENABLE, 0);
    p = pb_push1(p, NV097_SET_CONTROL0, nv2a_control0(false));
    p = pb_push1(p, NV097_SET_STENCIL_MASK, 0xFFu);
    p = pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE,
                 g_rs.depth_write ? 1u : 0u);
    pb_end(p);
}

static void nv2a_scale_linear_texcoord(int tile, float &s, float &t,
                                       bool clamp_s, float max_s,
                                       bool clamp_t, float max_t)
{
    const int texture_id = g_active_texture[tile];

    if (texture_id <= 0 || texture_id >= MAX_TEXTURES ||
        !g_textures[texture_id].used || !g_textures[texture_id].vram) {
        return;
    }

    // gfx_pc supplies normalized coordinates, matching sampler2D on the GL
    // backend. Native swizzled textures consume those coordinates directly;
    // LU_IMAGE (linear/NPOT) textures use texel-space input.
    const NV2ATexture &texture = g_textures[texture_id];
    const uint32_t draw_width = g_draw_texture_width[tile]
        ? g_draw_texture_width[tile] : texture.width;
    const uint32_t draw_height = g_draw_texture_height[tile]
        ? g_draw_texture_height[tile] : texture.height;
    const float scale_s = texture.swizzled
        ? (float)draw_width / (float)(texture.storage_width
            ? texture.storage_width : texture.width)
        : (float)draw_width;
    const float scale_t = texture.swizzled
        ? (float)draw_height / (float)(texture.storage_height
            ? texture.storage_height : texture.height)
        : (float)draw_height;
    s *= scale_s;
    t *= scale_t;
    if (clamp_s) {
        max_s *= scale_s;
        if (s > max_s) s = max_s;
    }
    if (clamp_t) {
        max_t *= scale_t;
        if (t > max_t) t = max_t;
    }
}

static void nv2a_set_sampler_parameters(int sampler, bool linear_filter,
                                         uint32_t cms, uint32_t cmt,
                                         bool mipmaps)
{
    if (sampler < 0 || sampler >= TEXTURE_TILE_COUNT) return;

    int tex_id = g_active_texture[sampler];
    if (tex_id <= 0 || tex_id >= MAX_TEXTURES || !g_textures[tex_id].used) return;

    auto address_mode = [](uint32_t cm) -> uint8_t {
        const bool mirror = (cm & 1u) != 0;
        const bool clamp = (cm & 2u) != 0;
        if (mirror && clamp) return 6; // mirror-once, clamp-to-edge
        if (mirror) return 2;          // mirrored repeat
        if (clamp) return 3;           // clamp-to-edge
        return 1;                      // repeat
    };

    NV2ATexture &t = g_textures[tex_id];
    // Sampler state does not replace texture content. In particular, logical
    // previous-frame textures may alias a completed pbkit back buffer; keep
    // that alias live while updating filtering and address modes.
    t.linear_filter = linear_filter;
    t.address_s = address_mode(cms);
    t.address_t = address_mode(cmt);
    t.mipmaps = mipmaps && g_mipmap_mode != MIPMAP_DISABLED;
    // gfx_pc configures a newly reserved texture before uploading its pixels.
    // Preserve the requested sampler state, but do not submit OFFSET/FORMAT
    // until storage and dimensions exist. Real NV2A hardware halts PGRAPH on
    // the resulting zero FORMAT; XEMU silently accepted it.
    if (!nv2a_texture_data(t)) {
        nv2a_disable_texture(sampler);
        return;
    }
    nv2a_bind_texture(sampler, t);
}

static void nv2a_delete_texture(uint32_t texID)
{
    if (texID == 0 || texID >= MAX_TEXTURES) return;
    for (int tile = 0; tile < TEXTURE_TILE_COUNT; ++tile) {
        if (g_active_texture[tile] == (int)texID) {
            nv2a_disable_texture(tile);
            g_active_texture[tile] = -1;
        }
    }
    NV2ATexture &t = g_textures[texID];
    if (t.vram) {
        MmFreeContiguousMemory(t.vram);
        t.vram = nullptr;
    }
    t = {};
}

// ── Depth / blend / viewport ──────────────────────────────────────────────────

static void nv2a_set_depth_mode(bool depth_test, bool depth_write,
                                 bool depth_compare, bool depth_source_prim,
                                 uint16_t zmode)
{
    g_rs.depth_test  = depth_test;
    g_rs.depth_write = depth_write;

    uint32_t func = NV097_SET_DEPTH_FUNC_V_ALWAYS;
    bool decal = false;
    if (depth_compare) {
        switch (zmode) {
            case 0x400: // ZMODE_INTER
                func = NV097_SET_DEPTH_FUNC_V_LEQUAL;
                break;
            case 0xc00: // ZMODE_DEC
                func = NV097_SET_DEPTH_FUNC_V_LEQUAL;
                decal = true;
                break;
            case 0x000: // ZMODE_OPA
            case 0x800: // ZMODE_XLU
            default:
                func = depth_source_prim ? NV097_SET_DEPTH_FUNC_V_LEQUAL
                                         : NV097_SET_DEPTH_FUNC_V_LESS;
                break;
        }
    }

    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_DEPTH_TEST_ENABLE,  depth_test  ? 1 : 0);
    p = pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE, depth_write ? 1 : 0);
    p = pb_push1(p, NV097_SET_DEPTH_FUNC, func);
    p = pb_push1(p, NV097_SET_POLY_OFFSET_FILL_ENABLE, decal ? 1 : 0);
    p = pb_push1(p, NV097_SET_POLYGON_OFFSET_SCALE_FACTOR,
                 f2u(decal ? -2.0f : 0.0f));
    p = pb_push1(p, NV097_SET_POLYGON_OFFSET_BIAS,
                 f2u(decal ? -2.0f : 0.0f));
    pb_end(p);
}

static void nv2a_set_depth_range(float znear, float zfar)
{
    g_rs.znear = znear;
    g_rs.zfar = zfar;
    const float depth_scale = 16777215.0f;
    uint32_t *p = pb_begin();
    p = pb_push2(p, NV20_TCL_PRIMITIVE_3D_DEPTH_RANGE_NEAR,
                 f2u(znear * depth_scale), f2u(zfar * depth_scale));
    pb_end(p);
    nv2a_upload_viewport_constants();
}

static void nv2a_set_viewport(int x, int y, int w, int h)
{
    g_rs.vp_x = x;
    g_rs.vp_y = y;
    g_rs.vp_w = w;
    g_rs.vp_h = h;

    // NV2A viewport: offset + scale from NDC → screen
    float ox = (float)x + (float)w * 0.5f;
    const int top = (int)g_target_height - y - h;
    float oy = (float)top + (float)h * 0.5f;
    float sx = (float)w * 0.5f;
    float sy = (float)h * 0.5f;

    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 0*4, f2u(ox));
    p = pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 1*4, f2u(oy));
    p = pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 2*4, f2u(0.5f));   // Z offset
    p = pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 3*4, f2u(0.0f));
    p = pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 0*4, f2u(sx));
    p = pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 1*4, f2u(sy));
    p = pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 2*4, f2u(0.5f));   // Z scale
    p = pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 3*4, f2u(1.0f));
    pb_end(p);

    nv2a_upload_viewport_constants();
}

static void nv2a_set_scissor(int x, int y, int w, int h)
{
    g_rs.sc_x = x;
    g_rs.sc_y = y;
    g_rs.sc_w = w;
    g_rs.sc_h = h;

    int x0 = x < 0 ? 0 : x;
    int x1 = x + w;
    if (x1 < x0) x1 = x0;
    if (x1 > (int)g_target_width) x1 = (int)g_target_width;
    int y0 = (int)g_target_height - (y + h);
    int y1 = (int)g_target_height - y;
    if (y0 < 0) y0 = 0;
    if (y1 < y0) y1 = y0;
    if (y1 > (int)g_target_height) y1 = (int)g_target_height;

    // Use NV2A window clip region 0 as scissor. gfx_pc supplies OpenGL-style
    // bottom-left coordinates, while the NV2A window clip is top-left.
    // Format: bits [11:0] = min (inclusive), bits [27:16] = max (exclusive).
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_WINDOW_CLIP_HORIZONTAL,
                 ((uint32_t)x1 << 16) | (uint32_t)x0);
    p = pb_push1(p, NV097_SET_WINDOW_CLIP_VERTICAL,
                 ((uint32_t)y1 << 16) | (uint32_t)y0);
    pb_end(p);
}

static void nv2a_set_use_alpha(bool use_alpha, bool modulate)
{
    g_rs.use_alpha      = use_alpha;
    g_rs.alpha_modulate = modulate;
    const bool texture_edge = g_current_shader != nullptr &&
                              g_current_shader->cc.opt_texture_edge;
    nv2a_program_blend_state(texture_edge);
}

// ── Draw triangles ────────────────────────────────────────────────────────────
// fast3d gives us a packed float VBO with num_floats stride per vertex.
// We convert to the NV2A inline-array format and submit via pbkit.
//
// NV2A inline-array vertex layout (fixed-function):
//   POS4F  (x,y,z,w)
//   TEX0_2F (s,t) — if used
//   TEX1_2F (s,t) — if used
//   DIFFUSE (ARGB packed u32)
//   SPECULAR (ARGB packed u32) — used for fog
//
// We declare the vertex attribute sizes before the draw and then push the data.

static unsigned g_dbg_tris = 0;
static unsigned g_dbg_draws = 0;

// The NV2A vertex program below emits screen-space coordinates because NV2A
// programs do not provide OpenGL's automatic clip-space path.  Clip here,
// before its perspective divide, so triangles crossing the eye or any frustum
// plane cannot turn into unbounded screen-space wedges.  Interpolating the
// complete fast3d vertex preserves texture, fog, grayscale, and combiner
// varyings exactly as homogeneous rasterization requires.
#define NV2A_MAX_VERTEX_FLOATS 64u
#define NV2A_MAX_CLIPPED_VERTICES 12u

static float nv2a_clip_distance(const float *v, unsigned plane)
{
    switch (plane) {
        case 0: return v[0] + v[3]; // x >= -w
        case 1: return v[3] - v[0]; // x <=  w
        case 2: return v[1] + v[3]; // y >= -w
        case 3: return v[3] - v[1]; // y <=  w
        // fast3d supplies OpenGL clip-space vertices. The passthrough vertex
        // program maps NDC Z through the Xbox 0..1 viewport itself, so the
        // CPU clip volume must retain OpenGL's -w <= z <= w convention here.
        // Clipping against z >= 0 discarded menu/logo and other textured
        // quads before they ever reached the NV2A.
        case 4: return v[2] + v[3]; // z >= -w
        default: return v[3] - v[2]; // z <= w
    }
}

static uint8_t nv2a_clip_code(const float *v)
{
    uint8_t code = 0;
    for (unsigned plane = 0; plane < 6; ++plane) {
        if (nv2a_clip_distance(v, plane) < 0.0f) {
            code |= (uint8_t)(1u << plane);
        }
    }
    return code;
}

static size_t nv2a_clip_triangle(const float *triangle, size_t stride,
                                 float output[NV2A_MAX_CLIPPED_VERTICES]
                                             [NV2A_MAX_VERTEX_FLOATS])
{
    float work_a[NV2A_MAX_CLIPPED_VERTICES][NV2A_MAX_VERTEX_FLOATS];
    float work_b[NV2A_MAX_CLIPPED_VERTICES][NV2A_MAX_VERTEX_FLOATS];
    float (*source)[NV2A_MAX_VERTEX_FLOATS] = work_a;
    float (*target)[NV2A_MAX_VERTEX_FLOATS] = work_b;
    size_t count = 3;

    for (size_t i = 0; i < count; ++i) {
        memcpy(source[i], triangle + i * stride, stride * sizeof(float));
    }

    for (unsigned plane = 0; plane < 6 && count != 0; ++plane) {
        size_t target_count = 0;
        const float *previous = source[count - 1];
        float previous_distance = nv2a_clip_distance(previous, plane);
        bool previous_inside = previous_distance >= 0.0f;

        for (size_t i = 0; i < count; ++i) {
            const float *current = source[i];
            const float current_distance = nv2a_clip_distance(current, plane);
            const bool current_inside = current_distance >= 0.0f;

            if (current_inside != previous_inside) {
                assert(target_count < NV2A_MAX_CLIPPED_VERTICES);
                const float denominator = previous_distance - current_distance;
                const float t = denominator != 0.0f
                    ? previous_distance / denominator : 0.0f;
                for (size_t component = 0; component < stride; ++component) {
                    target[target_count][component] = previous[component]
                        + (current[component] - previous[component]) * t;
                }
                ++target_count;
            }
            if (current_inside) {
                assert(target_count < NV2A_MAX_CLIPPED_VERTICES);
                memcpy(target[target_count], current, stride * sizeof(float));
                ++target_count;
            }

            previous = current;
            previous_distance = current_distance;
            previous_inside = current_inside;
        }

        count = target_count;
        float (*swap)[NV2A_MAX_VERTEX_FLOATS] = source;
        source = target;
        target = swap;
    }

    for (size_t i = 0; i < count; ++i) {
        memcpy(output[i], source[i], stride * sizeof(float));
    }
    return count;
}

static void nv2a_draw_triangles(float buf_vbo[], size_t buf_vbo_len,
                                 size_t buf_vbo_num_tris)
{
    if (!g_current_shader || buf_vbo_num_tris == 0) return;
    nv2a_prepare_textures_for_draw();
    g_dbg_tris += (unsigned)buf_vbo_num_tris;
    ++g_dbg_draws;

    const ShaderProgram &prg   = *g_current_shader;
    const size_t         stride = prg.num_floats;
    const size_t         nverts = buf_vbo_num_tris * 3;
    if (stride > NV2A_MAX_VERTEX_FLOATS) {
        sysLogPrintf(LOG_ERROR, "NV2A: vertex stride %lu exceeds clip capacity",
                     (unsigned long)stride);
        return;
    }

    size_t layout_off = 4;
    for (int t = 0; t < TEXTURE_TILE_COUNT; ++t) {
        if (prg.used_textures[t]) {
            layout_off += 2;
            layout_off += prg.clamp[t][0] ? 1 : 0;
            layout_off += prg.clamp[t][1] ? 1 : 0;
        }
    }
    const size_t fog_base = layout_off;
    if (prg.opt_fog) layout_off += 4;
    const size_t grayscale_base = layout_off;
    if (prg.opt_grayscale) layout_off += 4;
    const size_t input_base = layout_off;
    const size_t input_width = prg.opt_alpha ? 4 : 3;
    const NV2ADrawBindings bindings =
        nv2a_allocate_draw_bindings(prg, buf_vbo, stride, nverts, input_base);

#ifdef PD_XBOX_RENDER_TRACE_FRAME
    if (g_frame_count == (uint32_t)PD_XBOX_RENDER_TRACE_FRAME) {
        float min_x = 1.0e30f, min_y = 1.0e30f;
        float max_x = -1.0e30f, max_y = -1.0e30f;
        uint8_t input_min[3] = { 255, 255, 255 };
        uint8_t input_max[3] = { 0, 0, 0 };
        uint8_t alpha_min[3] = { 255, 255, 255 };
        uint8_t alpha_max[3] = { 0, 0, 0 };
        uint8_t fog_min = 255, fog_max = 0;
        for (size_t vi = 0; vi < nverts; ++vi) {
            const float *v = buf_vbo + vi * stride;
            const float inv_w = v[3] != 0.0f ? 1.0f / v[3] : 0.0f;
            const float x = v[0] * inv_w;
            const float y = v[1] * inv_w;
            if (x < min_x) min_x = x;
            if (x > max_x) max_x = x;
            if (y < min_y) min_y = y;
            if (y > max_y) max_y = y;
            for (int slot = 0; slot < prg.num_inputs && slot < 3; ++slot) {
                const float *in = v + input_base + slot * input_width;
                const uint8_t intensity = nv2a_u8((in[0] + in[1] + in[2]) / 3.0f);
                const uint8_t alpha = prg.opt_alpha ? nv2a_u8(in[3]) : 255;
                if (intensity < input_min[slot]) input_min[slot] = intensity;
                if (intensity > input_max[slot]) input_max[slot] = intensity;
                if (alpha < alpha_min[slot]) alpha_min[slot] = alpha;
                if (alpha > alpha_max[slot]) alpha_max[slot] = alpha;
            }
            if (prg.opt_fog) {
                const uint8_t factor = nv2a_u8(v[fog_base + 3]);
                if (factor < fog_min) fog_min = factor;
                if (factor > fog_max) fog_max = factor;
            }
        }
        const int tex0 = g_active_texture[0];
        const int tex1 = g_active_texture[1];
        const NV2ATexture *t0 = tex0 > 0 && tex0 < MAX_TEXTURES
            ? &g_textures[tex0] : nullptr;
        const NV2ATexture *t1 = tex1 > 0 && tex1 < MAX_TEXTURES
            ? &g_textures[tex1] : nullptr;
        sysLogPrintf(LOG_NOTE,
            "RTRACE f=%lu tri=%lu sh=%08lx%08lx/%08lx tex=%d:%ux%u,%d:%ux%u "
            "bb=%ld,%ld,%ld,%ld in=%u-%u/%u-%u,%u-%u/%u-%u,%u-%u/%u-%u "
            "fog=%u-%u/%u,%u,%u reg=%x%x%x/%x%x%x c=%08lx,%08lx z=%d%d a=%d",
            (unsigned long)g_frame_count, (unsigned long)buf_vbo_num_tris,
            (unsigned long)(prg.shader_id0 >> 32), (unsigned long)prg.shader_id0,
            (unsigned long)prg.shader_id1,
            tex0, t0 ? t0->width : 0, t0 ? t0->height : 0,
            tex1, t1 ? t1->width : 0, t1 ? t1->height : 0,
            (long)(min_x * 1000.0f), (long)(min_y * 1000.0f),
            (long)(max_x * 1000.0f), (long)(max_y * 1000.0f),
            input_min[0], input_max[0], alpha_min[0], alpha_max[0],
            input_min[1], input_max[1], alpha_min[1], alpha_max[1],
            input_min[2], input_max[2], alpha_min[2], alpha_max[2],
            fog_min, fog_max,
            prg.opt_fog ? nv2a_u8(buf_vbo[fog_base + 0]) : 0,
            prg.opt_fog ? nv2a_u8(buf_vbo[fog_base + 1]) : 0,
            prg.opt_fog ? nv2a_u8(buf_vbo[fog_base + 2]) : 0,
            bindings.rgb_reg[0], bindings.rgb_reg[1], bindings.rgb_reg[2],
            bindings.alpha_reg[0], bindings.alpha_reg[1], bindings.alpha_reg[2],
            (unsigned long)bindings.const_color[0],
            (unsigned long)bindings.const_color[1],
            g_rs.depth_test ? 1 : 0, g_rs.depth_write ? 1 : 0,
            g_rs.use_alpha ? 1 : 0);
    }
#endif
    const float *fog_color = prg.opt_fog ? buf_vbo + fog_base : nullptr;
    const float *grayscale_color = prg.opt_grayscale
        ? buf_vbo + grayscale_base : nullptr;
    nv2a_program_exact_combiner(prg, bindings, fog_color, grayscale_color,
                                false);

    if (!g_stream_vertices) return;

    if (!g_stream_attributes_bound) {
        auto set_attribute = [](uint32_t index, uint32_t type,
                                uint32_t size, const void *data) {
            const uint32_t format =
                type | (size << 4) | (sizeof(NV2AStreamVertex) << 8);
            const uint32_t offset =
                (uint32_t)(uintptr_t)data & 0x03ffffffu;
#if defined(PD_XBOX_GPU_SYNC_TRACE)
            if (g_frame_count <= 1 && g_gpu_sync_trace_active) {
                sysLogPrintf(LOG_NOTE,
                             "NV2A ATTR index=%lu format=%08lx offset=%08lx",
                             (unsigned long)index, (unsigned long)format,
                             (unsigned long)offset);
            }
#endif
            uint32_t *q = pb_begin();
            q = pb_push1(q, NV097_SET_VERTEX_DATA_ARRAY_FORMAT + index * 4u,
                         format);
            q = pb_push1(q, NV097_SET_VERTEX_DATA_ARRAY_OFFSET + index * 4u,
                         offset);
            pb_end(q);
        };
        set_attribute(0, NV097_SET_VERTEX_DATA_ARRAY_FORMAT_TYPE_F, 4,
                      &g_stream_vertices[0].position[0]);
        set_attribute(3, NV097_SET_VERTEX_DATA_ARRAY_FORMAT_TYPE_UB_D3D, 4,
                      &g_stream_vertices[0].diffuse);
        set_attribute(4, NV097_SET_VERTEX_DATA_ARRAY_FORMAT_TYPE_UB_D3D, 4,
                      &g_stream_vertices[0].specular);
        // vp20compiler maps TEXCOORD0/1 to physical NV2A attributes 9/10.
        set_attribute(9, NV097_SET_VERTEX_DATA_ARRAY_FORMAT_TYPE_F, 2,
                      &g_stream_vertices[0].tex0[0]);
        set_attribute(10, NV097_SET_VERTEX_DATA_ARRAY_FORMAT_TYPE_F, 2,
                      &g_stream_vertices[0].tex1[0]);
        g_stream_attributes_bound = true;
    }

    auto emit_stream_range = [&](uint32_t first_vertex, uint32_t count) {
        if (count == 0) return;

        bool has_game_texture = false;
        bool has_external_texture = false;
        for (int tile = 0; tile < TEXTURE_TILE_COUNT; ++tile) {
            const bool stage_active = prg.used_textures[tile] &&
                g_draw_texture_id[tile] > 0 &&
                g_hw_texture[tile].valid && g_hw_texture[tile].enabled;
            has_game_texture |= stage_active;
            has_external_texture |= stage_active &&
                g_draw_texture_external[tile];
        }

        const char *proof_kind = nullptr;
        uint32_t proof_block = 0;
        if (g_texture_program != 0 && has_game_texture) {
            if (has_external_texture && !g_logged_external_texture_proof) {
                proof_kind = "external";
                proof_block = 2;
                g_logged_external_texture_proof = true;
            } else if (!has_external_texture &&
                       !g_logged_stock_texture_proof) {
                proof_kind = "stock";
                proof_block = 1;
                g_logged_stock_texture_proof = true;
            }
        }

        if (proof_kind) {
            sysLogPrintf(LOG_NOTE,
                         "NV2A TEXPROOF begin kind=%s build=%s frame=%lu first=%lu count=%lu texprog=%08lx shader_upload_serial=%lu",
                         proof_kind, NV2A_RUNTIME_BUILD_ID,
                         (unsigned long)g_frame_count,
                         (unsigned long)first_vertex, (unsigned long)count,
                         (unsigned long)g_texture_program,
                         (unsigned long)g_shader_abi_upload_serial);
            for (uint32_t stage = 0; stage < NV2A_TEXTURE_STAGE_COUNT;
                 ++stage) {
                const NV2AHardwareTextureState &hw = g_hw_texture[stage];
                const bool game_stage = stage < TEXTURE_TILE_COUNT;
                sysLogPrintf(LOG_NOTE,
                             "NV2A TEXPROOF kind=%s stage=%lu used=%u backend=%d external=%u extid=%08lx size=%lux%lu valid=%u enabled=%u words=%08lx,%08lx,%08lx,%08lx,%08lx,%08lx,%08lx",
                             proof_kind, (unsigned long)stage,
                             game_stage && prg.used_textures[stage] ? 1u : 0u,
                             game_stage ? g_draw_texture_id[stage] : -1,
                             game_stage && g_draw_texture_external[stage]
                                 ? 1u : 0u,
                             (unsigned long)(game_stage
                                 ? g_draw_texture_external_id[stage]
                                 : UINT32_MAX),
                             (unsigned long)(game_stage
                                 ? g_draw_texture_width[stage] : 0u),
                             (unsigned long)(game_stage
                                 ? g_draw_texture_height[stage] : 0u),
                             hw.valid ? 1u : 0u, hw.enabled ? 1u : 0u,
                             (unsigned long)hw.words[0],
                             (unsigned long)hw.words[1],
                             (unsigned long)hw.words[2],
                             (unsigned long)hw.words[3],
                             (unsigned long)hw.words[4],
                             (unsigned long)hw.words[5],
                             (unsigned long)hw.words[6]);
            }
            const uint32_t inspect_count = count < 3u ? count : 3u;
            for (uint32_t i = 0; i < inspect_count; ++i) {
                const NV2AStreamVertex &v =
                    g_stream_vertices[first_vertex + i];
                sysLogPrintf(LOG_NOTE,
                             "NV2A TEXPROOF kind=%s vertex=%lu pos=%08lx,%08lx,%08lx,%08lx tex0=%08lx,%08lx tex1=%08lx,%08lx",
                             proof_kind,
                             (unsigned long)(first_vertex + i),
                             (unsigned long)f2u(v.position[0]),
                             (unsigned long)f2u(v.position[1]),
                             (unsigned long)f2u(v.position[2]),
                             (unsigned long)f2u(v.position[3]),
                             (unsigned long)f2u(v.tex0[0]),
                             (unsigned long)f2u(v.tex0[1]),
                             (unsigned long)f2u(v.tex1[0]),
                             (unsigned long)f2u(v.tex1[1]));
            }
        }

        auto complete_texture_proof = [&](const uint32_t *end) {
            if (!proof_kind) return;
            const bool idle = nv2a_wait_for_idle_trace(
                "texture_proof_draw", proof_block, __LINE__, end);
            sysLogPrintf(idle ? LOG_NOTE : LOG_ERROR,
                         "NV2A TEXPROOF complete kind=%s build=%s gpu_idle=%u texture_stage_enabled=1 c5_upload_serial=%lu",
                         proof_kind, NV2A_RUNTIME_BUILD_ID,
                         idle ? 1u : 0u,
                         (unsigned long)g_shader_abi_upload_serial);
        };

#if defined(PD_XBOX_GPU_SYNC_TRACE)
        if (g_frame_count <= 1 && g_gpu_sync_trace_active) {
            const uint32_t stream_offset =
                (uint32_t)(uintptr_t)g_stream_vertices & 0x03ffffffu;
            sysLogPrintf(LOG_NOTE,
                         "NV2A DRAWSTATE first=%lu count=%lu cursor=%lu stream=%p offset=%08lx stride=%lu texprog=%08lx",
                         (unsigned long)first_vertex, (unsigned long)count,
                         (unsigned long)g_stream_vertex_cursor,
                         g_stream_vertices, (unsigned long)stream_offset,
                         (unsigned long)sizeof(NV2AStreamVertex),
                         (unsigned long)g_texture_program);
            for (uint32_t stage = 0; stage < NV2A_TEXTURE_STAGE_COUNT;
                 ++stage) {
                const NV2AHardwareTextureState &hw = g_hw_texture[stage];
                sysLogPrintf(LOG_NOTE,
                             "NV2A DRAWSTATE TEX%lu valid=%u enabled=%u words=%08lx,%08lx,%08lx,%08lx,%08lx,%08lx,%08lx",
                             (unsigned long)stage, hw.valid ? 1u : 0u,
                             hw.enabled ? 1u : 0u,
                             (unsigned long)hw.words[0],
                             (unsigned long)hw.words[1],
                             (unsigned long)hw.words[2],
                             (unsigned long)hw.words[3],
                             (unsigned long)hw.words[4],
                             (unsigned long)hw.words[5],
                             (unsigned long)hw.words[6]);
            }
            const uint32_t inspect_count = count < 3u ? count : 3u;
            for (uint32_t i = 0; i < inspect_count; ++i) {
                const NV2AStreamVertex &v =
                    g_stream_vertices[first_vertex + i];
                sysLogPrintf(LOG_NOTE,
                             "NV2A DRAWSTATE V%lu pos=%08lx,%08lx,%08lx,%08lx color=%08lx,%08lx tex=%08lx,%08lx/%08lx,%08lx",
                             (unsigned long)(first_vertex + i),
                             (unsigned long)f2u(v.position[0]),
                             (unsigned long)f2u(v.position[1]),
                             (unsigned long)f2u(v.position[2]),
                             (unsigned long)f2u(v.position[3]),
                             (unsigned long)v.diffuse,
                             (unsigned long)v.specular,
                             (unsigned long)f2u(v.tex0[0]),
                             (unsigned long)f2u(v.tex0[1]),
                             (unsigned long)f2u(v.tex1[0]),
                             (unsigned long)f2u(v.tex1[1]));
            }

            // CPU writes the write-combined streaming array continuously.
            // Invalidate Kelvin's vertex fetch cache before drawing from the
            // newly written range, then isolate BEGIN, every DRAW_ARRAYS, and
            // END so a real-Xbox PGRAPH trap identifies one exact method.
            uint32_t *trace = pb_begin();
            trace = pb_push1(trace, NV097_BREAK_VERTEX_BUFFER_CACHE, 0);
            pb_end(trace);

            trace = pb_begin();
            trace = pb_push1(trace, NV097_SET_BEGIN_END,
                             NV097_SET_BEGIN_END_OP_TRIANGLES);
            pb_end(trace);

            uint32_t start = first_vertex;
            uint32_t remaining = count;
            while (remaining) {
                const uint32_t draw_count =
                    remaining > 256u ? 256u : remaining;
                const uint32_t draw_word =
                    ((draw_count - 1u) << 24) | start;
                sysLogPrintf(LOG_NOTE,
                             "NV2A DRAWMETHOD start=%lu count=%lu word=%08lx",
                             (unsigned long)start,
                             (unsigned long)draw_count,
                             (unsigned long)draw_word);
                trace = pb_begin();
                trace = pb_push1(trace,
                                 0x40000000u | NV097_DRAW_ARRAYS,
                                 draw_word);
                pb_end(trace);
                start += draw_count;
                remaining -= draw_count;
            }

            trace = pb_begin();
            trace = pb_push1(trace, NV097_SET_BEGIN_END,
                             NV097_SET_BEGIN_END_OP_END);
            pb_end(trace);

            bool textured_draw = g_texture_program != 0;
            for (uint32_t stage = 0;
                 stage < NV2A_TEXTURE_STAGE_COUNT && !textured_draw;
                 ++stage) {
                textured_draw = g_hw_texture[stage].valid &&
                                g_hw_texture[stage].enabled;
            }
            if (textured_draw) {
                sysLogPrintf(LOG_NOTE,
                             "NV2A TRACE CUTOFF first textured draw complete");
                g_gpu_sync_trace_active = false;
            }
            complete_texture_proof(trace);
            return;
        }
#endif

        uint32_t *p = pb_begin();
        p = pb_push1(p, NV097_BREAK_VERTEX_BUFFER_CACHE, 0);
        p = pb_push1(p, NV097_SET_BEGIN_END, NV097_SET_BEGIN_END_OP_TRIANGLES);
        uint32_t start = first_vertex;
        uint32_t remaining = count;
        while (remaining) {
            const uint32_t draw_count = remaining > 256u ? 256u : remaining;
            p = pb_push1(p, 0x40000000u | NV097_DRAW_ARRAYS,
                         ((draw_count - 1u) << 24) | start);
            start += draw_count;
            remaining -= draw_count;
        }
        p = pb_push1(p, NV097_SET_BEGIN_END, NV097_SET_BEGIN_END_OP_END);
        pb_end(p);
        complete_texture_proof(p);
    };

    const bool spatial_noise_coverage = prg.cc.opt_noise &&
        prg.cc.opt_alpha && !prg.cc.opt_texture_edge;
    auto submit_stream_range = [&](uint32_t first_vertex, uint32_t count) {
        if (count == 0) return;
        if (!spatial_noise_coverage) {
            emit_stream_range(first_vertex, count);
            return;
        }

        const uint8_t stencil_ref = nv2a_next_noise_stencil_ref();
        g_combiner_cache_valid = false;
        nv2a_program_exact_combiner(prg, bindings, fog_color,
                                    grayscale_color, true);
        nv2a_begin_noise_mask(stencil_ref);
        emit_stream_range(first_vertex, count);

        // Replay with the unmodified PC combiner alpha and blend state. Stencil
        // performs only the stochastic discard, so surviving fragments retain
        // precisely the alpha that the PC shader writes.
        g_combiner_cache_valid = false;
        nv2a_program_exact_combiner(prg, bindings, fog_color,
                                    grayscale_color, false);
        nv2a_begin_noise_color(stencil_ref);
        emit_stream_range(first_vertex, count);
        nv2a_end_noise_stencil();
        ++g_dbg_noise_stencil_draws;
    };

    auto convert_vertex = [&](const float *v, NV2AStreamVertex &dst) {
        size_t off = 0;

        float px = v[off++], py = v[off++], pz = v[off++], pw = v[off++];
        float s0 = 0, t0 = 0, s1 = 0, t1 = 0;

        if (prg.used_textures[0]) {
            s0 = v[off++]; t0 = v[off++];
            const float max_s = prg.clamp[0][0] ? v[off++] : 0.0f;
            const float max_t = prg.clamp[0][1] ? v[off++] : 0.0f;
            nv2a_scale_linear_texcoord(0, s0, t0,
                                       prg.clamp[0][0], max_s,
                                       prg.clamp[0][1], max_t);
        }
        if (prg.used_textures[1]) {
            s1 = v[off++]; t1 = v[off++];
            const float max_s = prg.clamp[1][0] ? v[off++] : 0.0f;
            const float max_t = prg.clamp[1][1] ? v[off++] : 0.0f;
            nv2a_scale_linear_texcoord(1, s1, t1,
                                       prg.clamp[1][0], max_s,
                                       prg.clamp[1][1], max_t);
        }

        float fog_factor = 0.0f;
        if (prg.opt_fog) {
            off += 3;
            fog_factor = v[off++];
        }
        if (prg.opt_grayscale) off += 4;

        const float *inputs = v + off;
        auto input_component = [inputs, input_width](int slot, int component,
                                                      float fallback) {
            return slot >= 0 ? inputs[(size_t)slot * input_width + component]
                             : fallback;
        };

        float cr = input_component(bindings.diffuse_rgb, 0, 0.0f);
        float cg = input_component(bindings.diffuse_rgb, 1, 0.0f);
        float cb = input_component(bindings.diffuse_rgb, 2, 0.0f);
        float ca = input_component(bindings.diffuse_alpha, 3, 1.0f);
        float sr = input_component(bindings.specular_rgb, 0, 0.0f);
        float sg = input_component(bindings.specular_rgb, 1, 0.0f);
        float sb = input_component(bindings.specular_rgb, 2, 0.0f);
        float sa;
        if (prg.opt_fog) {
            sa = fog_factor;
        } else {
            sa = input_component(bindings.specular_alpha, 3, 0.0f);
        }

        dst.position[0] = px; dst.position[1] = py;
        dst.position[2] = pz; dst.position[3] = pw;
        dst.diffuse = nv2a_pack_argb(cr, cg, cb, ca);
        dst.specular = nv2a_pack_argb(sr, sg, sb, sa);
        dst.tex0[0] = s0; dst.tex0[1] = t0;
        dst.tex1[0] = s1; dst.tex1[1] = t1;
    };

    // A triangle clipped by six planes can produce at most nine polygon
    // vertices (seven fan triangles).  Reserve 21 stream slots per source
    // triangle; flush safely if a large fast3d batch crosses the ring end.
    uint32_t batch_first = g_stream_vertex_cursor;
    uint32_t batch_count = 0;
    size_t output_triangles = 0;
    size_t rejected_triangles = 0;
    float polygon[NV2A_MAX_CLIPPED_VERTICES][NV2A_MAX_VERTEX_FLOATS];

    for (size_t triangle_index = 0; triangle_index < buf_vbo_num_tris;
         ++triangle_index) {
        if (NV2A_STREAM_VERTEX_CAPACITY - g_stream_vertex_cursor < 21u) {
            submit_stream_range(batch_first, batch_count);
            batch_count = 0;
            pb_wait_until_gr_not_busy();
            g_stream_vertex_cursor = 0;
            batch_first = 0;
        }

        const float *triangle = buf_vbo + triangle_index * 3u * stride;
        const uint8_t code0 = nv2a_clip_code(triangle);
        const uint8_t code1 = nv2a_clip_code(triangle + stride);
        const uint8_t code2 = nv2a_clip_code(triangle + stride * 2u);
        const uint8_t code_or = code0 | code1 | code2;
        if (code_or == 0) {
            // Nearly all submitted world geometry is wholly inside the
            // frustum. Avoid six full-stride Sutherland-Hodgman copies here.
            convert_vertex(triangle,
                           g_stream_vertices[g_stream_vertex_cursor++]);
            convert_vertex(triangle + stride,
                           g_stream_vertices[g_stream_vertex_cursor++]);
            convert_vertex(triangle + stride * 2u,
                           g_stream_vertices[g_stream_vertex_cursor++]);
            batch_count += 3;
            ++output_triangles;
            continue;
        }
        if ((code0 & code1 & code2) != 0) {
            ++rejected_triangles;
            continue;
        }
        const size_t polygon_count =
            nv2a_clip_triangle(triangle, stride, polygon);
        if (polygon_count < 3) {
            ++rejected_triangles;
            continue;
        }

        for (size_t fan = 1; fan + 1 < polygon_count; ++fan) {
            convert_vertex(polygon[0],
                           g_stream_vertices[g_stream_vertex_cursor++]);
            convert_vertex(polygon[fan],
                           g_stream_vertices[g_stream_vertex_cursor++]);
            convert_vertex(polygon[fan + 1],
                           g_stream_vertices[g_stream_vertex_cursor++]);
            batch_count += 3;
            ++output_triangles;
        }
    }
    submit_stream_range(batch_first, batch_count);

#ifdef PD_XBOX_RENDER_TRACE_FRAME
    if (g_frame_count == (uint32_t)PD_XBOX_RENDER_TRACE_FRAME) {
        sysLogPrintf(LOG_NOTE, "RCLIP f=%lu tri=%lu>%lu reject=%lu",
                     (unsigned long)g_frame_count,
                     (unsigned long)buf_vbo_num_tris,
                     (unsigned long)output_triangles,
                     (unsigned long)rejected_triangles);
    }
#endif
}

// ── Frame lifecycle ────────────────────────────────────────────────────────────

static void nv2a_init(void)
{
    unsigned sync_trace = 0;
#if defined(PD_XBOX_GPU_SYNC_TRACE)
    g_gpu_sync_trace_active = true;
    sync_trace = 1;
#endif
    sysLogPrintf(LOG_NOTE, "NV2A TRACE BUILD id=%s sync=%u",
                 NV2A_RUNTIME_BUILD_ID, sync_trace);
    memset(g_textures, 0, sizeof(g_textures));
    memset(g_framebuffers, 0, sizeof(g_framebuffers));
    g_framebuffers[0].used = true;
    g_framebuffer_count = 1;
    g_current_framebuffer = FB_BACK_BUFFER;
    g_active_texture[0] = g_active_texture[1] = -1;
    g_last_selected_tile = 0;
    g_frame_count = 0;
    g_noise_stencil_ref = 0;
    memset(g_hw_texture, 0, sizeof(g_hw_texture));
    g_texture_program_valid = false;
    g_combiner_cache_valid = false;
    memset(g_draw_texture_width, 0, sizeof(g_draw_texture_width));
    memset(g_draw_texture_height, 0, sizeof(g_draw_texture_height));
    g_draw_texture_id[0] = g_draw_texture_id[1] = -1;
    memset(g_draw_texture_external, 0, sizeof(g_draw_texture_external));
    g_draw_texture_external_id[0] = UINT32_MAX;
    g_draw_texture_external_id[1] = UINT32_MAX;
    g_logged_stock_texture_proof = false;
    g_logged_external_texture_proof = false;
    g_logged_shader_abi_upload = false;
    g_shader_abi_upload_serial = 0;
    g_perf_frame_started = 0;
    g_perf_submit_ticks = 0;
    g_perf_gpu_drain_ticks = 0;
    g_perf_queue_ticks = 0;
    g_perf_submit_max = 0;
    g_perf_gpu_drain_max = 0;
    g_perf_queue_max = 0;
    g_perf_previous_vbl = pb_get_vbl_counter();
    g_perf_vbl_total = 0;
    g_perf_vbl_max = 0;
    g_perf_frame_samples = 0;
    g_blur_texture = {};
    g_blur_source_id = -1;
    g_blur_source_version = 0;
    nv2a_init_noise_texture();
    g_stream_vertex_cursor = 0;
    g_stream_attributes_bound = false;
    g_stream_vertices = (NV2AStreamVertex *)MmAllocateContiguousMemoryEx(
        NV2A_STREAM_VERTEX_CAPACITY * sizeof(NV2AStreamVertex),
        0, 0xFFFFFFFF, 0, PAGE_READWRITE | PAGE_WRITECOMBINE);
    if (!g_stream_vertices) {
        sysFatalError("NV2A: unable to allocate streaming vertex buffer");
    }
    nv2a_init_passthrough_shader();

    // Set initial NV2A state
    uint32_t *p;

    // fast3d supplies pre-lit colours, so fixed-function lighting stays off.
    // The second programmable colour output must remain enabled: register
    // combiners use it for an independent varying input and its alpha carries
    // fog. Without these bits NV2A/XEMU replaces COLOR1 with (0,0,0,1).
    p = pb_begin();
    p = pb_push1(p, NV097_SET_LIGHTING_ENABLE, 0);
    p = pb_push1(p, NV097_SET_SPECULAR_ENABLE, 1);
    p = pb_push1(p, NV097_SET_LIGHT_CONTROL,
                 NV097_SET_LIGHT_CONTROL_V_SEPARATE_SPECULAR |
                 NV097_SET_LIGHT_CONTROL_V_ALPHA_FROM_MATERIAL_SPECULAR);
    pb_end(p);

    // Culling off (N64 uses explicit winding)
    p = pb_begin();
    p = pb_push1(p, NV097_SET_CULL_FACE_ENABLE, 0);
    pb_end(p);

    // Depth test on by default
    p = pb_begin();
    p = pb_push1(p, NV097_SET_DEPTH_TEST_ENABLE,  1);
    p = pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE, 1);
    p = pb_push1(p, NV097_SET_DEPTH_FUNC, NV097_SET_DEPTH_FUNC_V_LEQUAL);
    pb_end(p);

    // Alpha test off
    p = pb_begin();
    p = pb_push1(p, NV097_SET_ALPHA_TEST_ENABLE, 0);
    pb_end(p);

    // Blending off by default
    p = pb_begin();
    p = pb_push1(p, NV097_SET_BLEND_ENABLE, 0);
    p = pb_push1(p, NV097_SET_STENCIL_TEST_ENABLE, 0);
    p = pb_push1(p, NV097_SET_CONTROL0, nv2a_control0(false));
    p = pb_push1(p, NV097_SET_COLOR_MASK,
                 NV097_SET_COLOR_MASK_BLUE_WRITE_ENABLE |
                 NV097_SET_COLOR_MASK_GREEN_WRITE_ENABLE |
                 NV097_SET_COLOR_MASK_RED_WRITE_ENABLE |
                 NV097_SET_COLOR_MASK_ALPHA_WRITE_ENABLE);
    pb_end(p);

    // Disable all texture units initially
    for (int t = 0; t < 4; ++t) {
        p = pb_begin();
        p = pb_push1(p, NV097_TEXTURE_FORMAT(t), 0x0001122au);
        p = pb_push1(p, NV097_TEXTURE_CONTROL1(t), 64u << 16);
        p = pb_push1(p, NV097_TEXTURE_IMAGE_RECT(t), (1u << 16) | 1u);
        p = pb_push1(p, NV097_TEXTURE_CONTROL0(t), 0);
        pb_end(p);
    }

    // The first frame starts with pb_reset(). Make sure initialization has
    // reached the GPU before that reset can recycle the pushbuffer head.
    while (pb_busy()) {
    }

    sysLogPrintf(LOG_NOTE, "NV2A renderer initialised");
}

static void nv2a_on_resize(void)
{
    // pbkit manages the back buffer; nothing to do here
}


// fast3d hands the backend vertices that are already in clip space. The vertex
// program performs the divide and viewport transform but must retain clip W;
// NV2A uses it for both perspective interpolation and pbkit's W-buffer.
static void nv2a_init_passthrough_shader(void)
{
    static const uint32_t program[] = {
#include "pdx_passthrough.vs.inl"
    };

    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_TRANSFORM_PROGRAM_START, 0);
    p = pb_push1(p, NV097_SET_TRANSFORM_EXECUTION_MODE,
                 NV097_SET_TRANSFORM_EXECUTION_MODE_MODE_PROGRAM | (1u << 2));
    p = pb_push1(p, NV097_SET_TRANSFORM_PROGRAM_CXT_WRITE_EN, 0);
    pb_end(p);

    p = pb_begin();
    p = pb_push1(p, NV097_SET_TRANSFORM_PROGRAM_LOAD, 0);
    pb_end(p);

    for (size_t i = 0; i < sizeof(program) / sizeof(program[0]); i += 4) {
        p = pb_begin();
        pb_push(p++, NV097_SET_TRANSFORM_PROGRAM, 4);
        memcpy(p, &program[i], 4 * sizeof(uint32_t));
        p += 4;
        pb_end(p);
    }
}

static void nv2a_upload_viewport_constants(void)
{
    const float w = (float)(g_rs.vp_w > 0 ? g_rs.vp_w : 1);
    const float h = (float)(g_rs.vp_h > 0 ? g_rs.vp_h : 1);
    const float top = (float)((int)g_target_height - g_rs.vp_y - g_rs.vp_h);
    const float depth_scale = 16777215.0f;
    const float zscale = (g_rs.zfar - g_rs.znear) * depth_scale;
    const float zoffset = g_rs.znear * depth_scale;
    const float viewport[16] = {
         w * 0.5f,  0.0f,       0.0f,     0.0f,
         0.0f,     -h * 0.5f,   0.0f,     0.0f,
         0.0f,      0.0f,      zscale,     0.0f,
         (float)g_rs.vp_x + w * 0.5f,
         top + h * 0.5f, zoffset, 1.0f,
    };
    // Texture stage 2 is a screen-space scalar-noise source. Multiplying its
    // coordinate by clip W and emitting Q=W in the vertex program cancels
    // perspective interpolation, leaving the same gl_FragCoord-based lattice
    // used by the PC shader. A per-frame phase makes successive fields unique.
    uint32_t phase = g_frame_count * 0x9e3779b9u + 0x7f4a7c15u;
    phase ^= phase >> 16;
    const float noise_transform[4] = {
        g_noise_scale,
        g_noise_scale,
        (float)(phase & 255u),
        (float)((phase >> 8) & 255u),
    };

    // cgc emits `#const c[5] = 0 1` for the literal Z/Q components in
    // pdx_passthrough.vs.cg. Generated vertex-program instructions do not
    // contain that constant data, so it is part of the shader ABI and must be
    // uploaded explicitly. In particular, TEX0/TEX1 Q must be 1 for the
    // 2D_PROJECTIVE texture stages; leaving c[5] undefined makes all texture
    // lookups undefined while untextured vertex colors continue to render.
    const float shader_literals[4] = { 0.0f, 1.0f, 0.0f, 0.0f };
    const uint32_t upload_serial = ++g_shader_abi_upload_serial;
    const bool log_shader_abi = !g_logged_shader_abi_upload;
    if (log_shader_abi) {
        sysLogPrintf(LOG_NOTE,
                     "NV2A SHADER ABI begin build=%s serial=%lu load=96 vectors=6 dwords=24",
                     NV2A_RUNTIME_BUILD_ID, (unsigned long)upload_serial);
        for (unsigned vector = 0; vector < 4; ++vector) {
            const float *c = viewport + vector * 4u;
            sysLogPrintf(LOG_NOTE,
                         "NV2A SHADER ABI c%u=%08lx,%08lx,%08lx,%08lx",
                         vector,
                         (unsigned long)f2u(c[0]), (unsigned long)f2u(c[1]),
                         (unsigned long)f2u(c[2]), (unsigned long)f2u(c[3]));
        }
        sysLogPrintf(LOG_NOTE,
                     "NV2A SHADER ABI c4=%08lx,%08lx,%08lx,%08lx",
                     (unsigned long)f2u(noise_transform[0]),
                     (unsigned long)f2u(noise_transform[1]),
                     (unsigned long)f2u(noise_transform[2]),
                     (unsigned long)f2u(noise_transform[3]));
        sysLogPrintf(LOG_NOTE,
                     "NV2A SHADER ABI c5=%08lx,%08lx,%08lx,%08lx q_literal=%08lx",
                     (unsigned long)f2u(shader_literals[0]),
                     (unsigned long)f2u(shader_literals[1]),
                     (unsigned long)f2u(shader_literals[2]),
                     (unsigned long)f2u(shader_literals[3]),
                     (unsigned long)f2u(shader_literals[1]));
    }

    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_TRANSFORM_CONSTANT_LOAD, 96);
    pb_push(p++, NV097_SET_TRANSFORM_CONSTANT, 24);
    memcpy(p, viewport, sizeof(viewport));
    p += 16;
    memcpy(p, noise_transform, sizeof(noise_transform));
    p += 4;
    memcpy(p, shader_literals, sizeof(shader_literals));
    p += 4;
    pb_end(p);
    if (log_shader_abi) {
        const bool idle = nv2a_wait_for_idle_trace(
            "shader_abi_c5", upload_serial, __LINE__, p);
        sysLogPrintf(idle ? LOG_NOTE : LOG_ERROR,
                     "NV2A SHADER ABI complete build=%s serial=%lu gpu_idle=%u c5_uploaded=1",
                     NV2A_RUNTIME_BUILD_ID, (unsigned long)upload_serial,
                     idle ? 1u : 0u);
        g_logged_shader_abi_upload = true;
    }
}

static void nv2a_set_passthrough_transform(void)
{
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_TRANSFORM_EXECUTION_MODE,
                 NV097_SET_TRANSFORM_EXECUTION_MODE_MODE_PROGRAM | (1u << 2));
    pb_end(p);
    nv2a_upload_viewport_constants();
}

static void nv2a_clear_surface(uint32_t mask);
static void nv2a_store_active_framebuffer(void);
static void nv2a_set_target_surface(int fb_id);
static void nv2a_copy_framebuffer(int fb_dst, int fb_src,
                                  int left, int top, bool flip_y,
                                  bool use_back);

// The buffer targeted this frame. pb_back_buffer() rotates, so sampling it
// after the swap reads a different surface than the one just rendered.
static DWORD *g_dbg_fb = 0;
static DWORD  g_dbg_fb_pitch = 0;
static DWORD *g_last_presented_fb = 0;
static DWORD  g_last_presented_pitch = 0;

// Stable C-linkage telemetry consumed by scripts/xemu_smoke.py.  Keeping these
// in the backend makes framebuffer capture available to every repo using the
// shared harness without depending on XEMU's host-window screenshot support.
extern "C" {
uint32_t g_SPXBFramebufferData = 0;
uint32_t g_SPXBFramebufferPitch = 0;
uint32_t g_SPXBFramebufferWidth = 0;
uint32_t g_SPXBFramebufferHeight = 0;
}

// Synchronous stage loading runs outside the normal scheduler/render loop.
// This tiny pbkit-only presenter mirrors Unreal Tournament Xbox's cooperative
// eight-dot loader without relying on fast3d state that is being torn down.
static bool g_loading_active = false;
static unsigned g_loading_step = 0;
static unsigned g_loading_draw_count = 0;
static ULONGLONG g_loading_started = 0;
static ULONGLONG g_loading_last_draw = 0;

static void nv2a_draw_loading_frame(bool force)
{
    if (!g_loading_active) return;

    const ULONGLONG now = KeQueryPerformanceCounter();
    const ULONGLONG frequency = KeQueryPerformanceFrequency();
    if (!force && g_loading_last_draw &&
            now - g_loading_last_draw < frequency / 10u) {
        return;
    }

    // Do not reset a pushbuffer that the GPU is still consuming. The previous
    // game frame or activity frame has already been queued for VBlank.
    while (pb_busy()) {
    }
    pb_reset();

    DWORD *target = pb_back_buffer();
    const DWORD pitch = pb_back_buffer_pitch();
    const int width = (int)pb_back_buffer_width();
    const int height = (int)pb_back_buffer_height();
    const float scale = (float)height / 480.0f;
    const float cx = (float)width * 0.5f;
    const float cy = (float)height - 30.0f * scale;
    const float radius = 13.0f * scale;
    const int dot = (int)(3.5f * scale + 0.5f) < 3
        ? 3 : (int)(3.5f * scale + 0.5f);
    static const float offsets[8][2] = {
        { 0.0f, -1.0f }, { 0.707f, -0.707f }, { 1.0f, 0.0f }, { 0.707f, 0.707f },
        { 0.0f,  1.0f }, {-0.707f,  0.707f }, {-1.0f, 0.0f }, {-0.707f,-0.707f },
    };

    pb_fill(0, 0, width, height, 0xFF000000u);
    for (unsigned i = 0; i < 8; ++i) {
        const unsigned age = (i - (g_loading_step & 7u) + 8u) & 7u;
        const unsigned red = 18u + (7u - age) * 4u;
        const unsigned green = 80u + (7u - age) * 18u;
        const unsigned blue = 135u + (7u - age) * 14u;
        const DWORD color = 0xFF000000u | (red << 16) | (green << 8) | blue;
        const int x = (int)(cx + offsets[i][0] * radius + 0.5f);
        const int y = (int)(cy + offsets[i][1] * radius + 0.5f);
        pb_fill(x - dot, y - dot, dot * 2, dot * 2, color);
    }

    while (pb_busy()) {
    }
    while (pb_finished()) {
    }

    // Keep the non-focus-stealing XEMU harness pointed at the completed load
    // frame, just as finish_render does for normal game frames.
    g_last_presented_fb = target;
    g_last_presented_pitch = pitch;
    g_SPXBFramebufferData = (uint32_t)(uintptr_t)target;
    g_SPXBFramebufferPitch = pitch;
    g_SPXBFramebufferWidth = (uint32_t)width;
    g_SPXBFramebufferHeight = (uint32_t)height;

    g_loading_last_draw = now;
    ++g_loading_draw_count;
    if (g_loading_draw_count <= 16u || (g_loading_draw_count & 7u) == 0u) {
        const unsigned long elapsed_ms = frequency
            ? (unsigned long)((now - g_loading_started) * 1000u / frequency) : 0;
        sysLogPrintf(LOG_NOTE,
                     "loading: frame=%u step=%u elapsed_ms=%lu",
                     g_loading_draw_count, g_loading_step, elapsed_ms);
    }
}

extern "C" void gfx_xbox_loading_begin(int from_stage, int to_stage)
{
    if (g_loading_active) return;
    g_loading_active = true;
    g_loading_step = 0;
    g_loading_draw_count = 0;
    g_loading_started = KeQueryPerformanceCounter();
    g_loading_last_draw = 0;
    sysLogPrintf(LOG_NOTE, "loading: begin from=%d to=%d", from_stage, to_stage);
    nv2a_draw_loading_frame(true);
    g_loading_step = 1;
}

extern "C" void gfx_xbox_loading_pulse(void)
{
    if (!g_loading_active) return;
    const unsigned previous_count = g_loading_draw_count;
    nv2a_draw_loading_frame(false);
    if (g_loading_draw_count != previous_count) {
        g_loading_step = (g_loading_step + 1u) & 7u;
    }
}

extern "C" void gfx_xbox_loading_end(int stage)
{
    if (!g_loading_active) return;
    const ULONGLONG now = KeQueryPerformanceCounter();
    const ULONGLONG frequency = KeQueryPerformanceFrequency();
    const unsigned long elapsed_ms = frequency
        ? (unsigned long)((now - g_loading_started) * 1000u / frequency) : 0;
    sysLogPrintf(LOG_NOTE, "loading: end stage=%d frames=%u elapsed_ms=%lu",
                 stage, g_loading_draw_count, elapsed_ms);
    g_loading_active = false;
}

static void nv2a_start_frame(void)
{
    static bool traced_first_frame = false;
    const bool trace_frame = !traced_first_frame;
    if (trace_frame) {
        traced_first_frame = true;
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F1 first frame begin");
    }
    // pb_finished() queues completed buffers for the VBlank ISR. Waiting for
    // another VBlank here serializes CPU submission behind scanout and can add
    // almost a full refresh interval before every 720p frame. pbkit explicitly
    // permits drawing ahead; its three-buffer queue supplies back-pressure.
    g_perf_frame_started = KeQueryPerformanceCounter();
    pb_reset();
    if (trace_frame) {
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F1b pushbuffer reset complete");
    }
    g_stream_vertex_cursor = 0;

    // pb_init() selects the first back buffer, and pb_finished() selects the
    // next one at the end of every frame. Reissuing pb_target_back_buffer()
    // here is redundant and, at 720p on original hardware, can wedge the NV2A
    // while pbkit reprograms its surface DMA objects a second time.
    g_current_framebuffer = FB_BACK_BUFFER;
    g_target_width = pb_back_buffer_width();
    g_target_height = pb_back_buffer_height();
    g_framebuffers[0].used = true;
    g_framebuffers[0].width = g_target_width;
    g_framebuffers[0].height = g_target_height;
    g_dbg_fb       = pb_back_buffer();
    g_dbg_fb_pitch = pb_back_buffer_pitch();
    // Until the first completed frame exists, expose the initial target so
    // boot diagnostics still have an address. Later frames keep telemetry on
    // the completed surface; publishing the active back buffer here lets the
    // external harness race the clear/draw stream and capture half a frame.
    if (g_SPXBFramebufferData == 0) {
        g_SPXBFramebufferData = (uint32_t)(uintptr_t)g_dbg_fb;
        g_SPXBFramebufferPitch = g_dbg_fb_pitch;
        g_SPXBFramebufferWidth = pb_back_buffer_width();
        g_SPXBFramebufferHeight = pb_back_buffer_height();
    }

    // Always start from a known surface. The game issues its own clears, but
    // anything it does not cover would otherwise expose stale video memory.
    nv2a_set_passthrough_transform();
    nv2a_clear_surface(0xF3);   // colour + depth + stencil
    if (trace_frame) {
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F2 first frame clear submitted");
    }
    g_noise_stencil_ref = 0;

    ++g_frame_count;
}

static void nv2a_end_frame(void)
{
    // All commands are submitted by finish_render.
}

static void nv2a_finish_render(void)
{
    static bool traced_first_finish = false;
    const bool trace_finish = !traced_first_finish;
    if (trace_finish) {
        traced_first_finish = true;
        sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F3 first finish begin");
    }
    if (g_current_framebuffer != FB_BACK_BUFFER) {
        nv2a_store_active_framebuffer();
        g_current_framebuffer = FB_BACK_BUFFER;
        nv2a_set_target_surface(FB_BACK_BUFFER);
    }
    // Hand the pushbuffer to the GPU and wait for it. Without this the
    // commands are never executed and we present a buffer nothing drew into.
    if (trace_finish) sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F4 waiting pb_busy");
    const ULONGLONG submit_complete = KeQueryPerformanceCounter();
#if defined(PD_XBOX_GPU_SYNC_TRACE)
    if (!nv2a_wait_for_idle_trace("finish_busy", 0, __LINE__, nullptr)) {
        for (;;) {
        }
    }
#else
    while (pb_busy()) {
        /* GPU still consuming */
    }
#endif
    const ULONGLONG gpu_complete = KeQueryPerformanceCounter();
    if (trace_finish) sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F5 pb_busy clear; waiting pb_finished");
#if defined(PD_XBOX_GPU_SYNC_TRACE)
    {
        const ULONGLONG start = KeQueryPerformanceCounter();
        const ULONGLONG timeout = KeQueryPerformanceFrequency() * 2u;
        while (pb_finished()) {
            if (KeQueryPerformanceCounter() - start >= timeout) {
                nv2a_dump_gpu_timeout("pb_finished", 0, __LINE__, nullptr);
                for (;;) {
                }
            }
        }
    }
#else
    while (pb_finished()) {
        /* waiting on swap prepare */
    }
#endif
    const ULONGLONG queue_complete = KeQueryPerformanceCounter();
    if (trace_finish) sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F6 GPU finish complete");

    if (g_perf_frame_started && submit_complete >= g_perf_frame_started &&
        gpu_complete >= submit_complete && queue_complete >= gpu_complete) {
        const ULONGLONG submit_ticks = submit_complete - g_perf_frame_started;
        const ULONGLONG gpu_ticks = gpu_complete - submit_complete;
        const ULONGLONG queue_ticks = queue_complete - gpu_complete;
        g_perf_submit_ticks += submit_ticks;
        g_perf_gpu_drain_ticks += gpu_ticks;
        g_perf_queue_ticks += queue_ticks;
        if (submit_ticks > g_perf_submit_max) g_perf_submit_max = submit_ticks;
        if (gpu_ticks > g_perf_gpu_drain_max) g_perf_gpu_drain_max = gpu_ticks;
        if (queue_ticks > g_perf_queue_max) g_perf_queue_max = queue_ticks;

        const DWORD vbl = pb_get_vbl_counter();
        const DWORD vbl_delta = vbl - g_perf_previous_vbl;
        g_perf_previous_vbl = vbl;
        g_perf_vbl_total += vbl_delta;
        if (vbl_delta > g_perf_vbl_max) g_perf_vbl_max = vbl_delta;

        if (++g_perf_frame_samples >= 30u) {
            const ULONGLONG frequency = KeQueryPerformanceFrequency();
            const ULONGLONG divisor = frequency * g_perf_frame_samples;
            const unsigned long submit_avg_us = divisor
                ? (unsigned long)(g_perf_submit_ticks * 1000000u / divisor) : 0;
            const unsigned long gpu_avg_us = divisor
                ? (unsigned long)(g_perf_gpu_drain_ticks * 1000000u / divisor) : 0;
            const unsigned long queue_avg_us = divisor
                ? (unsigned long)(g_perf_queue_ticks * 1000000u / divisor) : 0;
            const unsigned long submit_max_us = frequency
                ? (unsigned long)(g_perf_submit_max * 1000000u / frequency) : 0;
            const unsigned long gpu_max_us = frequency
                ? (unsigned long)(g_perf_gpu_drain_max * 1000000u / frequency) : 0;
            const unsigned long queue_max_us = frequency
                ? (unsigned long)(g_perf_queue_max * 1000000u / frequency) : 0;
            sysLogPrintf(LOG_NOTE,
                         "NV2A PERF frames=%u submit_us=%lu/%lu gpu_drain_us=%lu/%lu queue_us=%lu/%lu vbl=%lu max_delta=%lu",
                         g_perf_frame_samples,
                         submit_avg_us, submit_max_us,
                         gpu_avg_us, gpu_max_us,
                         queue_avg_us, queue_max_us,
                         (unsigned long)g_perf_vbl_total,
                         (unsigned long)g_perf_vbl_max);
            g_perf_submit_ticks = 0;
            g_perf_gpu_drain_ticks = 0;
            g_perf_queue_ticks = 0;
            g_perf_submit_max = 0;
            g_perf_gpu_drain_max = 0;
            g_perf_queue_max = 0;
            g_perf_vbl_total = 0;
            g_perf_vbl_max = 0;
            g_perf_frame_samples = 0;
        }
    }

#if defined(PD_XBOX_RENDER_QUALIFY_EFFECTS)
    // The game allocates its twenty 16x16 character-cloak captures during
    // stage setup, but reaching a visible cloaking character is not a stable
    // automated route. Copy one completed back-buffer region into an actual
    // game-created cloak target so the regression log proves the immediate,
    // unscaled partial-copy path independently of menu/full-screen captures.
    static bool qualified_cloak_copy = false;
    if (!qualified_cloak_copy) {
        for (int i = 1; i < g_framebuffer_count; ++i) {
            if (g_framebuffers[i].used &&
                    g_framebuffers[i].width == 16 &&
                    g_framebuffers[i].height == 16) {
                nv2a_copy_framebuffer(i, FB_BACK_BUFFER,
                                      0, 0, false, true);
                qualified_cloak_copy = true;
                break;
            }
        }
    }
#endif

    // Sample the finished frame and report it on the UART. PrintWindow
    // cannot read an OpenGL surface, so this is the only way to know what
    // is actually on screen without taking the window from the user.
    {
        static unsigned frames = 0;
        if ((++frames % 60u) == 0u) {
            DWORD *fb    = g_dbg_fb;
            const DWORD w = pb_back_buffer_width();
            const DWORD h = pb_back_buffer_height();
            const DWORD pitch = g_dbg_fb_pitch;
            unsigned nonblack = 0, samples = 0, sum = 0;
            if (fb && w && h && pitch) {
                const DWORD ystep = (h / 12) ? (h / 12) : 1;
                const DWORD xstep = (w / 16) ? (w / 16) : 1;
                for (DWORD y = 0; y < h; y += ystep) {
                    const DWORD *row = (const DWORD *)((const unsigned char *)fb + y * pitch);
                    for (DWORD x = 0; x < w; x += xstep) {
                        const DWORD px = row[x] & 0x00FFFFFFu;
                        ++samples;
                        if (px > 0x0A0A0Au) ++nonblack;
                        sum = sum * 31u + px;
                    }
                }
            }
            char m[256];
            snprintf(m, sizeof(m),
                     "fb %ux%u nonblack %u/%u sum %08X tris %u draws %u rc %u tex %u noise %u copy %u/%u/%u/%u/%u pix %lu\n",
                     (unsigned)w, (unsigned)h, nonblack, samples, sum,
                     g_dbg_tris, g_dbg_draws, g_dbg_combiner_programs,
                     g_dbg_texture_binds, g_dbg_noise_stencil_draws,
                     g_dbg_fb_copy_calls,
                     g_dbg_fb_copy_fast, g_dbg_fb_copy_alias,
                     g_dbg_fb_copy_scaled,
                     g_dbg_fb_copy_box,
                     (unsigned long)g_dbg_fb_copy_pixels);
            serialPuts(m);
            g_dbg_tris = 0;
            g_dbg_draws = 0;
            g_dbg_combiner_programs = 0;
            g_dbg_texture_binds = 0;
            g_dbg_noise_stencil_draws = 0;
            g_dbg_fb_copy_calls = 0;
            g_dbg_fb_copy_fast = 0;
            g_dbg_fb_copy_alias = 0;
            g_dbg_fb_copy_scaled = 0;
            g_dbg_fb_copy_box = 0;
            g_dbg_fb_copy_pixels = 0;
        }
    }

    // Preserve the completed buffer as GL_FRONT's Xbox equivalent. pbkit
    // rotates to a different render target after presentation, so the game can
    // sample this buffer during the next frame without a 1.2 MB CPU snapshot.
    g_last_presented_fb = g_dbg_fb;
    g_last_presented_pitch = g_dbg_fb_pitch;
    // Publish only after pb_finished has proven this surface complete. The
    // next frame targets another pbkit buffer, giving monitor pmemsave a stable
    // previous-frame image instead of an in-flight render target.
    g_SPXBFramebufferData = (uint32_t)(uintptr_t)g_last_presented_fb;
    g_SPXBFramebufferPitch = g_last_presented_pitch;
    g_SPXBFramebufferWidth = pb_back_buffer_width();
    g_SPXBFramebufferHeight = pb_back_buffer_height();
    // Do not call pb_show_front_screen() here. That helper writes PCRTC_START
    // immediately and points at pbkit's fixed initial front index, which can
    // change scanout in the middle of a field. pb_finished() has already queued
    // this completed frame for the VBlank ISR's tear-free rotation.
    if (trace_finish) sysLogPrintf(LOG_NOTE, "NV2A HWTRACE F7 VBlank swap queued");
}

// ── Framebuffer API ───────────────────────────────────────────────────────────

static NV2ATexture *nv2a_framebuffer_texture(int fb_id)
{
    if (fb_id <= 0 || fb_id >= g_framebuffer_count ||
        !g_framebuffers[fb_id].used) return nullptr;
    const uint32_t texture_id = g_framebuffers[fb_id].texture_id;
    if (texture_id >= MAX_TEXTURES || !g_textures[texture_id].used) return nullptr;
    return &g_textures[texture_id];
}

static void nv2a_wait_for_gpu(void)
{
    pb_wait_until_gr_not_busy();
}

static void nv2a_store_active_framebuffer(void)
{
    if (g_current_framebuffer <= 0) return;
    NV2ATexture *dst = nv2a_framebuffer_texture(g_current_framebuffer);
    DWORD *src = pb_extra_buffer(0);
    if (!dst || !dst->vram || !src) return;
    dst->alias_vram = nullptr;
    dst->alias_pitch = 0;
    const uint32_t src_pitch = pb_back_buffer_pitch();
    nv2a_wait_for_gpu();
    for (uint32_t y = 0; y < dst->height; ++y) {
        nv2a_copy_pixels(
            (uint32_t *)((uint8_t *)dst->vram + y * dst->pitch),
            (const uint32_t *)((const uint8_t *)src + y * src_pitch),
            dst->width);
    }
    ++dst->content_version;
}

static void nv2a_set_target_surface(int fb_id)
{
    if (fb_id == FB_BACK_BUFFER) {
        pb_target_back_buffer();
        g_target_width = pb_back_buffer_width();
        g_target_height = pb_back_buffer_height();
    } else {
        pb_target_extra_buffer(0);
        g_target_width = g_framebuffers[fb_id].width;
        g_target_height = g_framebuffers[fb_id].height;
    }
    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_SURFACE_CLIP_HORIZONTAL,
                 g_target_width << 16);
    p = pb_push1(p, NV097_SET_SURFACE_CLIP_VERTICAL,
                 g_target_height << 16);
    // Logical framebuffers share the D24S8 surface. NV2A's native W-buffer
    // path consumes the original clip W retained by the vertex program.
    p = pb_push1(p, NV20_TCL_PRIMITIVE_3D_W_YUV_FPZ_FLAGS, 0x00110001u);
    pb_end(p);
    nv2a_upload_viewport_constants();
    nv2a_set_scissor(g_rs.sc_x, g_rs.sc_y, g_rs.sc_w, g_rs.sc_h);
}

static int nv2a_create_framebuffer(void)
{
    if (g_framebuffer_count >= MAX_FRAMEBUFFERS) {
        sysLogPrintf(LOG_ERROR, "NV2A: framebuffer pool exhausted");
        return FB_BACK_BUFFER;
    }
    const int id = g_framebuffer_count++;
    NV2AFramebuffer &fb = g_framebuffers[id];
    fb = {};
    fb.used = true;
    fb.texture_id = MAX_GAME_TEXTURES + (uint32_t)id - 1u;
    g_textures[fb.texture_id].used = true;
    return id;
}

static void nv2a_update_framebuffer_parameters(int fb_id,
    uint32_t width, uint32_t height, uint32_t msaa_level,
    bool opengl_invert_y, bool render_target,
    bool has_depth_buffer, bool can_extract_depth)
{
    (void)msaa_level;
    (void)can_extract_depth;
    if (fb_id == FB_BACK_BUFFER) {
        g_framebuffers[0].used = true;
        g_framebuffers[0].width = width;
        g_framebuffers[0].height = height;
        return;
    }
    if (fb_id <= 0 || fb_id >= g_framebuffer_count) return;
    if (!width || !height || width > 1024 || height > 1024) {
        sysLogPrintf(LOG_ERROR, "NV2A: invalid framebuffer %d size %ux%u",
                     fb_id, width, height);
        return;
    }

    NV2AFramebuffer &fb = g_framebuffers[fb_id];
    fb.invert_y = opengl_invert_y;
    fb.render_target = render_target;
    fb.has_depth = has_depth_buffer;
    fb.width = width;
    fb.height = height;

    NV2ATexture &texture = g_textures[fb.texture_id];
    texture.alias_vram = nullptr;
    texture.alias_pitch = 0;
    const uint32_t pitch = (width * 4u + 63u) & ~63u;
    const uint32_t bytes = pitch * height;
    if (texture.vram &&
        (texture.width != width || texture.height != height ||
         texture.pitch != pitch)) {
        MmFreeContiguousMemory(texture.vram);
        texture.vram = nullptr;
    }
    if (!texture.vram) {
        texture.vram = (uint32_t *)MmAllocateContiguousMemoryEx(
            bytes, 0, 0xFFFFFFFF, 0, PAGE_READWRITE | PAGE_WRITECOMBINE);
        if (!texture.vram) {
            sysLogPrintf(LOG_ERROR,
                         "NV2A: framebuffer allocation failed (%ux%u)",
                         width, height);
            return;
        }
        memset(texture.vram, 0, bytes);
    }
    texture.used = true;
    texture.width = width;
    texture.height = height;
    texture.storage_width = width;
    texture.storage_height = height;
    texture.pitch = pitch;
    texture.fmt_word = NV2A_LINEAR_A8R8G8B8_FORMAT;
    texture.swizzled = false;
    texture.linear_filter = true;
    texture.address_s = 3;
    texture.address_t = 3;
    texture.mipmaps = false;
    ++texture.content_version;
}

static bool nv2a_start_draw_to_framebuffer(int fb_id, float noise_scale)
{
    if (fb_id < 0 || fb_id >= g_framebuffer_count ||
        !g_framebuffers[fb_id].used) return false;
    g_clip_invert_y = g_framebuffers[fb_id].invert_y;
    if (fb_id == g_current_framebuffer) {
        g_noise_scale = noise_scale != 0.0f ? 1.0f / noise_scale : 1.0f;
        return true;
    }
    nv2a_store_active_framebuffer();
    g_current_framebuffer = fb_id;
    g_noise_scale = noise_scale != 0.0f ? 1.0f / noise_scale : 1.0f;
    if (fb_id > FB_BACK_BUFFER) {
        NV2ATexture *target = nv2a_framebuffer_texture(fb_id);
        if (target) {
            // Rendering replaces the logical framebuffer's aliased snapshot.
            target->alias_vram = nullptr;
            target->alias_pitch = 0;
        }
    }
    nv2a_set_target_surface(fb_id);
    return true;
}

static uint32_t nv2a_average_rect(const uint8_t *base, uint32_t pitch,
                                  uint32_t x0, uint32_t y0,
                                  uint32_t x1, uint32_t y1)
{
    uint32_t a = 0, r = 0, g = 0, b = 0, count = 0;
    for (uint32_t y = y0; y < y1; ++y) {
        const uint32_t *row = (const uint32_t *)(base + y * pitch);
        for (uint32_t x = x0; x < x1; ++x) {
            const uint32_t px = row[x];
            b += px & 255u;
            g += (px >> 8) & 255u;
            r += (px >> 16) & 255u;
            a += px >> 24;
            ++count;
        }
    }
    if (!count) return 0;
    return ((a / count) << 24) | ((r / count) << 16) |
           ((g / count) << 8) | (b / count);
}

static uint32_t nv2a_average_rect(const NV2ATexture &texture,
                                  const uint8_t *base, uint32_t pitch,
                                  uint32_t x0, uint32_t y0,
                                  uint32_t x1, uint32_t y1)
{
    if (!texture.swizzled) {
        return nv2a_average_rect(base, pitch, x0, y0, x1, y1);
    }

    uint32_t a = 0, r = 0, g = 0, b = 0, count = 0;
    for (uint32_t y = y0; y < y1; ++y) {
        for (uint32_t x = x0; x < x1; ++x) {
            const uint32_t index = nv2a_swizzled_pixel_index(
                x, y, texture.storage_width, texture.storage_height);
            const uint32_t px = ((const uint32_t *)base)[index];
            b += px & 255u;
            g += (px >> 8) & 255u;
            r += (px >> 16) & 255u;
            a += px >> 24;
            ++count;
        }
    }
    if (!count) return 0;
    return ((a / count) << 24) | ((r / count) << 16) |
           ((g / count) << 8) | (b / count);
}

static NV2ATexture *nv2a_prepare_blur_texture(int source_id)
{
    if (source_id <= 0 || source_id >= MAX_TEXTURES) return nullptr;
    NV2ATexture &source = g_textures[source_id];
    if (!nv2a_texture_data(source) || !source.width || !source.height) return nullptr;
    if (g_blur_source_id == source_id &&
        g_blur_source_version == source.content_version &&
        g_blur_texture.vram) {
        return &g_blur_texture;
    }

    // The PC shader averages a 4x4 neighbourhood. Downsampling that same
    // neighbourhood once and letting the texture unit linearly reconstruct it
    // gives the NV2A equivalent without sixteen fragment samples per pixel.
    const uint32_t width = (source.width + 3u) / 4u;
    const uint32_t height = (source.height + 3u) / 4u;
    const uint32_t pitch = (width * 4u + 63u) & ~63u;
    const uint32_t bytes = pitch * height;
    if (g_blur_texture.vram &&
        (g_blur_texture.width != width ||
         g_blur_texture.height != height ||
         g_blur_texture.pitch != pitch)) {
        MmFreeContiguousMemory(g_blur_texture.vram);
        g_blur_texture.vram = nullptr;
    }
    if (!g_blur_texture.vram) {
        g_blur_texture.vram = (uint32_t *)MmAllocateContiguousMemoryEx(
            bytes, 0, 0xFFFFFFFF, 0, PAGE_READWRITE | PAGE_WRITECOMBINE);
        if (!g_blur_texture.vram) {
            sysLogPrintf(LOG_ERROR, "NV2A: blur texture allocation failed (%ux%u)",
                         width, height);
            return nullptr;
        }
    }

    // A previous-frame alias points at the completed front surface and is
    // immutable for this frame, so reading it does not require draining the
    // current back-buffer command stream. Owned textures may still have been
    // written by the GPU and retain the conservative synchronization.
    if (!source.alias_vram) {
        nv2a_wait_for_gpu();
    }
    const uint8_t *src = (const uint8_t *)nv2a_texture_data(source);
    const uint32_t source_pitch = nv2a_texture_pitch(source);
    uint8_t *dst = (uint8_t *)g_blur_texture.vram;
    for (uint32_t y = 0; y < height; ++y) {
        uint32_t *row = (uint32_t *)(dst + y * pitch);
        const uint32_t y0 = y * 4u;
        uint32_t y1 = y0 + 4u;
        if (y1 > source.height) y1 = source.height;
        for (uint32_t x = 0; x < width; ++x) {
            const uint32_t x0 = x * 4u;
            uint32_t x1 = x0 + 4u;
            if (x1 > source.width) x1 = source.width;
            row[x] = nv2a_average_rect(source, src, source_pitch,
                                       x0, y0, x1, y1);
        }
        memset(row + width, 0, pitch - width * 4u);
    }

    g_blur_texture.used = true;
    g_blur_texture.width = width;
    g_blur_texture.height = height;
    g_blur_texture.storage_width = width;
    g_blur_texture.storage_height = height;
    g_blur_texture.pitch = pitch;
    g_blur_texture.fmt_word = NV2A_LINEAR_A8R8G8B8_FORMAT;
    g_blur_texture.swizzled = false;
    g_blur_texture.linear_filter = true;
    g_blur_texture.address_s = 3;
    g_blur_texture.address_t = 3;
    g_blur_texture.mipmaps = false;
    g_blur_texture.external = source.external;
    g_blur_texture.external_id = source.external_id;
    ++g_blur_texture.content_version;
    g_blur_source_id = source_id;
    g_blur_source_version = source.content_version;
    return &g_blur_texture;
}

static void nv2a_copy_framebuffer(int fb_dst, int fb_src,
                                   int left, int top, bool flip_y, bool use_back)
{
    if (fb_dst <= 0 || fb_dst >= g_framebuffer_count ||
        fb_src < 0 || fb_src >= g_framebuffer_count) return;
    NV2ATexture *dst_texture = nv2a_framebuffer_texture(fb_dst);
    if (!dst_texture || !dst_texture->vram) return;

    const uint8_t *src_base = nullptr;
    uint32_t src_pitch = 0, src_width = 0, src_height = 0;
    if (fb_src == FB_BACK_BUFFER) {
        const bool use_presented = !use_back && g_last_presented_fb;
        src_base = (const uint8_t *)(use_presented
            ? g_last_presented_fb : g_dbg_fb);
        src_pitch = use_presented
            ? g_last_presented_pitch : g_dbg_fb_pitch;
        src_width = pb_back_buffer_width();
        src_height = pb_back_buffer_height();
    } else if (fb_src == g_current_framebuffer) {
        src_base = (const uint8_t *)pb_extra_buffer(0);
        src_pitch = pb_back_buffer_pitch();
        src_width = g_framebuffers[fb_src].width;
        src_height = g_framebuffers[fb_src].height;
    } else {
        NV2ATexture *src_texture = nv2a_framebuffer_texture(fb_src);
        if (!src_texture || !nv2a_texture_data(*src_texture)) return;
        src_base = (const uint8_t *)nv2a_texture_data(*src_texture);
        src_pitch = nv2a_texture_pitch(*src_texture);
        src_width = src_texture->width;
        src_height = src_texture->height;
    }
    if (!src_base || !src_width || !src_height) return;

    const uint32_t dst_width = dst_texture->width;
    const uint32_t dst_height = dst_texture->height;
    ++g_dbg_fb_copy_calls;
    g_dbg_fb_copy_pixels += (uint64_t)dst_width * dst_height;
    uint32_t copy_width = src_width;
    uint32_t copy_height = src_height;
    int src_left = 0;
    int src_top = 0;
    const bool scaled = left < 0 || top < 0;

    // GL_FRONT copies used by the motion-blur scheduler need the previously
    // presented image, not a mutable duplicate. pbkit's triple buffering keeps
    // that completed surface alive while the next frame targets another one.
    // Referencing it directly removes a GPU readback and 1.2 MB CPU copy every
    // frame. Immediate GL_BACK effects intentionally retain the copy path.
    if (fb_src == FB_BACK_BUFFER && !use_back && g_last_presented_fb &&
        scaled && src_width == dst_width && src_height == dst_height) {
        dst_texture->alias_vram = (uint32_t *)g_last_presented_fb;
        dst_texture->alias_pitch = g_last_presented_pitch;
        ++dst_texture->content_version;
        ++g_dbg_fb_copy_fast;
        ++g_dbg_fb_copy_alias;
        return;
    }

    nv2a_wait_for_gpu();
    dst_texture->alias_vram = nullptr;
    dst_texture->alias_pitch = 0;
    uint8_t *dst_base = (uint8_t *)dst_texture->vram;
    if (!scaled) {
        src_left = left;
        // gfx_pc converted a top-down game Y into OpenGL bottom coordinates.
        // Convert it back for the Xbox's top-down linear surface.
        src_top = flip_y ? (int)src_height - top - 1 : top;
        copy_width = dst_width;
        copy_height = dst_height;
    }

    const bool box_filter = scaled &&
        (src_width >= dst_width * 4u || src_height >= dst_height * 4u);

    // Previous-frame and menu captures are commonly 1:1. Copy complete rows
    // rather than doing two integer divisions for every pixel.
    if (scaled && src_width == dst_width && src_height == dst_height) {
        ++g_dbg_fb_copy_fast;
        for (uint32_t y = 0; y < dst_height; ++y) {
            nv2a_copy_pixels(
                (uint32_t *)(dst_base + y * dst_texture->pitch),
                (const uint32_t *)(src_base + y * src_pitch), dst_width);
        }
        ++dst_texture->content_version;
        return;
    }
    if (!scaled && src_left >= 0 && src_top >= 0 &&
        (uint32_t)src_left + dst_width <= src_width &&
        (uint32_t)src_top + dst_height <= src_height) {
        ++g_dbg_fb_copy_fast;
        for (uint32_t y = 0; y < dst_height; ++y) {
            nv2a_copy_pixels(
                (uint32_t *)(dst_base + y * dst_texture->pitch),
                (const uint32_t *)(src_base +
                    ((uint32_t)src_top + y) * src_pitch) + src_left,
                dst_width);
        }
        ++dst_texture->content_version;
        return;
    }

    uint16_t source_x[1024];
    ++g_dbg_fb_copy_scaled;
    if (box_filter) ++g_dbg_fb_copy_box;
    if (!box_filter) {
        for (uint32_t x = 0; x < dst_width; ++x) {
            int sx = src_left + (int)(x * copy_width / dst_width);
            if (sx < 0) sx = 0;
            if (sx >= (int)src_width) sx = (int)src_width - 1;
            source_x[x] = (uint16_t)sx;
        }
    }
    for (uint32_t y = 0; y < dst_height; ++y) {
        uint32_t *dst_row = (uint32_t *)(dst_base + y * dst_texture->pitch);
        uint32_t nearest_y = 0;
        if (!box_filter) {
            int sy = src_top + (int)(y * copy_height / dst_height);
            if (sy < 0) sy = 0;
            if (sy >= (int)src_height) sy = (int)src_height - 1;
            nearest_y = (uint32_t)sy;
        }
        for (uint32_t x = 0; x < dst_width; ++x) {
            uint32_t px = 0;
            if (box_filter) {
                uint32_t x0 = x * copy_width / dst_width;
                uint32_t x1 = (x + 1) * copy_width / dst_width;
                uint32_t y0 = y * copy_height / dst_height;
                uint32_t y1 = (y + 1) * copy_height / dst_height;
                if (x1 <= x0) x1 = x0 + 1;
                if (y1 <= y0) y1 = y0 + 1;
                if (x1 > src_width) x1 = src_width;
                if (y1 > src_height) y1 = src_height;
                px = nv2a_average_rect(src_base, src_pitch, x0, y0, x1, y1);
            } else {
                px = ((const uint32_t *)(src_base + nearest_y * src_pitch))
                    [source_x[x]];
            }
            dst_row[x] = px;
        }
    }
    ++dst_texture->content_version;
}

static void nv2a_clear_surface(uint32_t mask)
{
    // NV097_CLEAR_SURFACE only affects the region in SET_CLEAR_RECT_*, and the
    // clear values must be programmed too. Without both, the clear is a no-op
    // and stale video memory shows through as coloured streaks.
    const uint32_t w = g_target_width;
    const uint32_t h = g_target_height;

    uint32_t *p = pb_begin();
    p = pb_push1(p, NV097_SET_COLOR_CLEAR_VALUE, 0xFF000000);      // opaque black
    p = pb_push1(p, NV097_SET_ZSTENCIL_CLEAR_VALUE, 0xFFFFFF00);   // far depth
    p = pb_push1(p, NV097_SET_CLEAR_RECT_HORIZONTAL, ((w - 1) << 16));
    p = pb_push1(p, NV097_SET_CLEAR_RECT_VERTICAL,   ((h - 1) << 16));
    p = pb_push1(p, NV097_CLEAR_SURFACE, mask);
    pb_end(p);
}

static void nv2a_clear_framebuffer(bool clear_color, bool clear_depth)
{
    uint32_t mask = 0;
    if (clear_color) mask |= 0xF0; // RGBA channels
    if (clear_depth) mask |= 0x03; // depth + stencil
    if (mask) {
        uint32_t *p = pb_begin();
        p = pb_push1(p, NV097_SET_WINDOW_CLIP_HORIZONTAL,
                     g_target_width << 16);
        p = pb_push1(p, NV097_SET_WINDOW_CLIP_VERTICAL,
                     g_target_height << 16);
        pb_end(p);
        nv2a_clear_surface(mask);
        nv2a_set_scissor(g_rs.sc_x, g_rs.sc_y, g_rs.sc_w, g_rs.sc_h);
    }
}

static void nv2a_resolve_msaa_color_buffer(int fb_id_target, int fb_id_source)
{
    nv2a_copy_framebuffer(fb_id_target, fb_id_source, -1, -1, false, true);
}

static void *nv2a_get_framebuffer_texture_id(int fb_id)
{
    NV2ATexture *texture = nv2a_framebuffer_texture(fb_id);
    return texture ? (void *)(uintptr_t)g_framebuffers[fb_id].texture_id
                   : nullptr;
}

static void nv2a_select_texture_fb(int fb_id)
{
    if (fb_id == FB_BACK_BUFFER) {
        g_active_texture[0] = -1;
        nv2a_disable_texture(0);
        return;
    }
    NV2ATexture *texture = nv2a_framebuffer_texture(fb_id);
    if (!texture || !texture->vram) return;
    g_active_texture[0] = (int)g_framebuffers[fb_id].texture_id;
    g_last_selected_tile = 0;
    texture->linear_filter = true;
    nv2a_bind_texture(0, *texture);
}

// ── Texture filter API ────────────────────────────────────────────────────────

static void nv2a_set_texture_filter(enum FilteringMode mode)
{
    g_filter_mode = mode;
    for (int tile = 0; tile < TEXTURE_TILE_COUNT; ++tile) {
        const int id = g_active_texture[tile];
        if (id > 0 && id < MAX_TEXTURES && g_textures[id].vram) {
            nv2a_bind_texture(tile, g_textures[id]);
        }
    }
}

static enum FilteringMode nv2a_get_texture_filter(void)
{
    return g_filter_mode;
}

static void nv2a_set_mipmap_filter(enum MipmapFilteringMode mode)
{
    g_mipmap_mode = mode;
}

static void nv2a_set_anisotropy_level(int level)
{
    g_anisotropy = level < 1 ? 1 : (level > 2 ? 2 : level);
}

static int nv2a_get_max_anisotropy_level(void)
{
    return 2; // NV2A supports 2x anisotropic filtering
}

// ── API struct ────────────────────────────────────────────────────────────────

struct GfxRenderingAPI gfx_nv2a_api = {
    nv2a_get_name,
    nv2a_get_max_texture_size,
    nv2a_get_clip_parameters,
    nv2a_unload_shader,
    nv2a_load_shader,
    nv2a_create_and_load_new_shader,
    nv2a_lookup_shader,
    nv2a_shader_get_info,
    nv2a_clear_shaders,
    nv2a_new_texture,
    nv2a_select_texture,
    nv2a_upload_texture,
    nv2a_set_sampler_parameters,
    nv2a_set_depth_mode,
    nv2a_set_depth_range,
    nv2a_set_viewport,
    nv2a_set_scissor,
    nv2a_set_use_alpha,
    nv2a_draw_triangles,
    nv2a_init,
    nv2a_on_resize,
    nv2a_start_frame,
    nv2a_end_frame,
    nv2a_finish_render,
    nv2a_create_framebuffer,
    nv2a_update_framebuffer_parameters,
    nv2a_start_draw_to_framebuffer,
    nv2a_copy_framebuffer,
    nv2a_clear_framebuffer,
    nv2a_resolve_msaa_color_buffer,
    nv2a_get_framebuffer_texture_id,
    nv2a_select_texture_fb,
    nv2a_delete_texture,
    nv2a_set_texture_filter,
    nv2a_get_texture_filter,
    nv2a_set_mipmap_filter,
    nv2a_set_anisotropy_level,
    nv2a_get_max_anisotropy_level,
};

#endif // PLATFORM_XBOX
