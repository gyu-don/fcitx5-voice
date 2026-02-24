#pragma once

#include <fcitx/addonfactory.h>
#include <fcitx/addonmanager.h>
#include <fcitx/inputmethodengine.h>
#include <fcitx/instance.h>
#include <fcitx-utils/event.h>
#include <memory>
#include "dbus_client.h"

namespace fcitx {

class VoiceEngine final : public InputMethodEngineV2 {
public:
    VoiceEngine(Instance* instance);
    ~VoiceEngine() override;

    // InputMethodEngine interface
    void activate(const InputMethodEntry& entry,
                 InputContextEvent& event) override;
    void deactivate(const InputMethodEntry& entry,
                   InputContextEvent& event) override;
    void keyEvent(const InputMethodEntry& entry, KeyEvent& event) override;
    void reset(const InputMethodEntry& entry,
              InputContextEvent& event) override;

    // Instance access
    Instance* instance() { return instance_; }

private:
    void startRecording();
    void stopRecording();
    void toggleRecording();
    void onTranscriptionComplete(const std::string& text, int segment_num);
    void onTranscriptionDelta(const std::string& text);
    void onError(const std::string& message);
    void showNotification(const std::string& message);
    void clearNotification();
    void setPreedit(const std::string& text);
    void clearPreedit();
    void updateStatus();  // Update status based on recording_ and processing_ flags

    Instance* instance_;
    std::unique_ptr<DBusClient> dbus_client_;
    std::unique_ptr<EventSource> event_source_;
    bool recording_ = false;
    int processing_count_ = 0;  // Number of segments currently being processed
    std::string preedit_text_;  // Accumulated delta text shown as preedit
};

class VoiceEngineFactory : public AddonFactory {
public:
    AddonInstance* create(AddonManager* manager) override {
        return new VoiceEngine(manager->instance());
    }
};

} // namespace fcitx
