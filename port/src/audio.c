#include <PR/ultratypes.h>
#include <stdio.h>
#include <string.h>
#include <SDL.h>
#include "platform.h"
#include "config.h"
#include "audio.h"
#include "system.h"

#ifdef PLATFORM_XBOX
#include <xboxkrnl/xboxkrnl.h>
#include <hal/audio.h>
void serialPuts(const char *s);

#define XBOX_AUDIO_RATE 48000u
#define GAME_AUDIO_RATE 22020u
#define XBOX_AUDIO_BUFFER_COUNT 32u
#define XBOX_AUDIO_BUFFER_BYTES 8192u

static u8 *xboxBuffers[XBOX_AUDIO_BUFFER_COUNT];
static u32 xboxBufferIndex;
static u32 xboxRateRemainder;
#else
static SDL_AudioDeviceID dev;
#endif

static const s16 *nextBuf;
static u32 nextSize = 0;

static s32 bufferSize = 512;
static s32 queueLimit = 8192;

s32 audioInit(void)
{
#ifdef PLATFORM_XBOX
	for (u32 i = 0; i < XBOX_AUDIO_BUFFER_COUNT; ++i) {
		xboxBuffers[i] = MmAllocateContiguousMemoryEx(
				XBOX_AUDIO_BUFFER_BYTES, 0, 0xffffffff, 0, PAGE_READWRITE);
		if (!xboxBuffers[i]) {
			sysLogPrintf(LOG_ERROR, "Xbox audio buffer allocation failed at %u", i);
			return -1;
		}
		memset(xboxBuffers[i], 0, XBOX_AUDIO_BUFFER_BYTES);
	}

	XAudioInit(16, 2, NULL, NULL);
	// Prime enough silence to cover boot-time jitter before the first mix.
	for (u32 i = 0; i < 3; ++i) {
		XAudioProvideSamples(xboxBuffers[i], 4096, FALSE);
	}
	xboxBufferIndex = 3;
	xboxRateRemainder = 0;
	XAudioPlay();
	nextBuf = NULL;
	sysLogPrintf(LOG_NOTE, "audio: Xbox AC97 direct stream 48000 Hz, stereo S16");
#else
	if (SDL_InitSubSystem(SDL_INIT_AUDIO) != 0) {
		sysLogPrintf(LOG_ERROR, "SDL audio init error: %s", SDL_GetError());
		return -1;
	}

	SDL_AudioSpec want, have;
	SDL_zero(want);
	want.freq = 22020; // TODO: this might cause trouble for some platforms
	want.format = AUDIO_S16SYS;
	want.channels = 2;
	want.samples = bufferSize;
	want.callback = NULL;

	nextBuf = NULL;

	dev = SDL_OpenAudioDevice(NULL, 0, &want, &have, 0);
	if (dev == 0) {
		sysLogPrintf(LOG_ERROR, "SDL_OpenAudio error: %s", SDL_GetError());
		return -1;
	}

	SDL_PauseAudioDevice(dev, 0);
	sysLogPrintf(LOG_NOTE, "audio: opened %d Hz, %d channels, %d samples, format %04x",
			have.freq, have.channels, have.samples, (unsigned)have.format);
#endif

	return 0;
}

s32 audioGetBytesBuffered(void)
{
#ifdef PLATFORM_XBOX
	// The direct AC97 path is frame-paced. Reporting an empty software queue
	// keeps the game mixer producing its normal two naudio frames per tick.
	return 0;
#else
	return SDL_GetQueuedAudioSize(dev);
#endif
}

s32 audioGetSamplesBuffered(void)
{
	return audioGetBytesBuffered() / 4;
}

void audioSetNextBuffer(const s16 *buf, u32 len)
{
	nextBuf = buf;
	nextSize = len;
#ifdef PLATFORM_XBOX
	{
		static u32 calls;
		if (++calls == 1u) {
			char msg[80];
			snprintf(msg, sizeof(msg), "audio: first mix buffer bytes=%u\n", (unsigned)len);
			serialPuts(msg);
		}
	}
#endif
}

void audioEndFrame(void)
{
#ifdef PLATFORM_XBOX
	{
		static u32 calls;
		if (++calls == 1u) {
			serialPuts("audio: first end-frame\n");
		}
	}
#endif
	if (nextBuf && nextSize) {
#ifdef PLATFORM_XBOX
		const u32 inFrames = nextSize / (2u * sizeof(*nextBuf));
		u32 outFrames = 0;
		if (inFrames) {
			const u32 scaled = inFrames * XBOX_AUDIO_RATE + xboxRateRemainder;
			outFrames = scaled / GAME_AUDIO_RATE;
			xboxRateRemainder = scaled % GAME_AUDIO_RATE;
		}

		const u32 maxFrames = XBOX_AUDIO_BUFFER_BYTES / (2u * sizeof(s16));
		if (outFrames > maxFrames) outFrames = maxFrames;
		s16 *dst = (s16 *)xboxBuffers[xboxBufferIndex];
		u32 nonzero = 0;
		s32 bufferPeak = 0;

		for (u32 frame = 0; frame < outFrames; ++frame) {
			const u64 position = outFrames > 1 && inFrames > 1
					? ((u64)frame * (inFrames - 1u) << 16) / (outFrames - 1u)
					: 0;
			const u32 srcFrame = (u32)(position >> 16);
			const u32 nextFrame = srcFrame + 1u < inFrames ? srcFrame + 1u : srcFrame;
			const s32 fraction = (s32)(position & 0xffffu);
			for (u32 channel = 0; channel < 2; ++channel) {
				const s32 a = nextBuf[srcFrame * 2u + channel];
				const s32 b = nextBuf[nextFrame * 2u + channel];
				const s32 sample = a + (s32)(((s64)(b - a) * fraction) >> 16);
				dst[frame * 2u + channel] = (s16)sample;
				const s32 magnitude = sample < 0 ? -sample : sample;
				if (sample) ++nonzero;
				if (magnitude > bufferPeak) bufferPeak = magnitude;
			}
		}

		const u32 outBytes = outFrames * 2u * sizeof(s16);
		if (outBytes) {
			static u32 buffersQueued;
			static u32 bytesQueued;
			static u32 nonzeroSamples;
			static s32 peak;
			XAudioProvideSamples((u8 *)dst, (u16)outBytes, FALSE);
			xboxBufferIndex = (xboxBufferIndex + 1u) % XBOX_AUDIO_BUFFER_COUNT;
			++buffersQueued;
			bytesQueued += outBytes;
			nonzeroSamples += nonzero;
			if (bufferPeak > peak) peak = bufferPeak;
			if ((buffersQueued % 120u) == 0u) {
				char msg[128];
				snprintf(msg, sizeof(msg),
						"audio: ac97 queued=%u bytes=%u nonzero=%u peak=%d\n",
						(unsigned)buffersQueued, (unsigned)bytesQueued,
						(unsigned)nonzeroSamples, (int)peak);
				serialPuts(msg);
			}
		}
#else
		if (audioGetSamplesBuffered() < queueLimit) {
			if (SDL_QueueAudio(dev, nextBuf, nextSize) != 0) {
				sysLogPrintf(LOG_ERROR, "SDL_QueueAudio error: %s", SDL_GetError());
			}
		}
#endif
		nextBuf = NULL;
		nextSize = 0;
	}
}

PD_CONSTRUCTOR static void audioConfigInit(void)
{
	configRegisterInt("Audio.BufferSize", &bufferSize, 0, 1 * 1024 * 1024);
	configRegisterInt("Audio.QueueLimit", &queueLimit, 0, 1 * 1024 * 1024);
}
