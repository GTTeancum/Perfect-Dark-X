# NXDK toolchain for Original Xbox
# Usage: cmake -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-nxdk.cmake ..
#
# Requires NXDK to be installed and $NXDK_DIR set (or passed via -DNXDK_DIR=..).
#
# The NXDK Docker image (ghcr.io/xboxdev/nxdk) ships compiler wrappers in
# ${NXDK_DIR}/bin/ (nxdk-cc, nxdk-cxx, nxdk-lib) that configure clang with
# the correct Xbox target triple, march, and flags automatically.
# For local builds with a source checkout, we fall back to the bundled LLVM.

cmake_minimum_required(VERSION 3.16)

# ── Locate NXDK ──────────────────────────────────────────────────────────────

if(NOT DEFINED NXDK_DIR)
  if(DEFINED ENV{NXDK_DIR})
    set(NXDK_DIR "$ENV{NXDK_DIR}")
  else()
    message(FATAL_ERROR
      "NXDK_DIR is not set.\n"
      "Set it to the root of your NXDK install, e.g.:\n"
      "  cmake -DNXDK_DIR=/usr/src/nxdk -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-nxdk.cmake ..")
  endif()
endif()

get_filename_component(NXDK_DIR "${NXDK_DIR}" ABSOLUTE)

# Accept either a source checkout (has pbkit.c) or a pre-built install (has pbkit.h/libpbkit.lib)
if(NOT EXISTS "${NXDK_DIR}/lib/pbkit/pbkit.c" AND
   NOT EXISTS "${NXDK_DIR}/lib/pbkit/pbkit.h" AND
   NOT EXISTS "${NXDK_DIR}/lib/libpbkit.lib")
  message(FATAL_ERROR
    "NXDK_DIR does not look like a valid NXDK install: ${NXDK_DIR}\n"
    "Expected one of:\n"
    "  ${NXDK_DIR}/lib/pbkit/pbkit.h   (pre-built Docker image)\n"
    "  ${NXDK_DIR}/lib/pbkit/pbkit.c   (source checkout)")
endif()

message(STATUS "NXDK: ${NXDK_DIR}")

# ── Target system ────────────────────────────────────────────────────────────

set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR i686)
set(XBOX TRUE)

# ── Compiler ─────────────────────────────────────────────────────────────────
#
# Preference order:
#   1. ${NXDK_DIR}/bin/nxdk-cc  — wrapper script in pre-built Docker image;
#      handles target triple, march, NXDK defines, include paths automatically
#   2. ${NXDK_DIR}/tools/llvm/bin/clang  — bundled LLVM in source checkout
#   3. System clang  — last resort

set(_NXDK_CC  "")
set(_NXDK_CXX "")

if(WIN32)
  set(EXE ".exe")
else()
  set(EXE "")
endif()

if(EXISTS "${NXDK_DIR}/bin/nxdk-cc")
  # Pre-built Docker image — use NXDK wrapper scripts for compilation
  set(_NXDK_CC  "${NXDK_DIR}/bin/nxdk-cc")
  set(_NXDK_CXX "${NXDK_DIR}/bin/nxdk-cxx")
  message(STATUS "NXDK compiler: using nxdk-cc/nxdk-cxx wrappers")
elseif(EXISTS "${NXDK_DIR}/tools/llvm/bin/clang${EXE}")
  # Source checkout with bundled LLVM
  set(_NXDK_CC  "${NXDK_DIR}/tools/llvm/bin/clang${EXE}")
  set(_NXDK_CXX "${NXDK_DIR}/tools/llvm/bin/clang++${EXE}")
  message(STATUS "NXDK compiler: using bundled LLVM at ${NXDK_DIR}/tools/llvm/bin")
else()
  # Fall back to system clang — must support i686-pc-windows-msvc cross target
  find_program(_NXDK_CC  NAMES clang   REQUIRED)
  find_program(_NXDK_CXX NAMES clang++ REQUIRED)
  message(STATUS "NXDK compiler: using system clang (${_NXDK_CC})")
endif()

# Archiver: always use llvm-ar (GNU ar-compatible).  nxdk-lib is MSVC-style
# and does not accept the 'qc' flags CMake generates for ar.
find_program(_NXDK_AR
  NAMES llvm-ar-20 llvm-ar-18 llvm-ar
  HINTS "${NXDK_DIR}/tools/llvm/bin"
  PATHS /usr/lib/llvm20/bin /usr/lib/llvm18/bin /usr/bin
)
if(NOT _NXDK_AR)
  # Last resort — GNU ar can archive LLVM object files
  find_program(_NXDK_AR NAMES ar REQUIRED)
endif()

find_program(_NXDK_RANLIB
  NAMES llvm-ranlib-20 llvm-ranlib-18 llvm-ranlib ranlib
  HINTS "${NXDK_DIR}/tools/llvm/bin"
  PATHS /usr/lib/llvm20/bin /usr/lib/llvm18/bin /usr/bin
)

set(CMAKE_C_COMPILER   "${_NXDK_CC}"     CACHE FILEPATH "C compiler")
set(CMAKE_CXX_COMPILER "${_NXDK_CXX}"   CACHE FILEPATH "C++ compiler")
set(CMAKE_AR           "${_NXDK_AR}"     CACHE FILEPATH "Archiver")
if(_NXDK_RANLIB)
  set(CMAKE_RANLIB     "${_NXDK_RANLIB}" CACHE FILEPATH "Ranlib")
endif()

# ── Target triple (only needed when using raw clang, not nxdk-cc wrapper) ────
#
# nxdk-cc uses i386-pc-win32 (confirmed from wrapper script); use the same
# triple so our explicit --target flags match what the compiler already outputs.

set(XBOX_TARGET_TRIPLE "i386-pc-win32")

if(NOT EXISTS "${NXDK_DIR}/bin/nxdk-cc")
  # When using raw clang we must set the target explicitly
  set(CMAKE_C_COMPILER_TARGET   "${XBOX_TARGET_TRIPLE}")
  set(CMAKE_CXX_COMPILER_TARGET "${XBOX_TARGET_TRIPLE}")
endif()

# ── Include paths ─────────────────────────────────────────────────────────────
#
# Container (pre-built): headers live under ${NXDK_DIR}/lib/
# Source checkout:       headers also under ${NXDK_DIR}/lib/ (same layout)

set(NXDK_INC_XBOXKRNL "${NXDK_DIR}/lib/xboxkrnl")
set(NXDK_INC_PBKIT    "${NXDK_DIR}/lib/pbkit")
set(NXDK_INC_SDL2     "${NXDK_DIR}/lib/sdl/SDL2/include")
set(NXDK_INC_XBOXRT   "${NXDK_DIR}/lib/xboxrt/libc_extensions")

include_directories(SYSTEM
  "${NXDK_INC_XBOXKRNL}"
  "${NXDK_INC_PBKIT}"
  "${NXDK_INC_SDL2}"
)

# xboxrt extension headers (may not exist in all installs)
if(EXISTS "${NXDK_INC_XBOXRT}")
  include_directories(SYSTEM "${NXDK_INC_XBOXRT}")
endif()

# ── Compiler flags (only applied when using raw clang) ───────────────────────
#
# When nxdk-cc is used these are already baked into the wrapper script.
# We still set them so local-clang builds work, and they are no-ops if the
# wrapper already sets them (compiler deduplicates duplicate flags).

set(XBOX_C_FLAGS_LIST
  "-D_XBOX=1"
  "-DXBOX=1"
  "-D_WIN32=1"
  "-march=pentium3"
  "-msse"
  "-mfpmath=sse"
  "-ffreestanding"
)

if(NOT EXISTS "${NXDK_DIR}/bin/nxdk-cc")
  list(APPEND XBOX_C_FLAGS_LIST "--target=${XBOX_TARGET_TRIPLE}")
endif()

string(JOIN " " XBOX_C_FLAGS_STR ${XBOX_C_FLAGS_LIST})
set(CMAKE_C_FLAGS_INIT   "${XBOX_C_FLAGS_STR}")
set(CMAKE_CXX_FLAGS_INIT "${XBOX_C_FLAGS_STR} -fno-rtti -fno-exceptions")

# ── Linker flags ──────────────────────────────────────────────────────────────
#
# nxdk-cc uses -fuse-ld=nxdk-link internally (confirmed from wrapper script).
# nxdk-link calls:  lld -flavor link -subsystem:windows -fixed -base:0x00010000
#                   -stack:65536 -merge:.edata=.edataxb "$@"
#
# CRITICAL: Do NOT pass -Wl,/subsystem:xbox — nxdk-link already sets
# -subsystem:windows and lld's "link" flavor doesn't know "xbox".
# DO pass /entry:XboxStartup — nxdk-link doesn't set a default entry point.

set(NXDK_LINK_FLAGS_LIST
  "-fuse-ld=nxdk-link"
  "--target=${XBOX_TARGET_TRIPLE}"
  "-Wl,/entry:XboxStartup"
  # Emit an lld-link symbol map next to the .exe (basename.map) so EIP values
  # from the XEMU monitor can be translated to symbols.  Harmless at runtime.
  "-Wl,/MAP"
)

string(JOIN " " NXDK_LINK_FLAGS_STR ${NXDK_LINK_FLAGS_LIST})
set(CMAKE_EXE_LINKER_FLAGS_INIT "${NXDK_LINK_FLAGS_STR}")

# Executables produced by lld targeting MSVC have .exe extension
set(CMAKE_EXECUTABLE_SUFFIX ".exe")

# Don't try to run test executables on the host — we are cross-compiling
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

# ── Library variables ─────────────────────────────────────────────────────────
#
# Libs are named lib{name}.lib in the pre-built image.
# We specify them with full paths to avoid linker ambiguity.

set(_NXDK_LIB "${NXDK_DIR}/lib")

# SDL2 include exported so CMakeLists.txt can pass it to find_package overrides
set(SDL2_INCLUDE_DIR  "${NXDK_INC_SDL2}")
set(SDL2_LIBRARY      "${_NXDK_LIB}/libSDL2.lib")
set(ZLIB_INCLUDE_DIR  "${NXDK_DIR}/lib/zlib")
set(ZLIB_LIBRARY      "${_NXDK_LIB}/libzlib.lib")
set(GL_LIBRARY        "")   # no external GL — direct pbkit/NV2A

# Full-path library list consumed by CMakeLists.txt target_link_libraries
set(EXTRA_LIBRARIES
  "${_NXDK_LIB}/libpbkit.lib"
  "${_NXDK_LIB}/libnxdk_hal.lib"
  "${_NXDK_LIB}/libnxdk.lib"
  "${_NXDK_LIB}/libxboxrt.lib"
  "${_NXDK_LIB}/libSDL2.lib"
  "${_NXDK_LIB}/nxdk_usb.lib"
  "${_NXDK_LIB}/libzlib.lib"
  "${_NXDK_LIB}/libc++.lib"
  "${_NXDK_LIB}/libpdclib.lib"
  "${_NXDK_LIB}/libwinapi.lib"
  "${_NXDK_LIB}/xboxkrnl/libxboxkrnl.lib"
)

# ── cxbe / xbe tooling ────────────────────────────────────────────────────────

find_program(CXBE_EXECUTABLE cxbe
  HINTS "${NXDK_DIR}/tools" "${NXDK_DIR}/tools/cxbe" "${NXDK_DIR}/bin"
  DOC "cxbe: converts PE .exe to Xbox .xbe"
)

find_program(EXTRACT_XISO_EXECUTABLE extract-xiso
  HINTS "${NXDK_DIR}/tools/extract-xiso/build" "${NXDK_DIR}/bin"
  DOC "extract-xiso: create XISO images"
)
