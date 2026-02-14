cmake_minimum_required(VERSION 3.22)
project(fcitx5-voice VERSION 0.1.0)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

# Find fcitx5
find_package(Fcitx5Core 5.1.0 REQUIRED)

# Include fcitx5 compiler settings
include("${FCITX_INSTALL_CMAKECONFIG_DIR}/Fcitx5Utils/Fcitx5CompilerSettings.cmake")

# Add plugin subdirectory
add_subdirectory(plugin)
