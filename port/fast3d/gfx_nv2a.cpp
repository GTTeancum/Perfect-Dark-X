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
//   Textured triangles   →  pb INLINE_ARRAY submission
//   Texture objects      →  MmAllocateContiguousMemory VRAM uploads
//   Depth / blend modes  →  NV2A fixed-function state words
//
// The full N64 colour-combiner space is large, so we decode CCFeatures and
// program the register combiners for the most common patterns.  Unknown
// patterns fall back to TEX0 * SHADE.

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

// Texture-unit stride: 0x40 bytes between consecutive texture stages
#define NV097_TEXTURE_OFFSET(t)      (NV097_SET_TEXTURE_OFFSET      + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_FORMAT(t)      (NV097_SET_TEXTURE_FORMAT       + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_ADDRESS(t)     (NV097_SET_TEXTURE_ADDRESS      + (uint32_t)(t) * 0x40u)
#define NV097_TEXTURE_CONTROL0(t)    (NV097_SET_TEXTURE_CONTROL0     + (uint32_t)(t) * 0x40u)
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

// ── Texture management ───────────────────────────────────────────────────────

#define MAX_TEXTURES 512
#define TEXTURE_TILE_COUNT 2

struct NV2ATexture {
    bool        used;
    uint32_t   *vram;        // contiguous physical memory for the texture
    uint32_t    width;
    uint32_t    height;
    uint32_t    fmt_word;    // NV097_SET_TEXTURE_FORMAT value cached
    bool        linear_filter;
};

static NV2ATexture g_textures[MAX_TEXTURES];
static int         g_active_texture[TEXTURE_TILE_COUNT] = { -1, -1 };

static uint32_t nv2a_alloc_texture_id(void)
{
    for (uint32_t i = 1; i < MAX_TEXTURES; ++i) {
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
#define NV2A_REG_DIFFUSE     0x04  // GL_PRIMARY_COLOR_NV (vertex diffuse)
#define NV2A_REG_SPECULAR    0x05  // GL_SECONDARY_COLOR_NV (fog / secondary)
#define NV2A_REG_TEX0        0x08
#define NV2A_REG_TEX1        0x09
#define NV2A_REG_CONST0      0x01  // GL_CONSTANT_COLOR0_NV (prim colour)
#define NV2A_REG_CONST1      0x02  // GL_CONSTANT_COLOR1_NV (env colour)
#define NV2A_REG_COMBINED    0x10  // output of previous combiner stage

// NV2A map (alpha modifier) codes used in ICW
#define NV2A_MAP_UNSIGNED_ID  0x0  // A → A
#define NV2A_MAP_UNSIGNED_INV 0x1  // A → 1 − A

// Build one ICW ABCD input block:  A_MAP, A_REG, B_MAP, B_REG, C_MAP, C_REG, D_MAP, D_REG
static uint32_t icw(uint8_t am, uint8_t ar,
                    uint8_t bm, uint8_t br,
                    uint8_t cm, uint8_t cr,
                    uint8_t dm, uint8_t dr)
{
    return ((uint32_t)am       ) |
           ((uint32_t)ar  <<  4) |
           ((uint32_t)bm  <<  8) |
           ((uint32_t)br  << 12) |
           ((uint32_t)cm  << 16) |
           ((uint32_t)cr  << 20) |
           ((uint32_t)dm  << 24) |
           ((uint32_t)dr  << 28);
}

// Output config word — for our purposes we always write to DISCARD the output
// register and output to the color/alpha of the current stage.
// NV097_SET_COMBINER_COLOR_OCW: bits 4..5 = output AB, 8..9 = output CD, 12..13 = output MUXSUM
#define NV2A_OCW_DEFAULT  0x00001C00  // MUX to sumAB, no bias, no scale

struct CombinerState {
    // Register combiner stage 0 (the only one we configure for simple cases)
    uint32_t color_icw;
    uint32_t alpha_icw;
    uint32_t color_ocw;
    uint32_t alpha_ocw;
    // Constant colours (RGBA packed)
    uint32_t const0;
    uint32_t const1;
    // How many combiner stages to use (1 or 2)
    uint32_t num_stages;
    // Whether textures are needed
    bool use_tex[TEXTURE_TILE_COUNT];
};

struct ShaderProgram {
    uint64_t        shader_id0;
    uint32_t        shader_id1;
    uint8_t         num_inputs;
    bool            used_textures[TEXTURE_TILE_COUNT];
    uint8_t         num_floats;   // vertex stride in floats
    CombinerState   cs;
};

static std::map<std::pair<uint64_t,uint32_t>, ShaderProgram> g_shader_pool;
static ShaderProgram *g_current_shader = nullptr;

// Translate an N64 CC input source (SHADER_* enum) to an NV2A register code.
static uint8_t cc_src_to_nv2a_reg(uint8_t src)
{
    switch (src) {
        case SHADER_TEXEL0:   return NV2A_REG_TEX0;
        case SHADER_TEXEL1:   return NV2A_REG_TEX1;
        case SHADER_TEXEL0A:  return NV2A_REG_TEX0;  // alpha used via alpha ICW
        case SHADER_TEXEL1A:  return NV2A_REG_TEX1;
        case SHADER_INPUT_1:  return NV2A_REG_DIFFUSE;   // shade colour
        case SHADER_INPUT_2:  return NV2A_REG_CONST0;    // prim colour
        case SHADER_INPUT_3:  return NV2A_REG_CONST1;    // env colour
        case SHADER_INPUT_4:  return NV2A_REG_SPECULAR;  // fog colour
        case SHADER_COMBINED: return NV2A_REG_COMBINED;
        case SHADER_1:        return NV2A_REG_ZERO;  // 1 via invert(0) trick
        case SHADER_0:
        default:              return NV2A_REG_ZERO;
    }
}

// Build a CombinerState from decoded CCFeatures.
// We implement (A-B)*C+D by choosing the NV2A combiner formula that best
// matches.  NV2A stage formula: out = A*B + (1-A)*C + D  (sum-of-products).
// Mapping:  A=ccC, B=ccA, C=ccB, D=ccD  gives A*(A-B)+B+D ≈ (ccA-ccB)*ccC+ccD
// which is a good approximation for do_mix / do_multiply cases.
static CombinerState build_combiner_state(const CCFeatures &cc)
{
    CombinerState cs = {};
    cs.num_stages    = 1;
    cs.color_ocw     = NV2A_OCW_DEFAULT;
    cs.alpha_ocw     = NV2A_OCW_DEFAULT;
    cs.const0        = 0xFFFFFFFF;  // prim colour default: white
    cs.const1        = 0x00000000;  // env colour default: transparent

    uint8_t Ar = NV2A_REG_ZERO, Br = NV2A_REG_ZERO,
            Cr = NV2A_REG_ZERO, Dr = NV2A_REG_ZERO;
    uint8_t Am = NV2A_MAP_UNSIGNED_ID, Bm = NV2A_MAP_UNSIGNED_ID,
            Cm = NV2A_MAP_UNSIGNED_ID, Dm = NV2A_MAP_UNSIGNED_ID;

    if (cc.do_single[0][0]) {
        // out = C (single source)
        Cr = cc_src_to_nv2a_reg(cc.c[0][0][2]);
        Ar = NV2A_REG_ZERO;
        Br = NV2A_REG_ZERO;
        Dr = NV2A_REG_ZERO;
    } else if (cc.do_multiply[0][0]) {
        // out = A * C  (OpenGL-style A*B where B=1 → C*A)
        Ar = cc_src_to_nv2a_reg(cc.c[0][0][0]);  // ccA
        Br = cc_src_to_nv2a_reg(cc.c[0][0][2]);  // ccC
        Cr = NV2A_REG_ZERO;
        Dr = NV2A_REG_ZERO;
    } else if (cc.do_mix[0][0]) {
        // lerp(ccB, ccA, ccC) = ccB + ccC*(ccA-ccB)
        // NV2A: A*B + (1-A)*C + D  with A=ccC, B=ccA, C=ccB, D=0
        Ar = cc_src_to_nv2a_reg(cc.c[0][0][2]);  // ccC (interpolant)
        Br = cc_src_to_nv2a_reg(cc.c[0][0][0]);  // ccA
        Cm = NV2A_MAP_UNSIGNED_ID;
        Cr = cc_src_to_nv2a_reg(cc.c[0][0][1]);  // ccB
        Dr = NV2A_REG_ZERO;
    } else {
        // Full (A-B)*C+D — approximate as A*C+D (drop B subtraction)
        Ar = cc_src_to_nv2a_reg(cc.c[0][0][0]);
        Br = cc_src_to_nv2a_reg(cc.c[0][0][2]);
        Cr = NV2A_REG_ZERO;
        Dr = cc_src_to_nv2a_reg(cc.c[0][0][3]);
    }

    // Handle SHADER_1 — requires the UNSIGNED_INVERT map on ZERO
    if (cc.c[0][0][0] == SHADER_1) { Ar = NV2A_REG_ZERO; Am = NV2A_MAP_UNSIGNED_INV; }
    if (cc.c[0][0][1] == SHADER_1) { /* only relevant for mix */ }
    if (cc.c[0][0][2] == SHADER_1) { Cr = NV2A_REG_ZERO; Cm = NV2A_MAP_UNSIGNED_INV; }
    if (cc.c[0][0][3] == SHADER_1) { Dr = NV2A_REG_ZERO; Dm = NV2A_MAP_UNSIGNED_INV; }

    cs.color_icw = icw(Am, Ar, Bm, Br, Cm, Cr, Dm, Dr);

    // Alpha combiner — same logic on alpha channel
    uint8_t aAr = NV2A_REG_ZERO, aBr = NV2A_REG_ZERO,
            aCr = NV2A_REG_ZERO, aDr = NV2A_REG_ZERO;
    uint8_t aAm = NV2A_MAP_UNSIGNED_ID, aBm = NV2A_MAP_UNSIGNED_ID,
            aCm = NV2A_MAP_UNSIGNED_ID, aDm = NV2A_MAP_UNSIGNED_ID;

    if (!cc.opt_alpha) {
        // No alpha — output 1
        aCr = NV2A_REG_ZERO;
        aCm = NV2A_MAP_UNSIGNED_INV;
    } else if (cc.do_single[0][1]) {
        aCr = cc_src_to_nv2a_reg(cc.c[0][1][2]);
    } else if (cc.do_multiply[0][1]) {
        aAr = cc_src_to_nv2a_reg(cc.c[0][1][0]);
        aBr = cc_src_to_nv2a_reg(cc.c[0][1][2]);
    } else {
        aAr = cc_src_to_nv2a_reg(cc.c[0][1][0]);
        aBr = cc_src_to_nv2a_reg(cc.c[0][1][2]);
        aDr = cc_src_to_nv2a_reg(cc.c[0][1][3]);
    }

    cs.alpha_icw = icw(aAm, aAr, aBm, aBr, aCm, aCr, aDm, aDr);

    cs.use_tex[0] = cc.used_textures[0];
    cs.use_tex[1] = cc.used_textures[1];

    return cs;
}

// Apply a CombinerState to the NV2A hardware via pbkit.
static void apply_combiner_state(const CombinerState &cs)
{
    uint32_t *p;

    p = pb_begin();
    // Number of active combiner stages
    pb_push1(p, NV097_SET_COMBINER_CONTROL,
        (cs.num_stages - 1) |          // stages - 1
        (0 << 4) |                     // MUX select: MSB of A
        (0x10 << 8));                  // final combiner: C0 alpha for fog
    pb_end(p);

    p = pb_begin();
    // Stage 0 colour
    pb_push1(p, NV097_COMBINER_COLOR_ICW(0), cs.color_icw);
    pb_push1(p, NV097_COMBINER_COLOR_OCW(0), cs.color_ocw);
    // Stage 0 alpha
    pb_push1(p, NV097_COMBINER_ALPHA_ICW(0), cs.alpha_icw);
    pb_push1(p, NV097_COMBINER_ALPHA_OCW(0), cs.alpha_ocw);
    pb_end(p);

    // For stages 1-7 disable by setting them to pass-through
    static const uint32_t passthru_icw = 0; // all zero = ZERO inputs
    static const uint32_t passthru_ocw = NV2A_OCW_DEFAULT;
    for (int s = 1; s < 8; ++s) {
        p = pb_begin();
        pb_push1(p, NV097_COMBINER_COLOR_ICW(s), passthru_icw);
        pb_push1(p, NV097_COMBINER_COLOR_OCW(s), passthru_ocw);
        pb_push1(p, NV097_COMBINER_ALPHA_ICW(s), passthru_icw);
        pb_push1(p, NV097_COMBINER_ALPHA_OCW(s), passthru_ocw);
        pb_end(p);
    }

    // Final combiner: output = RGB of stage 0, alpha from stage 0 alpha
    // NV097_SET_SPECULAR_FOG_FACTOR: final RGB = E*F + (1-E)*G
    //   with E=ZERO, F=ZERO, G=stage0 output → G = stage0 RGB
    p = pb_begin();
    pb_push1(p, NV097_SET_SPECULAR_FOG_FACTOR + 0,
        (NV2A_REG_ZERO)        |  // E
        (NV2A_REG_ZERO  <<  4) |  // F
        (NV2A_REG_COMBINED << 8)| // G = previous stage combined output
        (NV2A_REG_ZERO  << 12) |  // unused
        (NV2A_REG_ZERO  << 16) |  // A (alpha)
        (0 << 20));                // final alpha = stage0 alpha
    pb_end(p);

    // Constants
    p = pb_begin();
    pb_push1(p, NV097_SET_COMBINER_FACTOR0, cs.const0);
    pb_push1(p, NV097_SET_COMBINER_FACTOR1, cs.const1);
    pb_end(p);
}

// ── Render state ─────────────────────────────────────────────────────────────

static struct {
    int   vp_x, vp_y, vp_w, vp_h;
    int   sc_x, sc_y, sc_w, sc_h;
    bool  depth_test;
    bool  depth_write;
    float zfar;
    bool  use_alpha;
    bool  alpha_modulate;
} g_rs = { 0, 0, 640, 480, 0, 0, 640, 480, true, true, 1.0f, false, false };

static uint32_t g_frame_count = 0;

// ── Framebuffer ───────────────────────────────────────────────────────────────
// Xbox has a fixed double-buffered framebuffer managed by pbkit.
// We expose a single "framebuffer 0" that refers to the back buffer.

#define FB_BACK_BUFFER 0

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
    return { true, false };
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
    if (new_prg) {
        apply_combiner_state(new_prg->cs);
    }
}

static struct ShaderProgram *nv2a_create_and_load_new_shader(uint64_t id0, uint32_t id1)
{
    CCFeatures cc;
    gfx_cc_get_features(id0, id1, &cc);

    ShaderProgram prg = {};
    prg.shader_id0 = id0;
    prg.shader_id1 = id1;
    prg.used_textures[0] = cc.used_textures[0];
    prg.used_textures[1] = cc.used_textures[1];
    prg.num_inputs = cc.num_inputs;
    prg.cs = build_combiner_state(cc);

    // Calculate vertex stride:
    // [x,y,z,w] always present (4 floats)
    // [s,t] per used texture (2 floats each)
    // [r,g,b,a] per input pair (4 floats, packed as needed)
    // [fog r,g,b,a] if fog enabled (4 floats)
    uint8_t nf = 4; // position
    if (cc.used_textures[0]) nf += 2;
    if (cc.used_textures[1]) nf += 2;
    nf += (uint8_t)(cc.num_inputs * 4);
    if (cc.opt_fog)       nf += 4;
    if (cc.opt_grayscale) nf += 4;
    prg.num_floats = nf;

    auto key = std::make_pair(id0, id1);
    g_shader_pool[key] = prg;
    g_current_shader = &g_shader_pool[key];
    apply_combiner_state(g_current_shader->cs);
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

static void nv2a_select_texture(int tile, uint32_t texture_id, bool linear_filter)
{
    if (tile < 0 || tile >= TEXTURE_TILE_COUNT) return;
    g_active_texture[tile] = (int)texture_id;

    if (texture_id == 0 || !g_textures[texture_id].used || !g_textures[texture_id].vram) {
        // Disable texture unit
        uint32_t *p = pb_begin();
        pb_push1(p, NV097_TEXTURE_CONTROL0(tile), 0);  // disable
        pb_end(p);
        return;
    }

    NV2ATexture &t = g_textures[texture_id];
    t.linear_filter = linear_filter;

    uint32_t *p = pb_begin();

    // Texture offset (physical address in VRAM)
    pb_push1(p, NV097_TEXTURE_OFFSET(tile), (uint32_t)(uintptr_t)t.vram);

    // Texture format
    pb_push1(p, NV097_TEXTURE_FORMAT(tile), t.fmt_word);

    // Texture address (clamp S and T)
    pb_push1(p, NV097_TEXTURE_ADDRESS(tile),
        (1 << 0) |  // S: wrap
        (1 << 8));  // T: wrap

    // Filter
    uint32_t min_filter = linear_filter ? 0x03 : 0x02; // LINEAR or NEAREST
    uint32_t mag_filter = linear_filter ? 0x02 : 0x01;
    pb_push1(p, NV097_TEXTURE_FILTER(tile),
        (min_filter << 16) |
        (mag_filter << 24));

    // Enable + size
    uint32_t ctrl0 = 0x40000000; // enable
    ctrl0 |= ((t.width  - 1) << 12);
    ctrl0 |= ((t.height - 1) << 0);
    pb_push1(p, NV097_TEXTURE_CONTROL0(tile), ctrl0);

    pb_push1(p, NV097_TEXTURE_IMAGE_RECT(tile),
        (t.height << 16) | t.width);

    pb_end(p);
}

static void nv2a_upload_texture(const uint8_t *rgba32_buf,
                                 uint32_t width, uint32_t height,
                                 bool gen_mipmaps)
{
    (void)gen_mipmaps; // TODO: mipmap generation

    // Determine current texture slot (we upload to whichever tile was last selected)
    int tex_id = g_active_texture[0];
    if (tex_id <= 0 || tex_id >= MAX_TEXTURES) return;

    NV2ATexture &t = g_textures[tex_id];

    const uint32_t bytes = width * height * 4;

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

    if (!t.vram) {
        sysLogPrintf(LOG_ERROR, "NV2A: MmAllocateContiguousMemory failed (%ux%u)", width, height);
        return;
    }

    // Copy RGBA data — NV2A uses ARGB internally, so we swizzle
    const uint8_t *src = rgba32_buf;
    uint8_t       *dst = (uint8_t *)t.vram;
    for (uint32_t i = 0; i < width * height; ++i) {
        uint8_t r = src[0], g = src[1], b = src[2], a = src[3];
        dst[0] = b;  // B
        dst[1] = g;  // G
        dst[2] = r;  // R
        dst[3] = a;  // A
        src += 4;
        dst += 4;
    }

    t.width  = width;
    t.height = height;

    // NV097_SET_TEXTURE_FORMAT for A8R8G8B8 linear (not swizzled)
    // Bits: COLOR=6 (A8R8G8B8), BASE_SIZE_U/V log2, DMA_A context
    uint32_t log2w = 0, log2h = 0;
    { uint32_t v = width;  while (v >>= 1) ++log2w; }
    { uint32_t v = height; while (v >>= 1) ++log2h; }

    t.fmt_word =
        (1 << 0)  |          // DMA_A = context 1
        (1 << 2)  |          // LINEAR (not swizzled) = bit 2
        (6 << 7)  |          // COLOR_FORMAT = 6 → A8R8G8B8
        (log2w << 20) |      // BASE_SIZE_U
        (log2h << 24);       // BASE_SIZE_V
}

static void nv2a_set_sampler_parameters(int sampler, bool linear_filter,
                                         uint32_t cms, uint32_t cmt,
                                         bool mipmaps)
{
    (void)mipmaps;
    if (sampler < 0 || sampler >= TEXTURE_TILE_COUNT) return;

    int tex_id = g_active_texture[sampler];
    if (tex_id <= 0 || tex_id >= MAX_TEXTURES || !g_textures[tex_id].used) return;

    // Wrap modes: 1=wrap, 2=mirror, 3=clamp
    uint32_t s_wrap = (cms == 0) ? 3 : 1;  // cms==0 means clamp
    uint32_t t_wrap = (cmt == 0) ? 3 : 1;

    uint32_t min_filter = linear_filter ? 0x03 : 0x02;
    uint32_t mag_filter = linear_filter ? 0x02 : 0x01;

    uint32_t *p = pb_begin();
    pb_push1(p, NV097_TEXTURE_ADDRESS(sampler),
        (s_wrap << 0) | (t_wrap << 8));
    pb_push1(p, NV097_TEXTURE_FILTER(sampler),
        (min_filter << 16) | (mag_filter << 24));
    pb_end(p);
}

static void nv2a_delete_texture(uint32_t texID)
{
    if (texID == 0 || texID >= MAX_TEXTURES) return;
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
    (void)depth_compare; (void)depth_source_prim; (void)zmode;
    g_rs.depth_test  = depth_test;
    g_rs.depth_write = depth_write;

    uint32_t *p = pb_begin();
    pb_push1(p, NV097_SET_DEPTH_TEST_ENABLE,  depth_test  ? 1 : 0);
    pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE, depth_write ? 1 : 0);
    // LEQUAL (3) is a reasonable default
    pb_push1(p, NV097_SET_DEPTH_FUNC, 3);
    pb_end(p);
}

static void nv2a_set_depth_range(float znear, float zfar)
{
    (void)znear;
    g_rs.zfar = zfar;
}

static void nv2a_set_viewport(int x, int y, int w, int h)
{
    g_rs.vp_x = x;
    g_rs.vp_y = y;
    g_rs.vp_w = w;
    g_rs.vp_h = h;

    // NV2A viewport: offset + scale from NDC → screen
    float ox = (float)x + (float)w * 0.5f;
    float oy = (float)y + (float)h * 0.5f;
    float sx = (float)w * 0.5f;
    float sy = (float)h * 0.5f;

    uint32_t *p = pb_begin();
    pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 0*4, f2u(ox));
    pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 1*4, f2u(oy));
    pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 2*4, f2u(0.5f));   // Z offset
    pb_push1(p, NV097_SET_VIEWPORT_OFFSET + 3*4, f2u(0.0f));
    pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 0*4, f2u(sx));
    pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 1*4, f2u(sy));
    pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 2*4, f2u(0.5f));   // Z scale
    pb_push1(p, NV097_SET_VIEWPORT_SCALE  + 3*4, f2u(1.0f));
    pb_end(p);
}

static void nv2a_set_scissor(int x, int y, int w, int h)
{
    g_rs.sc_x = x;
    g_rs.sc_y = y;
    g_rs.sc_w = w;
    g_rs.sc_h = h;

    // Use NV2A window clip region 0 as scissor.
    // Format: bits [11:0] = min (inclusive), bits [27:16] = max (exclusive).
    uint32_t *p = pb_begin();
    pb_push1(p, NV097_SET_WINDOW_CLIP_HORIZONTAL, ((uint32_t)(x + w) << 16) | (uint32_t)x);
    pb_push1(p, NV097_SET_WINDOW_CLIP_VERTICAL,   ((uint32_t)(y + h) << 16) | (uint32_t)y);
    pb_end(p);
}

static void nv2a_set_use_alpha(bool use_alpha, bool modulate)
{
    g_rs.use_alpha      = use_alpha;
    g_rs.alpha_modulate = modulate;

    uint32_t *p = pb_begin();
    if (use_alpha) {
        pb_push1(p, NV097_SET_BLEND_ENABLE, 1);
        if (modulate) {
            // Premultiplied-alpha style: SRC_ALPHA, ONE
            pb_push1(p, NV097_SET_BLEND_FUNC_SFACTOR, 0x0302); // GL_SRC_ALPHA
            pb_push1(p, NV097_SET_BLEND_FUNC_DFACTOR, 0x0303); // GL_ONE_MINUS_SRC_ALPHA
        } else {
            // Standard alpha blend
            pb_push1(p, NV097_SET_BLEND_FUNC_SFACTOR, 0x0302); // GL_SRC_ALPHA
            pb_push1(p, NV097_SET_BLEND_FUNC_DFACTOR, 0x0303); // GL_ONE_MINUS_SRC_ALPHA
        }
        pb_push1(p, NV097_SET_BLEND_EQUATION, 0x8006); // GL_FUNC_ADD
    } else {
        pb_push1(p, NV097_SET_BLEND_ENABLE, 0);
    }
    pb_end(p);
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

static void nv2a_draw_triangles(float buf_vbo[], size_t buf_vbo_len,
                                 size_t buf_vbo_num_tris)
{
    if (!g_current_shader || buf_vbo_num_tris == 0) return;

    const ShaderProgram &prg   = *g_current_shader;
    const size_t         stride = prg.num_floats;
    const size_t         nverts = buf_vbo_num_tris * 3;

    // Declare vertex format to NV2A.
    // We use DRAW_ARRAYS with inline data (inline array mode).
    // First, set vertex attribute sizes.
    uint32_t *p;

    // Position: 4 floats
    p = pb_begin();
    pb_push1(p, NV097_SET_VERTEX_DATA_ARRAY_FORMAT + 0*4,
        NV097_SET_VERTEX_DATA_ARRAY_FORMAT_TYPE_F |
        (4 << 8) |              // component count
        (stride * 4 << 16));    // stride in bytes
    pb_end(p);

    // We'll interpret the float buffer and push vertex data inline.
    // Layout in buf_vbo per the shader's attrib order:
    //   [x,y,z,w]  [s0,t0]?  [s1,t1]?  [inputs 4f each]  [fog 4f]?

    // For inline arrays, push NV097_SET_BEGIN_END → data → END
    p = pb_begin();
    pb_push1(p, NV097_SET_BEGIN_END, NV097_SET_BEGIN_END_OP_TRIANGLES);
    pb_end(p);

    for (size_t vi = 0; vi < nverts; ++vi) {
        const float *v = buf_vbo + vi * stride;
        size_t off = 0;

        float px = v[off++], py = v[off++], pz = v[off++], pw = v[off++];
        float s0 = 0, t0 = 0, s1 = 0, t1 = 0;

        if (prg.used_textures[0]) { s0 = v[off++]; t0 = v[off++]; }
        if (prg.used_textures[1]) { s1 = v[off++]; t1 = v[off++]; }

        // Colour inputs — the first input is the shade/diffuse colour
        float cr = 0, cg = 0, cb = 0, ca = 1;
        if (prg.num_inputs >= 1) {
            cr = v[off++]; cg = v[off++]; cb = v[off++]; ca = v[off++];
        } else {
            off += 0;
        }
        // Skip remaining inputs (prim, env already handled via constants)
        for (int ii = 1; ii < prg.num_inputs; ++ii) off += 4;

        // (CCFeatures.opt_fog would require 4 more floats here — skip for now)

        // Submit one vertex as inline data
        p = pb_begin();
        // Texture coordinates
        if (prg.used_textures[0]) {
            pb_push2f(p, NV097_SET_TEXCOORD0_2F, s0, t0);
        }
        if (prg.used_textures[1]) {
            pb_push2f(p, NV097_SET_TEXCOORD1_2F, s1, t1);
        }
        // Diffuse colour as 4 floats (RGBA) — nv_regs.h defines 4F form, not 4UB
        pb_push4f(p, NV097_SET_DIFFUSE_COLOR4F, cr, cg, cb, ca);
        // Specular/fog — zero out
        pb_push4f(p, NV097_SET_SPECULAR_COLOR4F, 0.0f, 0.0f, 0.0f, 0.0f);
        // Position — must be last to trigger vertex emit
        pb_push4f(p, NV097_SET_VERTEX4F, px, py, pz, pw);
        pb_end(p);
    }

    p = pb_begin();
    pb_push1(p, NV097_SET_BEGIN_END, NV097_SET_BEGIN_END_OP_END);
    pb_end(p);
}

// ── Frame lifecycle ────────────────────────────────────────────────────────────

static void nv2a_init(void)
{
    memset(g_textures, 0, sizeof(g_textures));
    g_active_texture[0] = g_active_texture[1] = -1;
    g_frame_count = 0;

    // Set initial NV2A state
    uint32_t *p;

    // Disable lighting
    p = pb_begin();
    pb_push1(p, NV097_SET_LIGHTING_ENABLE, 0);
    pb_push1(p, NV097_SET_SPECULAR_ENABLE, 0);
    pb_end(p);

    // Culling off (N64 uses explicit winding)
    p = pb_begin();
    pb_push1(p, NV097_SET_CULL_FACE_ENABLE, 0);
    pb_end(p);

    // Depth test on by default
    p = pb_begin();
    pb_push1(p, NV097_SET_DEPTH_TEST_ENABLE,  1);
    pb_push1(p, NV097_SET_DEPTH_WRITE_ENABLE, 1);
    pb_push1(p, NV097_SET_DEPTH_FUNC, 3); // LEQUAL
    pb_end(p);

    // Alpha test off
    p = pb_begin();
    pb_push1(p, NV097_SET_ALPHA_TEST_ENABLE, 0);
    pb_end(p);

    // Blending off by default
    p = pb_begin();
    pb_push1(p, NV097_SET_BLEND_ENABLE, 0);
    pb_end(p);

    // Disable all texture units initially
    for (int t = 0; t < 4; ++t) {
        p = pb_begin();
        pb_push1(p, NV097_TEXTURE_CONTROL0(t), 0);
        pb_end(p);
    }

    sysLogPrintf(LOG_NOTE, "NV2A renderer initialised");
}

static void nv2a_on_resize(void)
{
    // pbkit manages the back buffer; nothing to do here
}

static void nv2a_start_frame(void)
{
    pb_wait_for_vbl();
    pb_target_back_buffer();

    uint32_t *p = pb_begin();
    // Clear colour + depth
    pb_push1(p, NV097_SET_SURFACE_CLIP_HORIZONTAL, (pb_back_buffer_width()  << 16));
    pb_push1(p, NV097_SET_SURFACE_CLIP_VERTICAL,   (pb_back_buffer_height() << 16));
    pb_end(p);

    ++g_frame_count;
}

static void nv2a_end_frame(void)
{
    // Nothing additional needed before finish_render
}

static void nv2a_finish_render(void)
{
    pb_show_front_screen();
}

// ── Framebuffer API (stub — Xbox has fixed double-buffer) ─────────────────────

static int nv2a_create_framebuffer(void)
{
    return FB_BACK_BUFFER;
}

static void nv2a_update_framebuffer_parameters(int fb_id,
    uint32_t width, uint32_t height, uint32_t msaa_level,
    bool opengl_invert_y, bool render_target,
    bool has_depth_buffer, bool can_extract_depth)
{
    (void)fb_id; (void)width; (void)height; (void)msaa_level;
    (void)opengl_invert_y; (void)render_target;
    (void)has_depth_buffer; (void)can_extract_depth;
}

static bool nv2a_start_draw_to_framebuffer(int fb_id, float noise_scale)
{
    (void)fb_id; (void)noise_scale;
    return true;
}

static void nv2a_copy_framebuffer(int fb_dst, int fb_src,
                                   int left, int top, bool flip_y, bool use_back)
{
    (void)fb_dst; (void)fb_src; (void)left; (void)top;
    (void)flip_y; (void)use_back;
    // TODO: implement screen-space blit via pbkit BLIT_2D engine
}

static void nv2a_clear_framebuffer(bool clear_color, bool clear_depth)
{
    uint32_t *p = pb_begin();
    if (clear_color || clear_depth) {
        uint32_t mask = 0;
        if (clear_color) mask |= 0xF0; // RGBA channels
        if (clear_depth) mask |= 0x03; // depth + stencil
        pb_push1(p, NV097_CLEAR_SURFACE, mask);
    }
    pb_end(p);
}

static void nv2a_resolve_msaa_color_buffer(int fb_id_target, int fb_id_source)
{
    (void)fb_id_target; (void)fb_id_source;
    // No MSAA on Xbox
}

static void *nv2a_get_framebuffer_texture_id(int fb_id)
{
    (void)fb_id;
    return nullptr;
}

static void nv2a_select_texture_fb(int fb_id)
{
    (void)fb_id;
}

// ── Texture filter API ────────────────────────────────────────────────────────

static FilteringMode     g_filter_mode    = FILTER_LINEAR;
static MipmapFilteringMode g_mipmap_mode  = MIPMAP_DISABLED;
static int               g_anisotropy     = 1;

static void nv2a_set_texture_filter(enum FilteringMode mode)
{
    g_filter_mode = mode;
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
    g_anisotropy = level;
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
