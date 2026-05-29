// port/include/zlib.h — zlib shim for freestanding targets (Xbox / N64).
//
// On PLATFORM_XBOX (NXDK, -ffreestanding) the system zlib.h tries to pull in
// sys/types.h via zconf.h, which doesn't exist in the freestanding env.
// We intercept the include here and provide a minimal, self-contained
// declaration of the subset used by rzip_c.c.
// The actual implementation is supplied by NXDK's libzlib.lib at link time.
//
// On hosted platforms we just forward to the real zlib.h.

#ifdef PLATFORM_XBOX

#ifndef PORT_ZLIB_H
#define PORT_ZLIB_H

#include <stddef.h>   /* size_t */

// ── Basic zlib types (subset — no sys/types.h required) ──────────────────────

typedef unsigned char  Byte;
typedef unsigned int   uInt;
typedef unsigned long  uLong;
typedef Byte          *Bytef;
typedef long           z_off_t;
typedef void          *voidp;
typedef void          *voidpf;
typedef const void    *voidpc;

// ── Error codes ───────────────────────────────────────────────────────────────

#define Z_OK              0
#define Z_STREAM_END      1
#define Z_NEED_DICT       2
#define Z_ERRNO          (-1)
#define Z_STREAM_ERROR   (-2)
#define Z_DATA_ERROR     (-3)
#define Z_MEM_ERROR      (-4)
#define Z_BUF_ERROR      (-5)
#define Z_VERSION_ERROR  (-6)

// ── Flush values ─────────────────────────────────────────────────────────────

#define Z_NO_FLUSH        0
#define Z_PARTIAL_FLUSH   1
#define Z_SYNC_FLUSH      2
#define Z_FULL_FLUSH      3
#define Z_FINISH          4
#define Z_BLOCK           5
#define Z_TREES           6

// ── z_stream struct (must match the layout compiled into libzlib.lib) ─────────

typedef struct z_stream_s {
    Bytef    *next_in;
    uInt      avail_in;
    uLong     total_in;
    Bytef    *next_out;
    uInt      avail_out;
    uLong     total_out;
    char     *msg;
    struct internal_state *state;
    voidpf    zalloc;  /* alloc_func — we pass NULL to use the default */
    voidp     zfree;   /* free_func  — we pass NULL */
    voidp     opaque;
    int       data_type;
    uLong     adler;
    uLong     reserved;
} z_stream;
typedef z_stream *z_streamp;

#define Z_NULL  0

// ── Version (must match the libzlib.lib build) ───────────────────────────────

#define ZLIB_VERSION  "1.2.11"
#define ZLIB_VERNUM   0x12b0

// ── API declarations ─────────────────────────────────────────────────────────
// zlib uses a versioned init function; the macros forward to it.

int inflateInit2_ (z_streamp strm, int  windowBits,
                   const char *version, int stream_size);
int inflate       (z_streamp strm, int flush);
int inflateEnd    (z_streamp strm);

#define inflateInit2(strm, wbits) \
    inflateInit2_((strm), (wbits), ZLIB_VERSION, (int)sizeof(z_stream))

int deflateInit_ (z_streamp strm, int level,
                  const char *version, int stream_size);
int deflate       (z_streamp strm, int flush);
int deflateEnd    (z_streamp strm);

#define deflateInit(strm, level) \
    deflateInit_((strm), (level), ZLIB_VERSION, (int)sizeof(z_stream))

#endif /* PORT_ZLIB_H */

#else  /* not PLATFORM_XBOX — use system zlib */

#include_next <zlib.h>

#endif /* PLATFORM_XBOX */
