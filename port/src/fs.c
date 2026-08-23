#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <ctype.h>
#ifndef PLATFORM_XBOX
#include <limits.h>
#include <unistd.h>
#include <sys/stat.h>
#else
#include <windows.h>      // MAX_PATH
#include <nxdk/mount.h>   // nxMountDrive / nxIsDriveMounted
#include <nxdk/path.h>    // nxGetCurrentXbeNtPath
#include <hal/debug.h>
#endif
#include <PR/ultratypes.h>
#include "config.h"
#include "system.h"
#include "platform.h"
#include "utils.h"
#include "fs.h"
#ifdef PLATFORM_XBOX
#include <windows.h>
#endif

#ifdef PLATFORM_WIN32
#include <direct.h>
#endif

#define DEFAULT_BASEDIR_NAME "data"

static char baseDir[FS_MAXPATH + 1]; // replaces $B
static char modDir[FS_MAXPATH + 1];  // replaces $M
static char saveDir[FS_MAXPATH + 1]; // replaces $S
static char homeDir[FS_MAXPATH + 1]; // replaces $H
static char exeDir[FS_MAXPATH + 1];  // replaces $E

static s32 fsPathIsWritable(const char *path)
{
#ifdef PLATFORM_XBOX
	(void)path;
	return 1; // D:\ is always writable on Xbox
#elif defined(PLATFORM_WIN32)
	// on windows access() on directories will only check if the directory exists, so
	char tmp[FS_MAXPATH + 1] = { 0 };
	snprintf(tmp, sizeof(tmp), "%s/.tmp", path);
	FILE *f = fopen(tmp, "wb");
	if (f) {
		fclose(f);
		remove(tmp);
		return 1;
	}
	return 0;
#else
	return (access(path, W_OK) == 0);
#endif
}

s32 fsPathIsAbsolute(const char *path)
{
 return (path[0] == '/' || (isalpha(path[0]) && path[1] == ':'));
}

s32 fsPathIsCwdRelative(const char *path)
{
	// ., .., ./, ../
	return (path[0] == '.' && (path[1] == '.' || path[1] == '/' || path[1] == '\\' || path[1] == '\0'));
}

#ifdef PLATFORM_XBOX
// nxdk's file API (CreateFile/fopen) resolves DOS paths with backslash
// separators only; forward slashes fail to open.  Normalise in place.
static const char *fsXboxNormalize(char *p)
{
	for (char *c = p; *c; ++c) {
		if (*c == '/') {
			*c = '\\';
		}
	}
	return p;
}
#endif

const char *fsFullPath(const char *relPath)
{
	static char pathBuf[FS_MAXPATH + 1];

	if (relPath[0] == '$') {
		// expandable placeholder $X; will be replaced with the corresponding path, if any
		const char *expStr = NULL;
		switch (relPath[1]) {
			case 'E': expStr = exeDir; break;
			case 'H': expStr = homeDir; break;
			case 'M': expStr = modDir; break;
			case 'B': expStr = baseDir; break;
			case 'S': expStr = saveDir; break;
			default: break;
		}
		if (expStr) {
			const u32 len = strlen(expStr);
			if (len > 0) {
				memcpy(pathBuf, expStr, len);
				strncpy(pathBuf + len, relPath + 2, FS_MAXPATH - len);
#ifdef PLATFORM_XBOX
				fsXboxNormalize(pathBuf);
#endif
				return pathBuf;
			}
		}
		// couldn't expand anything, return as is
		return relPath;
	} else if (!baseDir[0] || fsPathIsAbsolute(relPath) || fsPathIsCwdRelative(relPath)) {
		// user explicitly wants working directory or this is an absolute path or we have no baseDir set up yet
		return relPath;
	}

	// path relative to mod or base dir; this will be a read request, so check where the file actually is
	if (modDir[0]) {
		snprintf(pathBuf, FS_MAXPATH, "%s/%s", modDir, relPath);
#ifdef PLATFORM_XBOX
		fsXboxNormalize(pathBuf);
#endif
		if (fsFileSize(pathBuf) >= 0) {
			return pathBuf;
		}
	}
	// fall back to basedir
	snprintf(pathBuf, FS_MAXPATH, "%s/%s", baseDir, relPath);
#ifdef PLATFORM_XBOX
	fsXboxNormalize(pathBuf);
#endif
	return pathBuf;
}

#ifdef PLATFORM_XBOX
static void xboxMakeDirs(const char *abs);
#endif

s32 fsInit(void)
{
#ifdef PLATFORM_XBOX
	// On Xbox everything lives on D:\ (the game disc).
	// Mount D: explicitly: libnxdk_automount_d wires this via a .CRT$XIT
	// section that lld-link can dead-strip, so D: may otherwise be unmounted.
	if (!nxIsDriveMounted('D')) {
		char ntPath[MAX_PATH];
		nxGetCurrentXbeNtPath(ntPath);
		char *sep = strrchr(ntPath, '\\');
		if (sep) {
			*(sep + 1) = '\0'; // keep the XBE's directory (with trailing '\')
		}
		sysLogPrintf(LOG_NOTE, "mounting D: -> %s", ntPath);
		if (!nxMountDrive('D', ntPath)) {
			sysLogPrintf(LOG_ERROR, "FAILED to mount D: (%s)", ntPath);
		}
	} else {
		sysLogPrintf(LOG_NOTE, "D: already mounted");
	}

	strncpy(exeDir,  "D:",                      FS_MAXPATH);
	strncpy(homeDir, "D:",                      FS_MAXPATH);
	strncpy(baseDir, "D:",                      FS_MAXPATH);
	// Saves live on E: (HDD partition 1). Nothing mounts it for us -- only D:
	// is handled above -- so the save dir was unreachable and every EEPROM
	// read/write failed with ENOENT.
	if (!nxIsDriveMounted('E')) {
		sysLogPrintf(LOG_NOTE, "mounting E: -> %s", "\\Device\\Harddisk0\\Partition1\\");
		if (!nxMountDrive('E', "\\Device\\Harddisk0\\Partition1\\")) {
			sysLogPrintf(LOG_ERROR, "FAILED to mount E:");
		}
	}

	strncpy(saveDir, "E:\\TDATA\\PerfectDarkX", FS_MAXPATH);
	xboxMakeDirs(saveDir);
	sysLogPrintf(LOG_NOTE, "base dir: %s", baseDir);
	sysLogPrintf(LOG_NOTE, "save dir: %s", saveDir);
	return 0;
#else
	sysGetExecutablePath(exeDir, FS_MAXPATH);

	// if this is set, default to exe path for everything
	const s32 portable = sysArgCheck("--portable");
	if (portable) {
		strcpy(homeDir, exeDir);
	} else {
		sysGetHomePath(homeDir, FS_MAXPATH);
	}

	// get path to base dir and expand it if needed
	const char *path = sysArgGetString("--basedir");
	if (!path) {
		// check if there's a `data` directory in working directory or homeDir, otherwise default to exe directory
		path = "$E/" DEFAULT_BASEDIR_NAME;
		if (!portable) {
			if (fsFileSize("./" DEFAULT_BASEDIR_NAME) >= 0) {
				path = "./" DEFAULT_BASEDIR_NAME;
			} else if (fsFileSize("$H/" DEFAULT_BASEDIR_NAME) >= 0) {
				path = "$H/" DEFAULT_BASEDIR_NAME;
			}
		}
	}
	strncpy(baseDir, fsFullPath(path), FS_MAXPATH);

	// get path to mod dir and expand it if needed
	// mod directory is overlaid on top of base directory
	path = sysArgGetString("--moddir");
	if (path) {
		if (fsPathIsAbsolute(path) || fsPathIsCwdRelative(path) || path[0] == '$') {
			// path is explicit; check as-is
			if (fsFileSize(path) >= 0) {
				strncpy(modDir, fsFullPath(path), FS_MAXPATH);
			}
		} else {
			// path is relative to workdir; try to find it
			const char *priority[] = { ".", "$E", "$H" };
			for (s32 i = 0; i < 2 + (portable != 0); ++i) {
				char *tmp = strFmt("%s/%s", priority[i], path);
				if (fsFileSize(tmp) >= 0) {
					strncpy(modDir, fsFullPath(tmp), FS_MAXPATH);
					break;
				}
			}
		}
		if (!modDir[0]) {
			sysLogPrintf(LOG_WARNING, "could not find specified moddir `%s`", path);
		}
	}

	// get path to save dir and expand it if needed
	path = sysArgGetString("--savedir");
	if (!path) {
		if (portable) {
			path = "$E";
		} else {
#if defined(PLATFORM_LINUX) || defined(PLATFORM_OSX)
			// check if there's a config in the working directory, otherwise default to homeDir
			if (fsFileSize("./" CONFIG_FNAME) >= 0) {
				path = ".";
			} else {
				path = "$H";
			}
#else
			// check if working directory is writable, otherwise default to homeDir
			if (fsPathIsWritable("./")) {
				path = ".";
			} else {
				sysLogPrintf(LOG_WARNING, "cannot write to working directory, will use %s for saves instead", homeDir);
				path = "$H";
			}
#endif
		}
	}

	strncpy(saveDir, fsFullPath(path), FS_MAXPATH);

	if (modDir[0]) {
		sysLogPrintf(LOG_NOTE, " mod dir: %s", modDir);
	}
	sysLogPrintf(LOG_NOTE, "base dir: %s", baseDir);
	sysLogPrintf(LOG_NOTE, "save dir: %s", saveDir);

	return 0;
#endif // !PLATFORM_XBOX
}

const char *fsGetModDir(void)
{
	return modDir[0] ? modDir : NULL;
}

s32 fsFileLoadTo(const char *name, void *dst, u32 dstSize)
{
	const char *fullName = fsFullPath(name);

	FILE *f = fopen(fullName, "rb");
	if (!f) {
		return -1;
	}

	fseek(f, 0, SEEK_END);
	const s32 size = ftell(f);
	fseek(f, 0, SEEK_SET);

	if (size < 0) {
		sysLogPrintf(LOG_ERROR, "fsFileLoadTo: empty file or invalid size (%d): %s", size, fullName);
		fclose(f);
		return -1;
	}

	if ((u32)size > dstSize) {
		sysLogPrintf(LOG_ERROR, "fsFileLoadTo: file too big for buffer (%u > %u): %s", size, dstSize, fullName);
		fclose(f);
		return -1;
	}

	fread(dst, 1, size, f);
	fclose(f);

	return size;
}

void *fsFileLoad(const char *name, u32 *outSize)
{
	const char *fullName = fsFullPath(name);

	FILE *f = fopen(fullName, "rb");
	if (!f) {
		sysLogPrintf(LOG_ERROR, "fsFileLoad: could not find file: %s", fullName);
		return NULL;
	}

	fseek(f, 0, SEEK_END);
	const s32 size = ftell(f);
	fseek(f, 0, SEEK_SET);

	if (size < 0) {
		sysLogPrintf(LOG_ERROR, "fsFileLoad: empty file or invalid size (%d): %s", size, fullName);
		fclose(f);
		return NULL;
	}

	void *buf = NULL;
	if (size) {
		buf = sysMemZeroAlloc(size + 1); // sick hack for a free null terminator
		if (!buf) {
			sysLogPrintf(LOG_ERROR, "fsFileLoad: could not alloc %d bytes for file: %s", size, fullName);
			fclose(f);
			return NULL;
		}
		fread(buf, 1, size, f);
	}

	fclose(f);

	if (outSize) {
		*outSize = size;
	}

	return buf;
}

s32 fsFileSize(const char *name)
{
	const char *fullName = fsFullPath(name);
#ifdef PLATFORM_XBOX
	FILE *f = fopen(fullName, "rb");
	if (!f) return -1;
	fseek(f, 0, SEEK_END);
	const s32 size = ftell(f);
	fclose(f);
	return size;
#else
	struct stat st;
	if (stat(fullName, &st) < 0) {
		return -1;
	} else {
		return st.st_size;
	}
#endif
}

FILE *fsFileOpenWrite(const char *name)
{
	return fopen(fsFullPath(name), "wb");
}

FILE *fsFileOpenRead(const char *name)
{
	return fopen(fsFullPath(name), "rb");
}

void fsFileFree(FILE *f)
{
	fclose(f);
}

#ifdef PLATFORM_XBOX
// E:\\ is writable but its directories do not exist on a fresh console, so
// create every component. Without this the save dir is missing and every
// EEPROM read/write fails with ENOENT.
static void xboxMakeDirs(const char *abs)
{
	char tmp[FS_MAXPATH + 1];
	strncpy(tmp, abs, FS_MAXPATH);
	tmp[FS_MAXPATH] = '\0';

	for (char *p = tmp; *p; ++p) {
		if ((*p == '\\' || *p == '/') && p != tmp && *(p - 1) != ':') {
			const char sep = *p;
			*p = '\0';
			CreateDirectoryA(tmp, NULL);
			*p = sep;
		}
	}

	CreateDirectoryA(tmp, NULL);
}
#endif

s32 fsCreateDir(const char *path)
{
#ifdef PLATFORM_XBOX
	// D: is the read-only disc, but saves live on E: and must be created.
	const char *full = (strchr(path, ':') != NULL) ? path : fsFullPath(path);
	if (CreateDirectoryA(full, NULL)) {
		return 0;
	}
	return (GetLastError() == ERROR_ALREADY_EXISTS) ? 0 : -1;
#elif defined(PLATFORM_WIN32)
	return _mkdir(fsFullPath(path));
#else
	return mkdir(fsFullPath(path), 0777);
#endif
}
