#include <PR/ultratypes.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <SDL.h>
#include "platform.h"
#include "config.h"
#include "audio.h"
#include "system.h"

static const s16 *nextBuf;
static u32 nextSize = 0;

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
static volatile bool xboxStageTransition;
static bool xboxTransitionResetOk;
static u64 xboxTransitionStartedUs;
static u32 xboxTransitionDroppedBuffers;
static u32 xboxTransitionDroppedBytes;
extern AC97_DEVICE ac97Device;

static void xboxAudioPrimeSilence(void)
{
	for (u32 i = 0; i < 3; ++i) {
		memset(xboxBuffers[i], 0, XBOX_AUDIO_BUFFER_BYTES);
		XAudioProvideSamples(xboxBuffers[i], 4096, FALSE);
	}
	xboxBufferIndex = 3;
}

static bool xboxAudioStopAndReset(void)
{
	volatile u8 *ac97 = (volatile u8 *)0xfec00000;
	const u64 timeoutTicks = KeQueryPerformanceFrequency() / 10u;
	u64 started;

	// Pause first, then reset both PCM-out bus-master channels. Unlike
	// XAudioPause, this discards CIV/LVI state instead of retaining the current
	// descriptor for a later XAudioPlay call.
	XAudioPause();
	ac97[0x11b] = 0x1e;
	started = KeQueryPerformanceCounter();
	while (ac97[0x11b] & 0x02) {
		if (KeQueryPerformanceCounter() - started >= timeoutTicks) {
			serialPuts("audio: analog DMA reset timed out\n");
			return false;
		}
	}

	ac97[0x17b] = 0x1e;
	started = KeQueryPerformanceCounter();
	while (ac97[0x17b] & 0x02) {
		if (KeQueryPerformanceCounter() - started >= timeoutTicks) {
			serialPuts("audio: digital DMA reset timed out\n");
			return false;
		}
	}

	ac97[0x116] = 0xff;
	ac97[0x176] = 0xff;

	for (u32 i = 0; i < XBOX_AUDIO_BUFFER_COUNT; ++i) {
		ac97Device.pcmOutDescriptor[i].bufferStartAddress = 0;
		ac97Device.pcmOutDescriptor[i].bufferLengthInSamples = 0;
		ac97Device.pcmOutDescriptor[i].bufferControl = 0;
		ac97Device.pcmSpdifDescriptor[i].bufferStartAddress = 0;
		ac97Device.pcmSpdifDescriptor[i].bufferLengthInSamples = 0;
		ac97Device.pcmSpdifDescriptor[i].bufferControl = 0;
		memset(xboxBuffers[i], 0, XBOX_AUDIO_BUFFER_BYTES);
	}

	ac97Device.nextDescriptor = 0;
	ac97Device.mmio = (volatile unsigned int *)ac97;
	ac97Device.mmio[0x110 >> 2] = MmGetPhysicalAddress(
			(void *)&ac97Device.pcmOutDescriptor[0]);
	ac97Device.mmio[0x170 >> 2] = MmGetPhysicalAddress(
			(void *)&ac97Device.pcmSpdifDescriptor[0]);
	xboxBufferIndex = 0;
	xboxRateRemainder = 0;
	nextBuf = NULL;
	nextSize = 0;
	__asm__ __volatile__("sfence" ::: "memory");
	return true;
}
#else
static SDL_AudioDeviceID dev;
#endif

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
	xboxAudioPrimeSilence();
	xboxRateRemainder = 0;
	xboxStageTransition = false;
	xboxTransitionResetOk = true;
	xboxTransitionStartedUs = 0;
	xboxTransitionDroppedBuffers = 0;
	xboxTransitionDroppedBytes = 0;
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

void audioBeginStageTransition(s32 fromStage, s32 toStage)
{
#ifdef PLATFORM_XBOX
	volatile u8 *ac97 = (volatile u8 *)0xfec00000;
	char msg[240];

	if (xboxStageTransition) return;
	xboxStageTransition = true;
	xboxTransitionStartedUs = sysGetMicroseconds();
	xboxTransitionDroppedBuffers = 0;
	xboxTransitionDroppedBytes = 0;
	xboxTransitionResetOk = xboxAudioStopAndReset();

	sysLogPrintf(LOG_NOTE,
			"audio: transition begin from=%d to=%d mode=stopped reset=%u civ=%u lvi=%u sr=%04x cr=%02x next=%u",
			fromStage, toStage, xboxTransitionResetOk ? 1u : 0u,
			(unsigned)ac97[0x114], (unsigned)ac97[0x115],
			*(volatile u16 *)(ac97 + 0x116), (unsigned)ac97[0x11b],
			(unsigned)ac97Device.nextDescriptor);
	snprintf(msg, sizeof(msg),
			"audio: transition begin from=%d to=%d mode=stopped reset=%u civ=%u lvi=%u sr=%04x cr=%02x next=%u\n",
			fromStage, toStage, xboxTransitionResetOk ? 1u : 0u,
			(unsigned)ac97[0x114], (unsigned)ac97[0x115],
			*(volatile u16 *)(ac97 + 0x116), (unsigned)ac97[0x11b],
			(unsigned)ac97Device.nextDescriptor);
	serialPuts(msg);
#else
	(void)fromStage;
	(void)toStage;
#endif
}

void audioEndStageTransition(s32 stage)
{
#ifdef PLATFORM_XBOX
	volatile u8 *ac97 = (volatile u8 *)0xfec00000;
	u64 elapsedUs;
	char msg[240];

	if (!xboxStageTransition) return;
	elapsedUs = sysGetMicroseconds() - xboxTransitionStartedUs;
	// The audio thread continues to run while the main thread loads a stage.
	// Keep its output blocked through this second reset: otherwise descriptors
	// mixed during the spinner survive the first reset and all play at restart.
	xboxTransitionResetOk = xboxAudioStopAndReset() && xboxTransitionResetOk;
	// Start a brand-new descriptor ring containing silence only. Real stage
	// audio cannot enter the ring until xboxStageTransition is cleared below.
	xboxAudioPrimeSilence();
	XAudioPlay();
	xboxStageTransition = false;
	sysLogPrintf(LOG_NOTE,
			"audio: transition end stage=%d ms=%llu mode=restarted reset=%u dropped=%u/%u civ=%u lvi=%u sr=%04x cr=%02x next=%u",
			stage, (unsigned long long)(elapsedUs / 1000u),
			xboxTransitionResetOk ? 1u : 0u,
			(unsigned)xboxTransitionDroppedBuffers,
			(unsigned)xboxTransitionDroppedBytes, (unsigned)ac97[0x114],
			(unsigned)ac97[0x115], *(volatile u16 *)(ac97 + 0x116),
			(unsigned)ac97[0x11b], (unsigned)ac97Device.nextDescriptor);
	snprintf(msg, sizeof(msg),
			"audio: transition end stage=%d ms=%llu mode=restarted reset=%u dropped=%u/%u civ=%u lvi=%u sr=%04x cr=%02x next=%u\n",
			stage, (unsigned long long)(elapsedUs / 1000u),
			xboxTransitionResetOk ? 1u : 0u,
			(unsigned)xboxTransitionDroppedBuffers,
			(unsigned)xboxTransitionDroppedBytes, (unsigned)ac97[0x114],
			(unsigned)ac97[0x115], *(volatile u16 *)(ac97 + 0x116),
			(unsigned)ac97[0x11b], (unsigned)ac97Device.nextDescriptor);
	serialPuts(msg);
#else
	(void)stage;
#endif
}

void audioStopForDashboard(void)
{
#ifdef PLATFORM_XBOX
	nextBuf = NULL;
	nextSize = 0;
	const bool reset = xboxAudioStopAndReset();
	serialPuts(reset
			? "audio: stopped and DMA reset for dashboard IGR\n"
			: "audio: dashboard IGR DMA reset failed; stream paused\n");
#endif
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
		if (xboxStageTransition) {
			++xboxTransitionDroppedBuffers;
			xboxTransitionDroppedBytes += nextSize;
			nextBuf = NULL;
			nextSize = 0;
			return;
		}

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
