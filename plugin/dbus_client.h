#pragma once

#include <dbus/dbus.h>
#include <functional>
#include <memory>
#include <string>

namespace fcitx {

/**
 * D-Bus client for communicating with fcitx5-voice daemon.
 */
class DBusClient {
public:
    using TranscriptionCallback = std::function<void(const std::string&, int)>;
    using TranscriptionDeltaCallback = std::function<void(const std::string&)>;
    using ErrorCallback = std::function<void(const std::string&)>;

    DBusClient();
    ~DBusClient();

    /**
     * Start audio recording via D-Bus.
     * @throws std::runtime_error if connection fails
     */
    void startRecording();

    /**
     * Stop audio recording via D-Bus.
     * @throws std::runtime_error if connection fails
     */
    void stopRecording();

    /**
     * Get current recording status.
     * @return "recording" or "idle"
     * @throws std::runtime_error if connection fails
     */
    std::string getStatus();

    /**
     * Set callback for transcription completion.
     */
    void setTranscriptionCallback(TranscriptionCallback cb);

    /**
     * Set callback for transcription delta (partial/streaming result).
     */
    void setTranscriptionDeltaCallback(TranscriptionDeltaCallback cb);

    /**
     * Set callback for error events.
     */
    void setErrorCallback(ErrorCallback cb);

    /**
     * Process pending D-Bus messages (call from event loop).
     */
    void processEvents();

    /**
     * Get the D-Bus connection file descriptor for event loop integration.
     * @return file descriptor, or -1 if not connected
     */
    int getFileDescriptor();

    /**
     * Check if connected to daemon.
     */
    bool isConnected() const { return connected_; }

private:
    void connect();
    void disconnect();
    void callMethod(const char* method);
    void handleMessage(DBusMessage* msg);
    static DBusHandlerResult messageFilter(DBusConnection* conn,
                                          DBusMessage* msg,
                                          void* user_data);

    DBusConnection* conn_ = nullptr;
    TranscriptionCallback transcription_cb_;
    TranscriptionDeltaCallback transcription_delta_cb_;
    ErrorCallback error_cb_;
    bool connected_ = false;
};

} // namespace fcitx
