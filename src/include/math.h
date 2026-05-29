#ifndef IN_MATH_H
#define IN_MATH_H

#if !defined(PLATFORM_N64) && !defined(PLATFORM_XBOX)
#include_next <math.h>
#undef M_PI
#undef M_TAU
#endif

// fabsf/roundf: declared here for N64/Xbox ffreestanding targets where
// no system math.h is available; hosted targets get them from math.h above.
float fabsf(float x);
float roundf(float x);

// @bug?
#define M_BADPI 3.141092641f
#define M_PI    3.141592741f

#define M_BADTAU (M_BADPI * 2)
#define M_TAU    (M_PI * 2)

#define BADDEG2RAD(deg) ((deg) * (M_BADPI / 180.0f))
#define DEG2RAD(deg)    ((deg) * (M_PI / 180.0f))

#define RAD2DEG(rad) ((rad) * (180.0f / M_PI))
#define RAD2DEG2(rad) ((rad) * 180.0f / M_PI)
#define BADRAD2DEG(rad) ((rad) * (180.0f / M_BADPI))

#endif
