#include "voice_engine.h"
#include <fcitx-utils/event.h>
#include <fcitx-utils/log.h>
#include <fcitx/inputcontext.h>
#include <fcitx/inputpanel.h>
#include <fcitx/text.h>

namespace fcitx {

VoiceEngine::VoiceEngine(Instance* instance)
    : instance_(instance), dbus_client_(std::make_unique<DBusClient>()) {

    // Set up D-Bus callbacks
    dbus_client_->setTranscriptionCallback(
        [this](const std::string& text, int segment_num) {
            onTranscriptionComplete(text, segment_num);
        });

    dbus_client_->setProcessingStartedCallback(
        [this](int segment_num) {
            processing_count_++;
            updateStatus();
        });

    dbus_client_->setErrorCallback(
        [this](const std::string& message) {
            onError(message);
        });

    // Set up IO event for D-Bus file descriptor
    int dbus_fd = dbus_client_->getFileDescriptor();
    if (dbus_fd >= 0) {
        event_source_ = instance_->eventLoop().addIOEvent(
            dbus_fd,
            IOEventFlag::In,
            [this](EventSource*, int, IOEventFlags) {
                dbus_client_->processEvents();
                return true;
            });
    } else {
        FCITX_ERROR() << "Failed to get D-Bus file descriptor, falling back to timer";
        // Fallback to timer-based polling
        event_source_ = instance_->eventLoop().addTimeEvent(
            CLOCK_MONOTONIC, now(CLOCK_MONOTONIC), 100000,
            [this](EventSourceTime*, uint64_t) {
                dbus_client_->processEvents();
                return true;
            });
    }
}

VoiceEngine::~VoiceEngine() = default;

void VoiceEngine::activate(const InputMethodEntry& entry,
                          InputContextEvent& event) {
}

void VoiceEngine::deactivate(const InputMethodEntry& entry,
                            InputContextEvent& event) {
    if (recording_) {
        stopRecording();
    }
}

void VoiceEngine::keyEvent(const InputMethodEntry& entry, KeyEvent& event) {
    // Check for Shift+Space hotkey
    if (event.key().check(FcitxKey_space, KeyState::Shift) &&
        !event.isRelease()) {
        toggleRecording();
        event.filterAndAccept();
        return;
    }
}

void VoiceEngine::reset(const InputMethodEntry& entry,
                       InputContextEvent& event) {
}

void VoiceEngine::startRecording() {
    if (recording_) {
        FCITX_WARN() << "Already recording";
        return;
    }

    try {
        dbus_client_->startRecording();
        recording_ = true;
        // Don't clear processing_count_ - previous transcriptions may still be in flight
        updateStatus();
    } catch (const std::exception& e) {
        FCITX_ERROR() << "Failed to start recording: " << e.what();
        showNotification("❌ 録音開始失敗");
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
        // Don't manually set processing - ProcessingStarted signals will increment the counter
        updateStatus();
    } catch (const std::exception& e) {
        FCITX_ERROR() << "Failed to stop recording: " << e.what();
        recording_ = false;
        processing_count_ = 0;
        clearNotification();
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
    // Decrement processing counter
    if (processing_count_ > 0) {
        processing_count_--;
    }

    // Don't insert empty text
    if (text.empty()) {
        updateStatus();
        return;
    }

    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        FCITX_WARN() << "No active input context";
        updateStatus();
        return;
    }

    // Insert transcribed text immediately
    ic->commitString(text);
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);

    updateStatus();
}

void VoiceEngine::onError(const std::string& message) {
    FCITX_ERROR() << "Daemon error: " << message;
    showNotification("❌ " + message);
    recording_ = false;
    processing_count_ = 0;  // Clear processing count on error
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

void VoiceEngine::updateStatus() {
    std::string status;
    bool processing = (processing_count_ > 0);

    if (recording_ && processing) {
        status = "🎤 録音中 | ⏳ 処理中";
    } else if (recording_) {
        status = "🎤 録音中 (Shift+Space で停止)";
    } else if (processing) {
        status = "⏳ 処理中...";
    } else {
        // Idle - clear notification
        clearNotification();
        return;
    }

    showNotification(status);
}

} // namespace fcitx
