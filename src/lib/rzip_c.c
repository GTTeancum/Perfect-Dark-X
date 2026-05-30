// see https://github.com/n64decomp/007/blob/master/tools/mktex/src/libpdtex/reader.c
// and https://github.com/doomhack/perfect_dark/blob/master/src/lib/rzip.c

#include <zlib.h>

#include "lib/rzip.h"

void *var80091558; // g_RzipUnused

// Diagnostics: last inflate() return code and produced length, so a failed
// data-segment inflate can be diagnosed post-mortem / in the fatal message.
volatile int g_RzipLastRet   = 0xdead;
volatile unsigned int g_RzipLastTotal = 0;
volatile unsigned int g_RzipLastCalls = 0;

bool rzipIs1172(void *buffer)
{
	const u8* src = buffer;
	return (src[0] == 0x11 && src[1] == 0x72);
}

bool rzipIs1173(void *buffer)
{
	const u8* src = buffer;
	return (src[0] == 0x11 && src[1] == 0x73);
}

static inline s32 rzipInflate1172(z_stream *strm, u8 *src, void *dst)
{
	strm->avail_in = 0x2000;
	strm->next_in = src;

	do {
		strm->avail_out = 0x2000;
		strm->next_out = dst;
		if (inflate(strm, Z_FINISH) == Z_STREAM_ERROR) {
			rmonPrintf("rzipInflate1172: Z_STREAM_ERROR\n");
			return 0;
		}
	} while (strm->avail_out == 0);

	return strm->total_out;
}

static inline s32 rzipInflate1173(z_stream *strm, u8 *src, void *dst, u32 dstLen)
{
	strm->avail_in = -1; // compressed size unknown
	strm->next_in = src;
	strm->avail_out = dstLen;
	strm->next_out = dst;

	// A single inflate() call is not guaranteed to fill the whole output
	// buffer: depending on the zlib build it may return Z_OK after a partial
	// flush with output room still left.  The original code assumed one call
	// always completes, which silently truncated the data segment on some
	// platforms (observed on the Xbox/nxdk zlib build) and corrupted the file
	// offset table.  Loop until the stream ends or the output buffer is full.
	for (;;) {
		const int ret = inflate(strm, Z_SYNC_FLUSH);
		g_RzipLastRet = ret;
		g_RzipLastTotal = (unsigned int)strm->total_out;
		g_RzipLastCalls++;
		if (ret == Z_STREAM_END) {
			break;
		}
		if (ret != Z_OK) {
			// Z_STREAM_ERROR / Z_DATA_ERROR / Z_MEM_ERROR / Z_BUF_ERROR
			rmonPrintf("rzipInflate1173: inflate returned %d (total_out=%u)\n", ret, (u32)strm->total_out);
			return 0;
		}
		if (strm->avail_out == 0) {
			// produced the full requested length
			break;
		}
		// Z_OK with output room remaining: keep going (avail_in is effectively
		// infinite, so inflate will keep making progress until done).
	}

	return strm->total_out;
}

s32 rzipInflate(void *srcp, void *dst, void *scratch)
{
	s32 ret = 0;
	u8 *src = srcp;
	z_stream strm = { 0 };

	ret = inflateInit2(&strm, -15);
	g_RzipLastRet = ret;                       // capture init return code
	g_RzipLastTotal = (unsigned int)sizeof(z_stream); // our sizeof(z_stream)
	if (ret != Z_OK) {
		rmonPrintf("rzipInflate: inflateInit2 failed: %d\n", ret);
		return 0;
	}

	if (rzipIs1173(src)) {
		// 1173, we know the uncompressed length
		const u32 dstLen = ((u32)src[2] << 16) | ((u32)src[3] << 8) | (u32)src[4];
		ret = rzipInflate1173(&strm, src + 5, dst, dstLen);
	} else if (rzipIs1172(src)) {
		// 1172, uncompressed length unknown
		ret = rzipInflate1172(&strm, src + 2, dst);
	} else {
		rmonPrintf("rzipInflate: input not in any known rare zip format\n");
		ret = 0;
	}

	inflateEnd(&strm);

	if (ret) {
		var80091558 = strm.next_in;
		return strm.total_out;
	} else {
		return 0;
	}
}

u32 rzipInit(void)
{
	// this builds tables in the original assembly version, we don't need that
	return 0;
}

void *rzipGetSomething(void)
{
	return var80091558;
}
