cmake_minimum_required(VERSION 3.16)
list(APPEND CMAKE_MODULE_PATH ${CMAKE_SOURCE_DIR}/cmake)

if(APPLE)
  if(CMAKE_OSX_ARCHITECTURES MATCHES "arm64")
    # ARM Macs start at OS 11.
    set(CMAKE_OSX_DEPLOYMENT_TARGET "11.0" CACHE STRING "Minimum OS X deployment version")
  else()
    # SDL2 requires at least this version for everything to work properly.
    set(CMAKE_OSX_DEPLOYMENT_TARGET "10.9" CACHE STRING "Minimum OS X deployment version")
  endif()
endif()

project(pd C CXX)

include(TargetArch)
include(FindPkgConfig)
include(AssetUtils)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_EXTENSIONS OFF)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_EXTENSIONS OFF)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# On first build set the build type to RelWithDebInfo if it's not already set
if (NOT EXISTS ${CMAKE_BINARY_DIR}/CMakeCache.txt)
  if (NOT CMAKE_BUILD_TYPE)
    set(CMAKE_BUILD_TYPE "RelWithDebInfo" CACHE STRING "" FORCE)
  endif()
endif()

# Options

set(ROMID "ntsc-final" CACHE STRING "Which ROM to build an executable for: ntsc-final, pal-final or jpn-final")
option(FORCE_CROSSCOMPILE "Force cross compiling" NO)
option(PD_XBOX_TRACE_MARKS "Emit high-frequency Xbox crash breadcrumbs to COM1" OFF)
option(PD_XBOX_GPU_SYNC_TRACE
  "Synchronize and log every first-frame NV2A pushbuffer block" OFF)
option(PD_XBOX_RENDER_QUALIFY_EFFECTS
  "Stress stochastic-alpha and character-cloak rendering on Xbox" OFF)
option(PD_XBOX_QUALIFICATION_CONFIG
  "Allow the Xbox build to load qualification-only pdx_boot.ini overrides" OFF)
option(PD_XBOX_IGR_TEST
  "Automatically exercise the Xbox dashboard-reset path after boot" OFF)
set(PD_XBOX_TITLE_ID "0x50440001" CACHE STRING
  "Original Xbox title ID used for the UDATA namespace")
set(PD_XBOX_RENDER_TRACE_FRAME "0" CACHE STRING
    "Emit one frame of aggregate NV2A draw-state diagnostics to COM1")

# Determine target arch

target_architecture(TARGET_ARCH)
if(TARGET_ARCH STREQUAL "i386")
  # for historical reasons
  set(TARGET_ARCH "i686")
endif()

# TODO
set(TARGET_IS_BIG_ENDIAN FALSE)

if((TARGET_ARCH MATCHES "unknown") OR (TARGET_ARCH STREQUAL ""))
  message(FATAL_ERROR "Unknown target arch")
elseif(TARGET_ARCH MATCHES "64")
  set(TARGET_IS_64BIT TRUE)
else()
  set(TARGET_IS_64BIT FALSE)
endif()

message(STATUS "Target arch: ${TARGET_ARCH}")

# Determine target OS and platform identifier ($arch-$os)

if(XBOX)
  # Original Xbox via NXDK
  set(TARGET_OS "xbox")
elseif(WIN32)
  # Windows
  set(TARGET_OS "windows")
elseif(APPLE)
  # OSX
  set(TARGET_OS "osx")
elseif(NINTENDO_SWITCH)
  # Switch
  set(TARGET_OS "nswitch")
elseif(UNIX)
  # Generic *nix; assume Linux
  set(TARGET_OS "linux")
else()
  # ???
  message(FATAL_ERROR "Unknown target OS or compiler")
endif()

set(TARGET_PLATFORM "${TARGET_ARCH}-${TARGET_OS}")

message(STATUS "Target platform: ${TARGET_PLATFORM}")

# Calculate version info and binary name

set(VERSION_IS_PAL 0)

if(ROMID STREQUAL "ntsc-final")
  set(VERSION 2)
  set(BIN_REGION "")
elseif(ROMID STREQUAL "pal-final")
  set(VERSION 4)
  set(BIN_REGION ".pal")
  set(VERSION_IS_PAL 1)
elseif(ROMID STREQUAL "jpn-final")
  set(VERSION 5)
  set(BIN_REGION ".jpn")
else()
  message(FATAL_ERROR "Unknown ROM ID: ${ROMID}")
endif()

# git commit hash
execute_process(
  COMMAND git rev-parse --short HEAD
  WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  OUTPUT_VARIABLE VERSION_HASH
  OUTPUT_STRIP_TRAILING_WHITESPACE
)

# git branch name
execute_process(
  COMMAND git rev-parse --abbrev-ref HEAD
  WORKING_DIRECTORY ${CMAKE_SOURCE_DIR}
  OUTPUT_VARIABLE VERSION_BRANCH
  OUTPUT_STRIP_TRAILING_WHITESPACE
)

set(VERSION_TARGET "${TARGET_PLATFORM}")
set(VERSION_ARCH "${TARGET_ARCH}")
set(VERSION_ROMID "${ROMID}")
set(VERSION_BUILD "${CMAKE_BUILD_TYPE}")

if((VERSION_BRANCH STREQUAL "") OR (VERSION_BRANCH STREQUAL "HEAD"))
  set(VERSION_BRANCH "port-custom")
endif()

message(STATUS "Version: ${VERSION_ROMID}, hash ${VERSION_HASH}, branch ${VERSION_BRANCH}")

configure_file("${CMAKE_SOURCE_DIR}/port/include/versioninfo.h.in" "${CMAKE_BINARY_DIR}/port/include/versioninfo.h" @ONLY)

set(ASSET_DIR "src/assets/${ROMID}")
set(GENERATED_DIR "src/generated/${ROMID}")

set(ENV{ROMID} ${ROMID})

# Build folder postfix

set(BUILD_ID "${VERSION_ROMID}-${VERSION_TARGET}")

# Compiler defs

add_definitions(
  -DMATCHING=0
  -DPIRACYCHECKS=0
  -DROM_SIZE=32
  -DAVOID_UB=1
  -D_LANGUAGE_C=1
  -DPAL=${VERSION_IS_PAL}
  -DVERSION=${VERSION}
)

if(TARGET_IS_64BIT)
  add_definitions(-DPLATFORM_64BIT=1)
endif()

if(XBOX)
  add_definitions(-DPLATFORM_XBOX=1)
  if(PD_XBOX_TRACE_MARKS)
    add_definitions(-DPD_XBOX_TRACE_MARKS=1)
  endif()
  if(PD_XBOX_GPU_SYNC_TRACE)
    add_definitions(-DPD_XBOX_GPU_SYNC_TRACE=1)
  endif()
  if(PD_XBOX_RENDER_QUALIFY_EFFECTS)
    add_definitions(-DPD_XBOX_RENDER_QUALIFY_EFFECTS=1)
  endif()
  if(PD_XBOX_QUALIFICATION_CONFIG)
    add_definitions(-DPD_XBOX_QUALIFICATION_CONFIG=1)
  endif()
  if(PD_XBOX_IGR_TEST)
    add_definitions(-DPD_XBOX_IGR_TEST=1)
  endif()
  if(NOT PD_XBOX_RENDER_TRACE_FRAME STREQUAL "0")
    add_definitions(-DPD_XBOX_RENDER_TRACE_FRAME=${PD_XBOX_RENDER_TRACE_FRAME})
  endif()
  # Xbox is Pentium III (SSE1 only, no SSE2); disable SIMD in minimp3
  add_definitions(-DMINIMP3_NO_SIMD=1)
endif()

# Compiler flags

# Include directories
include_directories("${CMAKE_SOURCE_DIR}/include")
include_directories("${CMAKE_SOURCE_DIR}/include/PR")
include_directories("${CMAKE_SOURCE_DIR}/src/include")
include_directories("${CMAKE_BINARY_DIR}/src/generated/${ROMID}")
include_directories("${CMAKE_SOURCE_DIR}/src/lib/ultra/audio")
include_directories("${CMAKE_SOURCE_DIR}/port/include")
include_directories("${CMAKE_BINARY_DIR}/port/include")

# The console build must fit its simulation, audio and renderer work inside a
# 60 Hz frame on a 733 MHz Pentium III. Keep the conservative desktop setting,
# but build Xbox Release with real optimization and inlining.
if(XBOX)
  if(CMAKE_BUILD_TYPE STREQUAL "Debug")
    add_compile_options(-Og -fno-inline-functions)
  else()
    add_compile_options(-O2)
    # propsTick mutates intrusive prop lists while iterating them.  GCC's O2
    # build is currently unstable on the Xbox target even without external
    # textures; keep this one translation unit conservative while the rest of
    # the release build retains the performance-critical O2 setting.
    set_source_files_properties(src/game/proptick.c PROPERTIES COMPILE_OPTIONS "-O1")
  endif()
elseif(NOT (CMAKE_BUILD_TYPE STREQUAL "Debug"))
  add_compile_options(-Og)
endif()

# Common CFLAGS
add_compile_options(
  -fno-strict-aliasing
  -funsigned-char
  -fwrapv
  -include "versioninfo.h"
)

if(NOT XBOX)
  add_compile_options(-fno-inline-functions)
endif()

# Common warning flags
add_compile_options(
  -Wall
  -Wno-address
  -Wno-int-in-bool-context
  -Wno-misleading-indentation
  -Wno-missing-braces
  -Wno-multichar
  -Wno-tautological-compare
  -Wno-unused-but-set-variable
  -Wno-unused-value
  -Wno-unused-variable
  -fdiagnostics-color=always
)

# C specific flags
add_compile_options($<$<COMPILE_LANGUAGE:C>:-Wno-pointer-sign>)

# C++ specific flags
add_compile_options($<$<COMPILE_LANGUAGE:CXX>:-Wno-unused-function>)

# Platform-specific flags
if (APPLE)
  # Make it look for libs in the executable folder manually since CMake's rpath system is wack
  set(CMAKE_EXE_LINKER_FLAGS "${CMAKE_EXE_LINKER_FLAGS} -rpath @executable_path")
else()
  # This warning is not recognized by XCode/Clang for some reason.
  add_compile_options(-Wno-format-truncation)
endif()

# Dependencies

set(LIBS )

# SDL2
if(XBOX)
  # SDL2_INCLUDE_DIR / SDL2_LIBRARY already set by toolchain-nxdk.cmake
  if(NOT SDL2_INCLUDE_DIR)
    message(FATAL_ERROR "SDL2_INCLUDE_DIR not set by NXDK toolchain")
  endif()
else()
  find_package(SDL2 REQUIRED)
  if(NINTENDO_SWITCH)
    if(NOT SDL2_LIBRARY OR NOT SDL2_INCLUDE_DIR)
      set(SDL2_LIBRARY "SDL2")
      set(SDL2_INCLUDE_DIR "${DEVKITPRO}/portlibs/switch/include/SDL2")
    endif()
  endif()
endif()
include_directories(SYSTEM "${SDL2_INCLUDE_DIR}")
list(APPEND LIBS "${SDL2_LIBRARY}")

# ZLib
if(XBOX)
  # NXDK ships zlib as lib/libzlib.lib; headers live in lib/zlib/
  set(ZLIB_LIBRARY "${NXDK_DIR}/lib/libzlib.lib")
  # zlib.h lives one level deeper: $NXDK_DIR/lib/zlib/zlib/zlib.h
  find_path(NXDK_ZLIB_INCLUDE_DIR zlib.h
    PATHS
      "${NXDK_DIR}/lib/zlib/zlib"
      "${NXDK_DIR}/lib/zlib"
      "${NXDK_DIR}/include"
      "${NXDK_DIR}/lib/zlib/include"
    NO_DEFAULT_PATH)
  if(NXDK_ZLIB_INCLUDE_DIR)
    include_directories(SYSTEM "${NXDK_ZLIB_INCLUDE_DIR}")
    message(STATUS "NXDK zlib.h: ${NXDK_ZLIB_INCLUDE_DIR}")
  else()
    message(WARNING "zlib.h not found in NXDK — rzip_c.c will fail.  "
      "Set NXDK_ZLIB_INCLUDE_DIR manually if the header is elsewhere.")
  endif()
else()
  find_package(ZLIB REQUIRED)
  if(ZLIB_INCLUDE_DIR)
    include_directories(SYSTEM "${ZLIB_INCLUDE_DIR}")
  endif()
endif()
list(APPEND LIBS "${ZLIB_LIBRARY}")

# GL — Xbox uses pbkit directly; no external GL lib needed
if(NOT XBOX)
  if(NOT GL_LIBRARY)
    if(WIN32)
      set(GL_LIBRARY opengl32)
    elseif(NINTENDO_SWITCH)
      set(GL_LIBRARY EGL glapi drm_nouveau)
    elseif(APPLE)
      set(GL_LIBRARY "-framework OpenGL")
    else()
      set(GL_LIBRARY GL)
    endif()
  endif()
  list(APPEND LIBS "${GL_LIBRARY}")
endif()

# Platform-specific extra libs
if(NOT EXTRA_LIBRARIES)
  if(XBOX)
    # EXTRA_LIBRARIES set by toolchain-nxdk.cmake
    set(EXTRA_LIBRARIES "")
  elseif(WIN32)
    set(EXTRA_LIBRARIES dbghelp -static-libstdc++)
  elseif(NINTENDO_SWITCH)
    set(EXTRA_LIBRARIES stdc++ pthread)
  else()
    set(EXTRA_LIBRARIES stdc++ m dl)
  endif()
endif()
list(APPEND LIBS "${EXTRA_LIBRARIES}")

# Sources

file(GLOB_RECURSE SRC_PORT "${CMAKE_SOURCE_DIR}/port/*.c")
file(GLOB_RECURSE SRC_PORT_CXX "${CMAKE_SOURCE_DIR}/port/*.cpp")

# port/src/xbox/ is appended explicitly in the XBOX branch below; stop the
# recursive globs above from pulling it into non-Xbox builds as well.
list(FILTER SRC_PORT     EXCLUDE REGEX ".*/port/src/xbox/.*")
list(FILTER SRC_PORT_CXX EXCLUDE REGEX ".*/port/src/xbox/.*")

if(XBOX)
  # On Xbox we use our own system/crash layer and NV2A/Xbox-WM backends.
  # Exclude the SDL2 window manager, OpenGL renderer, GLAD loader, and the
  # POSIX/Win32 system+crash files that assume a desktop OS.
  list(FILTER SRC_PORT EXCLUDE REGEX ".*/port/fast3d/glad/.*")
  list(FILTER SRC_PORT_CXX EXCLUDE REGEX ".*/port/fast3d/gfx_opengl\\.cpp")
  list(FILTER SRC_PORT_CXX EXCLUDE REGEX ".*/port/fast3d/gfx_sdl2?\\.cpp")
  list(FILTER SRC_PORT EXCLUDE REGEX ".*/port/src/system\\.c")
  list(FILTER SRC_PORT EXCLUDE REGEX ".*/port/src/crash\\.c")
  # main.c is kept but its int main() is guarded with #ifndef PLATFORM_XBOX
  # Xbox-specific sources are in port/src/xbox/
  file(GLOB SRC_PORT_XBOX "${CMAKE_SOURCE_DIR}/port/src/xbox/*.c")
  file(GLOB SRC_PORT_XBOX_CXX "${CMAKE_SOURCE_DIR}/port/src/xbox/*.cpp")
  list(APPEND SRC_PORT     ${SRC_PORT_XBOX})
  list(APPEND SRC_PORT_CXX ${SRC_PORT_XBOX_CXX})
endif()

file(GLOB_RECURSE SRC_GAME "${CMAKE_SOURCE_DIR}/src/game/*.c")

file(GLOB SRC_LIB_MP3 "${CMAKE_SOURCE_DIR}/src/lib/mp3/*.c")
file(GLOB SRC_LIB_NAUDIO "${CMAKE_SOURCE_DIR}/src/lib/naudio/*.c")
file(GLOB SRC_LIB_ULTRA_AUDIO "${CMAKE_SOURCE_DIR}/src/lib/ultra/audio/*.c")
file(GLOB SRC_LIB_GU "${CMAKE_SOURCE_DIR}/src/lib/ultra/gu/*.c")
set(SRC_LIB
  src/lib/ultra/io/vimodentsclan1.c
  src/lib/ultra/io/vimodepallan1.c
  src/lib/ultra/io/vimodempallan1.c
  src/lib/ultra/io/vitbl.c
  src/lib/ailist.c
  src/lib/anim.c
  src/lib/args.c
  src/lib/audiodma.c
  src/lib/audiomgr.c
  src/lib/base.c
  src/lib/collision.c
  src/lib/debughud.c
  src/lib/dma.c
  src/lib/fault.c
  src/lib/joy.c
  src/lib/lib_2f490_c.c
  src/lib/lib_2fc60.c
  src/lib/lib_3e3e0.c
  src/lib/lib_04f60nb.c
  src/lib/lib_17ce0.c
  src/lib/lib_034d0.c
  src/lib/lib_39c80.c
  src/lib/lib_15850.c
  src/lib/mema.c
  src/lib/memp.c
  src/lib/model.c
  src/lib/modelasm_c.c
  src/lib/mp3.c
  src/lib/mtx_c.c
  src/lib/mtx.c
  src/lib/music.c
  src/lib/path.c
  src/lib/profile.c
  src/lib/rdp.c
  src/lib/rmon.c
  src/lib/rng_c.c
  src/lib/rzip_c.c
  src/lib/snd.c
  src/lib/speaker.c
  src/lib/varsinit.c
  src/lib/vi.c
  src/textureconfig.c
)
list(PREPEND SRC_LIB "${CMAKE_SOURCE_DIR}")

set(SRC
  ${SRC_GAME}
  ${SRC_PORT}
  ${SRC_PORT_CXX}
  ${SRC_LIB_MP3}
  ${SRC_LIB_NAUDIO}
  ${SRC_LIB_ULTRA_AUDIO}
  ${SRC_LIB_GU}
  ${SRC_LIB}
)

if(WIN32)
  list(APPEND SRC "${CMAKE_SOURCE_DIR}/dist/windows/icon.rc")
endif()

# Asset files

generate_asset_headers("lang/" "${CMAKE_SOURCE_DIR}/tools/assetmgr/mklang" "en" JSON_HEADERS)
generate_asset_headers("pads/" "${CMAKE_SOURCE_DIR}/tools/assetmgr/mkpads" "" JSON_HEADERS)
generate_asset_headers("tiles/" "${CMAKE_SOURCE_DIR}/tools/assetmgr/mktiles" "" JSON_HEADERS)
generate_asset_headers("animations.json" "${CMAKE_SOURCE_DIR}/tools/assetmgr/mkanims" "" JSON_HEADERS)
generate_asset_headers("sequences.json" "${CMAKE_SOURCE_DIR}/tools/assetmgr/mksequences" "" JSON_HEADERS)
generate_asset_headers("textures.json" "${CMAKE_SOURCE_DIR}/tools/assetmgr/mktextures" "" JSON_HEADERS)

add_custom_target(pd_headers ALL DEPENDS ${JSON_HEADERS})

# Game target

set(BIN_NAME "pd${BIN_REGION}.${TARGET_ARCH}")
add_executable(pd ${SRC})
add_dependencies(pd pd_headers)
target_link_libraries(pd ${LIBS})
set_target_properties(pd PROPERTIES OUTPUT_NAME "${BIN_NAME}")
if(WIN32)
  set_target_properties(pd PROPERTIES WIN32_EXECUTABLE TRUE)
endif()

# Additional commands for specific platforms.

if(NINTENDO_SWITCH)
  add_custom_command(TARGET pd POST_BUILD
    COMMAND nacptool --create "Perfect Dark" "fgsfds and contributors" "${VERSION_ROMID}/${VERSION_BRANCH}/${VERSION_HASH}" "${BIN_NAME}.nacp"
    COMMAND aarch64-none-elf-strip -o ${CMAKE_BINARY_DIR}/${BIN_NAME}.stripped.elf ${CMAKE_BINARY_DIR}/${BIN_NAME}.elf
    COMMAND elf2nro ${CMAKE_BINARY_DIR}/${BIN_NAME}.stripped.elf ${CMAKE_BINARY_DIR}/${BIN_NAME}.nro --icon=${CMAKE_SOURCE_DIR}/dist/nswitch/icon.jpg --nacp=${BIN_NAME}.nacp
  )
endif()

if(XBOX)
  # Convert the PE .exe produced by lld into an Xbox XBE using cxbe.
  # The XBE title ID and title name can be adjusted here.
  if(CXBE_EXECUTABLE)
    # Note: this cxbe (NXDK Docker) only supports -OUT, -TITLE, -MODE, -LOGO,
    # -DEBUGPATH.  The -TITLEID and -STACK flags are not recognised; stack size
    # is controlled by nxdk-link's -stack:65536 flag instead.
    # Use $<TARGET_FILE:pd> (CMake generator expression) for the exact PE path —
    # CMAKE_EXECUTABLE_SUFFIX may or may not append .exe depending on the system.
    add_custom_command(TARGET pd POST_BUILD
      COMMAND ${CXBE_EXECUTABLE}
        -OUT:${CMAKE_BINARY_DIR}/default.xbe
        -TITLE:"Perfect Dark"
        $<TARGET_FILE:pd>
      COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_SOURCE_DIR}/scripts/patch-xbe-metadata.py
        --xbe ${CMAKE_BINARY_DIR}/default.xbe
        --title-id ${PD_XBOX_TITLE_ID}
      COMMAND ${Python3_EXECUTABLE}
        ${CMAKE_SOURCE_DIR}/scripts/insert-xbe-dashboard-assets.py
        --xbe ${CMAKE_BINARY_DIR}/default.xbe
        --title-info ${CMAKE_SOURCE_DIR}/port/xbox_assets/TitleInfo.txt
        --title-image ${CMAKE_SOURCE_DIR}/port/xbox_assets/TitleImage.xbx
        --save-image ${CMAKE_SOURCE_DIR}/port/xbox_assets/SaveImage.xbx
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
        ${CMAKE_SOURCE_DIR}/port/xbox_assets/TitleImage.xbx
        ${CMAKE_BINARY_DIR}/TitleImage.xbx
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
        ${CMAKE_SOURCE_DIR}/port/xbox_assets/SaveImage.xbx
        ${CMAKE_BINARY_DIR}/SaveImage.xbx
      COMMAND ${CMAKE_COMMAND} -E copy_if_different
        ${CMAKE_SOURCE_DIR}/port/xbox_assets/TitleMeta.xbx
        ${CMAKE_BINARY_DIR}/TitleMeta.xbx
      COMMENT "Converting pd executable -> default.xbe"
    )
  else()
    message(WARNING "cxbe not found — skipping XBE conversion. "
      "Set CXBE_EXECUTABLE or add NXDK tools to your PATH.")
  endif()
endif()
