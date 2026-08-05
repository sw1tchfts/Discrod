cmake_minimum_required(VERSION 3.20)
project(DiscrodApo CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

# The signal core (dsp + apo_core + bridge protocol) is platform-independent
# and is what the parity harness links against; it must build on Linux/gcc as
# well as MSVC.
add_library(discrod_apo_core STATIC
    src/dsp.cpp
    src/apo_core.cpp)
target_include_directories(discrod_apo_core PUBLIC src)

if(WIN32)
    # The full APO DLL only builds with the Windows SDK headers
    # (audioenginebaseapo.h etc.).  ThreadingModel/exports come from the .def.
    add_library(DiscrodApo SHARED
        src/discrod_apo.cpp
        src/dllmain.cpp
        src/discrod_apo.def)
    # ole32: CoTaskMemAlloc; shell32: SHGetFolderPathW; advapi32: Reg* APIs.
    target_link_libraries(DiscrodApo PRIVATE discrod_apo_core ole32 shell32 advapi32)
    set_target_properties(DiscrodApo PROPERTIES OUTPUT_NAME "DiscrodApo")
endif()

# Parity / RT harness: checks the shared-memory ring/seqlock end to end and the
# DSP behaviour of the C++ core.
enable_testing()
add_executable(apo_tests tests/apo_tests.cpp)
target_link_libraries(apo_tests PRIVATE discrod_apo_core)
add_test(NAME apo_tests COMMAND apo_tests)

# Oracle CLI used by app/tests/test_apo_bridge.py for cross-language DSP parity.
add_executable(dsp_oracle tools/dsp_oracle.cpp)
target_link_libraries(dsp_oracle PRIVATE discrod_apo_core)

# End-to-end probe: attaches ApoCore to a real bridge file (used by the Python
# round-trip test to verify writer/reader agreement through a memory mapping).
add_executable(bridge_probe tools/bridge_probe.cpp)
target_link_libraries(bridge_probe PRIVATE discrod_apo_core)
