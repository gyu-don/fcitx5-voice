#include "voice_engine.h"
#include <fcitx-utils/event.h>
#include <fcitx-utils/log.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/text.h>

namespace fcitx {

VoiceEngine::VoiceEngine(Instance* instance)
    : instance_(instance), dbus_client_(std::make_unique<DBusClient>()) {

    FCITX_INFO() << "VoiceEngine initialized";

    // Set up D-Bus callbacks
    dbus_client_->setTranscriptionCallback(
        [this](const std::string& text, int segment_num) {
            onTranscriptionComplete(text, segment_num);
        });

    dbus_client_->setErrorCallback(
        [this](const std::string& message) {
            onError(message);
        });

    // Set up periodic event processing for D-Bus
    event_source_ = instance_->eventLoop().addTimeEvent(
        CLOCK_MONOTONIC, now(CLOCK_MONOTONIC), 100000,
        [this](EventSourceTime*, uint64_t) {
            dbus_client_->processEvents();
            return true;
        });
}

VoiceEngine::~VoiceEngine() {
    FCITX_INFO() << "VoiceEngine destroyed";
}

void VoiceEngine::activate(const InputMethodEntry& entry,
                          InputContextEvent& event) {
    FCITX_DEBUG() << "VoiceEngine activated";
}

void VoiceEngine::deactivate(const InputMethodEntry& entry,
                            InputContextEvent& event) {
    FCITX_DEBUG() << "VoiceEngine deactivated";
    if (recording_) {
        stopRecording();
    }
}

void VoiceEngine::keyEvent(const InputMethodEntry& entry, KeyEvent& event) {
    // Check for Ctrl+Alt+V hotkey
    if (event.key().check(FcitxKey_v, KeyState::Ctrl_Alt)) {
        if (event.isRelease()) {
            return;
        }

        FCITX_INFO() << "Hotkey triggered: Ctrl+Alt+V";
        toggleRecording();
        event.filterAndAccept();
        return;
    }
}

void VoiceEngine::reset(const InputMethodEntry& entry,
                       InputContextEvent& event) {
    FCITX_DEBUG() << "VoiceEngine reset";
}

void VoiceEngine::startRecording() {
    if (recording_) {
        FCITX_WARN() << "Already recording";
        return;
    }

    try {
        dbus_client_->startRecording();
        recording_ = true;
        showNotification("🎤 Recording...");
        FCITX_INFO() << "Recording started";
    } catch (const std::exception& e) {
        FCITX_ERROR() << "Failed to start recording: " << e.what();
        showNotification("❌ Failed to start recording");
        recording_ = false;
    }
}

void VoiceEngine::stopRecording() {
    if (!recording_) {
        FCITX_WARN() << "Not recording";
        return;
    }

    try {
        dbus_client_->stopRecording();
        recording_ = false;
        clearNotification();
        FCITX_INFO() << "Recording stopped";
    } catch (const std::exception& e) {
        FCITX_ERROR() << "Failed to stop recording: " << e.what();
        recording_ = false;
    }
}

void VoiceEngine::toggleRecording() {
    if (recording_) {
        stopRecording();
    } else {
        startRecording();
    }
}

void VoiceEngine::onTranscriptionComplete(const std::string& text,
                                         int segment_num) {
    FCITX_INFO() << "Transcription complete: " << text;

    clearNotification();

    // Get the most recent input context
    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        FCITX_WARN() << "No active input context";
        return;
    }

    // Commit the transcribed text
    ic->commitString(text);
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);

    FCITX_INFO() << "Text committed to input context";
}

void VoiceEngine::onError(const std::string& message) {
    FCITX_ERROR() << "Daemon error: " << message;
    showNotification("❌ " + message);
    recording_ = false;
}

void VoiceEngine::showNotification(const std::string& message) {
    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        return;
    }

    Text text;
    text.append(message);
    ic->inputPanel().setAuxUp(text);
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);
}

void VoiceEngine::clearNotification() {
    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        return;
    }

    ic->inputPanel().reset();
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);
}

} // namespace fcitx
