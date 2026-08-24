// Bounded-memory diffuse texture-pack loader for Original Xbox.

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <zlib.h>

#include "fs.h"
#include "system.h"
#include "ext_texture_xbox.h"
#include "serial_xbox.h"

#define PDTX_MAGIC "PDTXPAK1"
#define PDTX_MAGIC_SIZE 8
#define PDTX_ENTRY_SIZE 16
#define PDTX_MAX_DIMENSION 512u
#define PDTX_MAX_RAW_SIZE (PDTX_MAX_DIMENSION * PDTX_MAX_DIMENSION * 4u)

struct pdtx_entry {
	u32 offset;
	u32 compressed_size;
	u32 raw_size;
	u16 width;
	u16 height;
};

static FILE *g_PdtxFile;
static struct pdtx_entry *g_PdtxEntries;
static u32 g_PdtxEntryCount;
static s32 g_PdtxInitAttempted;
static s32 g_PdtxReportedFirstLoad;
static u8 g_PdtxReportedLarge[8192 / 8];

static u16 readLe16(const u8 *src)
{
	return (u16)(src[0] | src[1] << 8);
}

static u32 readLe32(const u8 *src)
{
	return (u32)src[0] | (u32)src[1] << 8 | (u32)src[2] << 16 | (u32)src[3] << 24;
}

static voidpf pdtxZalloc(voidpf opaque, uInt items, uInt size)
{
	(void)opaque;
	return calloc(items, size);
}

static void pdtxZfree(voidpf opaque, voidpf address)
{
	(void)opaque;
	free(address);
}

static s32 pdtxInit(void)
{
	u8 header[12];
	u8 raw[PDTX_ENTRY_SIZE];
	u32 i;

	if (g_PdtxInitAttempted) {
		return g_PdtxFile != NULL;
	}

	g_PdtxInitAttempted = 1;
	g_PdtxFile = fopen(fsFullPath("ext_tex.pak"), "rb");

	if (!g_PdtxFile) {
		sysLogPrintf(LOG_NOTE, "texture pack: ext_tex.pak not present; using stock textures");
		return 0;
	}

	if (fread(header, 1, sizeof(header), g_PdtxFile) != sizeof(header)
			|| memcmp(header, PDTX_MAGIC, PDTX_MAGIC_SIZE) != 0) {
		sysLogPrintf(LOG_ERROR, "texture pack: invalid archive header");
		xboxExtTextureShutdown();
		return 0;
	}

	g_PdtxEntryCount = readLe32(header + PDTX_MAGIC_SIZE);

	if (g_PdtxEntryCount == 0 || g_PdtxEntryCount > 8192) {
		sysLogPrintf(LOG_ERROR, "texture pack: invalid entry count %lu",
				(unsigned long)g_PdtxEntryCount);
		xboxExtTextureShutdown();
		return 0;
	}

	g_PdtxEntries = calloc(g_PdtxEntryCount, sizeof(*g_PdtxEntries));

	if (!g_PdtxEntries) {
		sysLogPrintf(LOG_ERROR, "texture pack: unable to allocate index");
		xboxExtTextureShutdown();
		return 0;
	}

	for (i = 0; i < g_PdtxEntryCount; i++) {
		struct pdtx_entry *entry = &g_PdtxEntries[i];

		if (fread(raw, 1, sizeof(raw), g_PdtxFile) != sizeof(raw)) {
			sysLogPrintf(LOG_ERROR, "texture pack: truncated index at %lu", (unsigned long)i);
			xboxExtTextureShutdown();
			return 0;
		}

		entry->offset = readLe32(raw + 0);
		entry->compressed_size = readLe32(raw + 4);
		entry->raw_size = readLe32(raw + 8);
		entry->width = readLe16(raw + 12);
		entry->height = readLe16(raw + 14);
	}

	sysLogPrintf(LOG_NOTE, "texture pack: indexed %lu stock slots",
			(unsigned long)g_PdtxEntryCount);
	serialPuts("PDTX: archive indexed\n");
	return 1;
}

int xboxExtTextureExists(uint32_t texture_id)
{
	if (!pdtxInit() || texture_id >= g_PdtxEntryCount) {
		return 0;
	}

	return g_PdtxEntries[texture_id].offset != 0;
}

int xboxExtTextureGetInfo(uint32_t texture_id, uint32_t *width, uint32_t *height)
{
	struct pdtx_entry *entry;

	if (!xboxExtTextureExists(texture_id)) {
		return 0;
	}

	entry = &g_PdtxEntries[texture_id];

	if (entry->width == 0 || entry->height == 0
			|| entry->width > PDTX_MAX_DIMENSION
			|| entry->height > PDTX_MAX_DIMENSION
			|| entry->raw_size != (u32)entry->width * entry->height * 4u
			|| entry->raw_size > PDTX_MAX_RAW_SIZE
			|| entry->compressed_size == 0) {
		sysLogPrintf(LOG_ERROR, "texture pack: invalid entry %04lx", (unsigned long)texture_id);
		return 0;
	}

	if (width) {
		*width = entry->width;
	}

	if (height) {
		*height = entry->height;
	}

	return 1;
}

uint8_t *xboxExtTextureLoad(uint32_t texture_id, uint32_t *width, uint32_t *height)
{
	struct pdtx_entry *entry;
	u8 *compressed;
	u8 *pixels;
	z_stream stream;
	s32 ret;

	if (!xboxExtTextureGetInfo(texture_id, width, height)) {
		return NULL;
	}

	entry = &g_PdtxEntries[texture_id];

	compressed = malloc(entry->compressed_size);
	pixels = malloc(entry->raw_size);

	if (!compressed || !pixels) {
		sysLogPrintf(LOG_ERROR, "texture pack: allocation failed for %04lx",
				(unsigned long)texture_id);
		free(compressed);
		free(pixels);
		return NULL;
	}

	if (fseek(g_PdtxFile, entry->offset, SEEK_SET) != 0
			|| fread(compressed, 1, entry->compressed_size, g_PdtxFile) != entry->compressed_size) {
		sysLogPrintf(LOG_ERROR, "texture pack: read failed for %04lx",
				(unsigned long)texture_id);
		free(compressed);
		free(pixels);
		return NULL;
	}

	memset(&stream, 0, sizeof(stream));
	stream.next_in = compressed;
	stream.avail_in = entry->compressed_size;
	stream.next_out = pixels;
	stream.avail_out = entry->raw_size;
	stream.zalloc = pdtxZalloc;
	stream.zfree = pdtxZfree;
	ret = inflateInit2(&stream, 15);

	if (ret == Z_OK) {
		ret = inflate(&stream, Z_FINISH);
		inflateEnd(&stream);
	}

	free(compressed);

	if (ret != Z_STREAM_END || stream.total_out != entry->raw_size) {
		sysLogPrintf(LOG_ERROR, "texture pack: inflate failed for %04lx (%d, %lu/%lu)",
				(unsigned long)texture_id, ret, (unsigned long)stream.total_out,
				(unsigned long)entry->raw_size);
		free(pixels);
		return NULL;
	}

	if (!g_PdtxReportedFirstLoad) {
		g_PdtxReportedFirstLoad = 1;
		serialPuts("PDTX: first diffuse loaded\n");
	}
	if ((entry->width >= 512 || entry->height >= 512)
			&& !(g_PdtxReportedLarge[texture_id >> 3] & (1u << (texture_id & 7)))) {
		char message[80];
		g_PdtxReportedLarge[texture_id >> 3] |= 1u << (texture_id & 7);
		snprintf(message, sizeof(message), "PDTX: large %04lx %ux%u loaded\n",
				(unsigned long)texture_id, entry->width, entry->height);
		serialPuts(message);
	}
	return pixels;
}

void xboxExtTextureFree(uint8_t *pixels)
{
	free(pixels);
}

void xboxExtTextureShutdown(void)
{
	if (g_PdtxFile) {
		fclose(g_PdtxFile);
	}

	free(g_PdtxEntries);
	g_PdtxFile = NULL;
	g_PdtxEntries = NULL;
	g_PdtxEntryCount = 0;
	g_PdtxReportedFirstLoad = 0;
	memset(g_PdtxReportedLarge, 0, sizeof(g_PdtxReportedLarge));
}
