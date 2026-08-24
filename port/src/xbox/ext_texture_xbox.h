#ifndef PD_EXT_TEXTURE_XBOX_H
#define PD_EXT_TEXTURE_XBOX_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

int xboxExtTextureExists(uint32_t texture_id);
int xboxExtTextureGetInfo(uint32_t texture_id, uint32_t *width, uint32_t *height);
uint8_t *xboxExtTextureLoad(uint32_t texture_id, uint32_t *width, uint32_t *height);
void xboxExtTextureFree(uint8_t *pixels);
void xboxExtTextureShutdown(void);

#ifdef __cplusplus
}
#endif

#endif
