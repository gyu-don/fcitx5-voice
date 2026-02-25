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

    dbus_client_->setTranscriptionDeltaCallback(
        [this](const std::string& text) {
            onTranscriptionDelta(text);
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
    updateStatus();
}

void VoiceEngine::deactivate(const InputMethodEntry& entry,
                            InputContextEvent& event) {
    if (recording_) {
        stopRecording();
    }
    preedit_text_.clear();
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
    preedit_text_.clear();
}

void VoiceEngine::startRecording() {
    if (recording_) {
        FCITX_WARN() << "Already recording";
        return;
    }

    try {
        dbus_client_->startRecording();
        recording_ = true;
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

    // Commit any pending preedit text immediately
    if (!preedit_text_.empty()) {
        auto* ic = instance_->mostRecentInputContext();
        if (ic) {
            ic->commitString(preedit_text_);
        }
        preedit_text_.clear();
        clearPreedit();
    }

    try {
        dbus_client_->stopRecording();
    } catch (const std::exception& e) {
        FCITX_ERROR() << "Failed to stop recording: " << e.what();
    }
    recording_ = false;
    updateStatus();
}

void VoiceEngine::toggleRecording() {
    if (recording_) {
        stopRecording();
    } else {
        startRecording();
    }
}

void VoiceEngine::onTranscriptionDelta(const std::string& text) {
    if (text.empty()) {
        return;
    }

    // Replace preedit with latest delta (server resends full partial text each time)
    preedit_text_ = text;
    setPreedit(preedit_text_);
}

void VoiceEngine::onTranscriptionComplete(const std::string& text,
                                         int segment_num) {
    // Clear preedit (delta text is replaced by final text)
    preedit_text_.clear();
    clearPreedit();

    // Don't insert empty text
    if (text.empty()) {
        return;
    }

    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        FCITX_WARN() << "No active input context";
        return;
    }

    // Insert final transcribed text
    ic->commitString(text);
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);
}

void VoiceEngine::onError(const std::string& message) {
    FCITX_ERROR() << "Daemon error: " << message;
    recording_ = false;
    showTimedNotification("❌ " + message, 5000);
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

void VoiceEngine::setPreedit(const std::string& text) {
    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        return;
    }

    Text preedit;
    preedit.append(text);
    preedit.setCursor(text.length());
    ic->inputPanel().setClientPreedit(preedit);
    ic->updatePreedit();
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);
}

void VoiceEngine::clearPreedit() {
    auto* ic = instance_->mostRecentInputContext();
    if (!ic) {
        return;
    }

    ic->inputPanel().setClientPreedit(Text());
    ic->updatePreedit();
    ic->updateUserInterface(UserInterfaceComponent::InputPanel);
}

void VoiceEngine::updateStatus() {
    if (recording_) {
        notification_timer_.reset();
        showNotification("🎤 録音中 (Shift+Space で停止)");
    } else {
        showTimedNotification("🎤 停止中 (Shift+Space で開始)", 3000);
    }
}

void VoiceEngine::showTimedNotification(const std::string& message,
                                        uint64_t duration_ms) {
    showNotification(message);
    notification_timer_ = instance_->eventLoop().addTimeEvent(
        CLOCK_MONOTONIC,
        now(CLOCK_MONOTONIC) + duration_ms * 1000,
        0,
        [this](EventSourceTime*, uint64_t) {
            clearNotification();
            return true;
        });
}

} // namespace fcitx
