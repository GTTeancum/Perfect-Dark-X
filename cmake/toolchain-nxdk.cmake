# NXDK toolchain for Original Xbox
# Usage: cmake -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-nxdk.cmake ..
#
# Requires NXDK to be installed and $NXDK_DIR set (or passed via -DNXDK_DIR=...).
# NXDK ships Clang/LLVM targeting i686-pc-windows-msvc (Xbox ABI).

cmake_minimum_required(VERSION 3.16)

# ── Locate NXDK ──────────────────────────────────────────────────────────────

if(NOT DEFINED NXDK_DIR)
  if(DEFINED ENV{NXDK_DIR})
    set(NXDK_DIR "$ENV{NXDK_DIR}")
  else()
    message(FATAL_ERROR
      "NXDK_DIR is not set. "
      "Set it to the root of your nxdk checkout, e.g.:\n"
      "  cmake -DNXDK_DIR=/path/to/nxdk -DCMAKE_TOOLCHAIN_FILE=cmake/toolchain-nxdk.cmake ..")
  endif()
endif()

# Resolve to absolute path
get_filename_component(NXDK_DIR "${NXDK_DIR}" ABSOLUTE)

if(NOT EXISTS "${NXDK_DIR}/CMakeLists.txt" AND NOT EXISTS "${NXDK_DIR}/lib/pbkit/pbkit.c")
  message(FATAL_ERROR "NXDK_DIR does not appear to point at a valid NXDK checkout: ${NXDK_DIR}")
endif()

message(STATUS "NXDK: ${NXDK_DIR}")

# ── Target system ────────────────────────────────────────────────────────────

set(CMAKE_SYSTEM_NAME Generic)
set(CMAKE_SYSTEM_PROCESSOR i686)

# Tell our CMakeLists.txt which platform we're on
set(XBOX TRUE)

# ── Compiler ─────────────────────────────────────────────────────────────────

# NXDK bundles a pre-built LLVM toolchain
set(NXDK_LLVM "${NXDK_DIR}/tools/llvm/bin")

if(WIN32)
  set(EXE ".exe")
else()
  set(EXE "")
endif()

set(CMAKE_C_COMPILER   "${NXDK_LLVM}/clang${EXE}"   CACHE FILEPATH "C compiler")
set(CMAKE_CXX_COMPILER "${NXDK_LLVM}/clang++${EXE}" CACHE FILEPATH "C++ compiler")
set(CMAKE_AR           "${NXDK_LLVM}/llvm-ar${EXE}"  CACHE FILEPATH "Archiver")
set(CMAKE_RANLIB       "${NXDK_LLVM}/llvm-ranlib${EXE}" CACHE FILEPATH "Ranlib")
set(CMAKE_LINKER       "${NXDK_LLVM}/ld.lld${EXE}"   CACHE FILEPATH "Linker")

# Target triple for original Xbox (Pentium III, 32-bit Windows-like ABI)
set(XBOX_TARGET_TRIPLE "i686-pc-windows-msvc")

set(CMAKE_C_COMPILER_TARGET   "${XBOX_TARGET_TRIPLE}")
set(CMAKE_CXX_COMPILER_TARGET "${XBOX_TARGET_TRIPLE}")

# ── Sysroot / include paths ───────────────────────────────────────────────────

set(NXDK_INC "${NXDK_DIR}/include")
set(NXDK_LIB "${NXDK_DIR}/lib")

# Add NXDK's CRT and SDK headers
include_directories(SYSTEM
  "${NXDK_INC}"
  "${NXDK_INC}/SDL2"
  "${NXDK_LIB}/pbkit"
  "${NXDK_LIB}/xboxrt/libc_extensions"
)

# ── Compiler flags ────────────────────────────────────────────────────────────

# Xbox-specific defines injected by NXDK
set(XBOX_C_FLAGS
  "-D_XBOX=1"
  "-DXBOX=1"
  "-D_WIN32=1"   # some SDK headers test this
  "-march=pentium3"
  "-msse"
  "-mfpmath=sse"
  "-ffreestanding"
  "--target=${XBOX_TARGET_TRIPLE}"
)

string(JOIN " " XBOX_C_FLAGS_STR ${XBOX_C_FLAGS})

set(CMAKE_C_FLAGS_INIT   "${XBOX_C_FLAGS_STR}")
set(CMAKE_CXX_FLAGS_INIT "${XBOX_C_FLAGS_STR} -fno-rtti -fno-exceptions")

# ── Linker flags ──────────────────────────────────────────────────────────────

# NXDK links against its own CRT and produces a .exe that cxbe converts to .xbe
set(NXDK_LINK_FLAGS
  "-fuse-ld=lld"
  "--target=${XBOX_TARGET_TRIPLE}"
  "-Wl,/subsystem:xbox"
  "-Wl,/entry:XboxStartup"        # NXDK CRT entry point
  "-L${NXDK_LIB}"
  "-L${NXDK_LIB}/hal"
  "-L${NXDK_LIB}/pbkit"
  "-L${NXDK_LIB}/sdl/build/.libs" # SDL2 for Xbox
  "-L${NXDK_LIB}/usb"
  "-L${NXDK_LIB}/xboxrt"
  "-L${NXDK_LIB}/xboxrt/libc_extensions"
)

string(JOIN " " NXDK_LINK_FLAGS_STR ${NXDK_LINK_FLAGS})
set(CMAKE_EXE_LINKER_FLAGS_INIT "${NXDK_LINK_FLAGS_STR}")

# Don't try to run test executables on the host — we're cross-compiling
set(CMAKE_TRY_COMPILE_TARGET_TYPE STATIC_LIBRARY)

# ── Library variables used by CMakeLists.txt ──────────────────────────────────

# These mimic the SDL2/GL/extra lib variables the main build expects
set(SDL2_INCLUDE_DIR  "${NXDK_INC}/SDL2")
set(SDL2_LIBRARY      "SDL2")   # linked from NXDK's SDL2 build
set(ZLIB_INCLUDE_DIR  "${NXDK_INC}")
set(ZLIB_LIBRARY      "z")
set(GL_LIBRARY        "")       # no external GL — we use pbkit directly

# Extra libs NXDK always needs
set(EXTRA_LIBRARIES
  pbkit
  hal
  nxdk
  nxdk_cxx
  xboxrt
  SDL2
  usb
  z
)

# ── cxbe / xbe tooling ────────────────────────────────────────────────────────

find_program(CXBE_EXECUTABLE cxbe
  HINTS "${NXDK_DIR}/tools/cxbe"
  DOC "cxbe: converts PE .exe to Xbox .xbe"
)

find_program(EXTRACT_XISO_EXECUTABLE extract-xiso
  HINTS "${NXDK_DIR}/tools/extract-xiso/build"
  DOC "extract-xiso: create XISO images"
)
