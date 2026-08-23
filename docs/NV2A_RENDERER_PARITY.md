# PC OpenGL to Xbox NV2A renderer parity

This is the release checklist for `port/fast3d/gfx_nv2a.cpp`. The reference
implementation is `port/fast3d/gfx_opengl.cpp`; the Xbox backend consumes the
same `GfxRenderingAPI` vertex stream and `CCFeatures` description.

## Geometry and raster state

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| Clip-space `vec4` vertices | vp20 passthrough program with original clip W | Implemented |
| Homogeneous frustum clipping | CPU Sutherland-Hodgman clipping against all six `0 <= z <= w` clip planes before the NV2A screen-space divide | Implemented and capture-qualified |
| Perspective interpolation | Original clip W plus projective texture stages | Implemented |
| OpenGL clip-depth ordering | Native NV2A W buffer on the shared D24S8 surface | Implemented and capture-qualified |
| Viewport and depth range | Uploaded vertex-program constants, per-target FBO Y orientation, and NV2A depth range | Implemented |
| Scissor | NV2A window clip region 0 | Implemented |
| Triangle lists | Contiguous write-combined vertex stream and `DRAW_ARRAYS` | Implemented |
| Float RGBA inputs | Native normalized `UB_D3D` colors (the source is N64 8-bit RGBA) | Implemented |
| Back-face/cull decisions | Shared fast3d CPU geometry path before backend submission | Identical to PC |
| Fixed 640x480 output | Native Xbox 480-line pbkit surface and viewport | Implemented |

## Lighting and UI

The PC backend does not run a separate OpenGL lighting model. Perfect Dark and
the shared fast3d layer evaluate lights, material/shade colors, environment
colors, and fog coordinates before calling the rendering API. The Xbox direct
equivalents therefore begin with the same completed vertex colors.

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| Per-vertex world/object lighting | Identical primary RGBA vertex stream | Implemented and gameplay-qualified |
| Independent shade/fog channel | Enabled secondary RGBA vertex stream | Implemented and gameplay-qualified |
| Flat and interpolated shade colors | NV2A register-combiner primary/secondary inputs | Implemented |
| HUD, text, reticles, bars, and menu geometry | Same fast3d triangles, viewport, scissor, combiner, and alpha rules | Implemented and capture-qualified |
| Menu background capture and blur | Persistent framebuffer texture plus cached 4x4 box downsample | Implemented and PC-reference-qualified |
| 4:3 layout and edge clipping | Game-authored 640x480 coordinates and exact scissor rectangles | Implemented |

## Color combiner

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| RGB and alpha `(A-B)*C+D` | One or two register-combiner stages per N64 cycle | Implemented |
| One-cycle and two-cycle modes | Up to four of the eight NV2A stages | Implemented |
| Single, multiply, and mix fast paths | One register-combiner stage | Implemented |
| Texture 0/1, combined, zero, one | Native combiner sources and SPARE0 | Implemented |
| Up to seven dynamic shader inputs | Per-draw allocation across diffuse, specular, const0, const1 | Implemented |
| Constant/varying interpolation | One-pass classification followed by native vertex/constant bindings | Implemented |
| Two independent interpolated colors | Programmable primary and separately enabled secondary color, including secondary alpha | Implemented and capture-qualified |
| Per-cycle/final modular color wrap | NV2A signed intermediate arithmetic and clamp | Approximate; modulo has no fixed-function equivalent |
| Invisible output | Final-combiner alpha zero | Implemented |

The full programmed state is cached. A repeated draw does not re-emit texture,
combiner, or vertex-array state unless its effective values change.

## Textures and sampling

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| RGBA8 uploads | Linear A8R8G8B8 contiguous textures | Implemented |
| NPOT dimensions and pitch | `LU_IMAGE` rectangle textures with aligned pitch | Implemented |
| Repeat, mirror, and clamp | NV2A per-axis address modes plus software clamp coordinates | Implemented |
| Point and linear filtering | Native minification/magnification filters | Implemented |
| Three-point N64 filtering | Native bilinear reconstruction | Equivalent approximation |
| Sharp reconstruction | Correct NV097 box/tent sampler encodings with convolution kernels disabled | Implemented and capture-qualified |
| Blur shader | Cached 4x4 downsample plus linear reconstruction | Implemented NV2A equivalent |
| Requested mipmaps | Base level retained for TMEM-sized NPOT textures | N64 content complete; no mip chain |
| Anisotropy | Enabled when a mipmapped source is available | Implemented |

## Fragment effects

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| Fog `mix(color, fog, factor)` | NV2A final combiner using enabled secondary-color alpha | Implemented and capture-qualified |
| Alpha threshold | Fixed-function alpha test | Implemented |
| Texture-edge discard at 0.19 | Fixed-function alpha test at 49/255 | Implemented |
| Texture-edge surviving alpha forced to 1 | Alpha-tested survivor uses an opaque framebuffer write | Implemented; exact RGB result |
| `NOISE` RGB/alpha combiner source | Screen-space point-sampled scalar noise on texture stage 2, phase-shifted per frame | Implemented |
| Spatial post-combiner alpha noise | Screen-space stage-2 noise writes a D24S8 stencil mask, then the original combiner/blend draw is replayed through that mask | Implemented and stress-qualified |
| Grayscale intensity and tint | Two additional register-combiner stages | Implemented |

## Depth and blending

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| Depth enable/write | Native depth-test and depth-mask state | Implemented |
| LESS and LEQUAL | Native NV2A comparisons | Implemented |
| Decal depth offset | Polygon offset scale and bias | Implemented |
| Standard source-alpha blend | `SRC_ALPHA`, `ONE_MINUS_SRC_ALPHA` | Implemented |
| Extended modulation | `DST_COLOR`, `ZERO` | Implemented |

## Framebuffers and presentation

| PC behavior | NV2A equivalent | Status |
|---|---|---|
| Back buffer plus persistent FBOs | pbkit back buffers, one scratch target, persistent texture storage | Implemented |
| Previous-frame, menu, blur, and character-effect captures | 32 logical framebuffer slots; GL_FRONT motion captures alias the completed triple-buffer surface | Implemented |
| Resolve/copy/scale/flip | CPU-visible linear surfaces with point or box reconstruction | Implemented |
| 16x16 character-cloak captures | Immediate unscaled partial row copy into the game's persistent framebuffer textures | Implemented and qualification-harness-proven |
| Requested framebuffer MSAA | Single-sample D24S8; N64 edge/coverage behavior is handled by texture-edge and stochastic-alpha rules | Equivalent for game content |
| Color/depth clear | Explicit clear values, rectangles, and channel masks | Implemented |
| Present | `pb_finished`, `pb_busy`, `pb_reset`, and front-screen rotation | Implemented |
| External regression capture | Completed previous-frame surface published after GPU completion; XEMU is paused around each debugger memory snapshot | Implemented and 24-frame sequence-qualified |

## Qualification evidence

- Native PC OpenGL oracle captures are written by `PD_GL_CAPTURE_DIR`; these
  confirmed the rotating menu weapon, background scene, translucency ordering,
  and blur composition used as the Xbox reference.
- `PD_XBOX_RENDER_QUALIFY_EFFECTS` is a default-off qualification build switch.
  It routes translucent geometry through the real stochastic-alpha stencil
  replay and performs one immediate copy into an actual game-created 16x16
  cloak target. The XEMU UART proof is `noise 30254 copy 1/1/0/0/0 pix 256`.
- Normal release captures cover boot, title, profile/menu blur, mission UI,
  Defection world geometry, lighting, stars, city textures, HUD, gun animation,
  firing, reload, movement, framebuffer scaling, and direct AC97 output.
- The repo-local harness brackets framebuffer-memory snapshots with QEMU
  `stop`/`cont`, preventing the NV2A from recycling a triple-buffer surface
  while `pmemsave` is reading it. A six-frame coherent gameplay rerun qualified
  this capture path after the full 24-frame release sequence.

## Release maintenance gates

- Keep `PD_XBOX_RENDER_QUALIFY_EFFECTS`, high-frequency trace marks, and
  `PD_XBOX_RENDER_TRACE_FRAME` off in shipped images.
- Re-run a boot-to-gameplay capture after renderer changes and reject visible
  corruption, UART faults, sustained frame-rate regressions, or silent audio.
