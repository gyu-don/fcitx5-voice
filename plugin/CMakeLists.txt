# Find dependencies
find_package(PkgConfig REQUIRED)
pkg_check_modules(DBUS REQUIRED dbus-1)

# Source files
set(VOICE_SRCS
    voice_engine.cpp
    voice_engine_factory.cpp
    dbus_client.cpp
)

# Build the plugin as a MODULE (shared library without lib prefix)
add_library(voice MODULE ${VOICE_SRCS})

# Include directories
target_include_directories(voice PRIVATE
    ${CMAKE_CURRENT_SOURCE_DIR}
    ${DBUS_INCLUDE_DIRS}
)

# Link libraries
target_link_libraries(voice
    Fcitx5::Core
    Fcitx5::Utils
    ${DBUS_LIBRARIES}
)

# Set library properties
set_target_properties(voice PROPERTIES
    PREFIX ""  # Don't add 'lib' prefix
)

# Installation paths
if(NOT DEFINED CMAKE_INSTALL_LIBDIR)
    set(CMAKE_INSTALL_LIBDIR "${CMAKE_INSTALL_PREFIX}/lib")
endif()

if(NOT DEFINED CMAKE_INSTALL_DATADIR)
    set(CMAKE_INSTALL_DATADIR "${CMAKE_INSTALL_PREFIX}/share")
endif()

# Install the plugin library
install(TARGETS voice
    LIBRARY DESTINATION "${CMAKE_INSTALL_LIBDIR}/fcitx5"
)

# Process and install addon configuration
configure_file(voice-addon.conf.in voice-addon.conf COPYONLY)
install(FILES "${CMAKE_CURRENT_BINARY_DIR}/voice-addon.conf"
    RENAME voice.conf
    DESTINATION "${CMAKE_INSTALL_DATADIR}/fcitx5/addon"
)

# Install input method configuration
install(FILES voice.conf
    DESTINATION "${CMAKE_INSTALL_DATADIR}/fcitx5/inputmethod"
)
