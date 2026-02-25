#include "dbus_client.h"
#include <fcitx-utils/log.h>
#include <stdexcept>
#include <cstring>

namespace fcitx {

static const char* DBUS_SERVICE = "org.fcitx.Fcitx5.Voice";
static const char* DBUS_PATH = "/org/fcitx/Fcitx5/Voice";
static const char* DBUS_INTERFACE = "org.fcitx.Fcitx5.Voice";

DBusClient::DBusClient() {
    connect();
}

DBusClient::~DBusClient() {
    disconnect();
}

void DBusClient::connect() {
    DBusError error;
    dbus_error_init(&error);

    // Connect to session bus
    conn_ = dbus_bus_get(DBUS_BUS_SESSION, &error);
    if (dbus_error_is_set(&error)) {
        std::string msg = "Failed to connect to D-Bus: ";
        msg += error.message;
        dbus_error_free(&error);
        throw std::runtime_error(msg);
    }

    if (!conn_) {
        throw std::runtime_error("D-Bus connection is null");
    }

    // Add filter for signals
    // Note: Don't use sender='org.fcitx.Fcitx5.Voice' because D-Bus matches on unique names (:1.XXX), not well-known names
    const char* match_rule = "type='signal',interface='org.fcitx.Fcitx5.Voice',path='/org/fcitx/Fcitx5/Voice'";
    dbus_bus_add_match(conn_, match_rule, &error);
    if (dbus_error_is_set(&error)) {
        FCITX_ERROR() << "Failed to add D-Bus match: " << error.message;
        dbus_error_free(&error);
    }
    dbus_connection_flush(conn_);

    dbus_connection_add_filter(conn_, messageFilter, this, nullptr);

    connected_ = true;
}

void DBusClient::disconnect() {
    if (conn_) {
        dbus_connection_remove_filter(conn_, messageFilter, this);
        dbus_connection_unref(conn_);
        conn_ = nullptr;
    }
    connected_ = false;
}

void DBusClient::startRecording() {
    if (!connected_) {
        throw std::runtime_error("Not connected to D-Bus");
    }
    callMethod("StartRecording");
}

void DBusClient::stopRecording() {
    if (!connected_) {
        throw std::runtime_error("Not connected to D-Bus");
    }
    callMethod("StopRecording");
}

std::string DBusClient::getStatus() {
    if (!connected_) {
        throw std::runtime_error("Not connected to D-Bus");
    }

    DBusMessage* msg = dbus_message_new_method_call(
        DBUS_SERVICE, DBUS_PATH, DBUS_INTERFACE, "GetStatus");
    if (!msg) {
        throw std::runtime_error("Failed to create D-Bus message");
    }

    DBusError error;
    dbus_error_init(&error);

    DBusMessage* reply = dbus_connection_send_with_reply_and_block(
        conn_, msg, 1000, &error);
    dbus_message_unref(msg);

    if (dbus_error_is_set(&error)) {
        std::string err_msg = "D-Bus call failed: ";
        err_msg += error.message;
        dbus_error_free(&error);
        throw std::runtime_error(err_msg);
    }

    const char* status = nullptr;
    if (!dbus_message_get_args(reply, &error, DBUS_TYPE_STRING, &status,
                               DBUS_TYPE_INVALID)) {
        dbus_message_unref(reply);
        std::string err_msg = "Failed to parse reply: ";
        err_msg += error.message;
        dbus_error_free(&error);
        throw std::runtime_error(err_msg);
    }

    std::string result(status);
    dbus_message_unref(reply);
    return result;
}

void DBusClient::setTranscriptionCallback(TranscriptionCallback cb) {
    transcription_cb_ = std::move(cb);
}

void DBusClient::setTranscriptionDeltaCallback(TranscriptionDeltaCallback cb) {
    transcription_delta_cb_ = std::move(cb);
}

void DBusClient::setErrorCallback(ErrorCallback cb) {
    error_cb_ = std::move(cb);
}

void DBusClient::processEvents() {
    if (!conn_) return;

    // Read any incoming messages (non-blocking)
    if (!dbus_connection_read_write(conn_, 0)) {
        FCITX_WARN() << "D-Bus connection lost";
        return;
    }

    // Dispatch all pending messages
    while (dbus_connection_dispatch(conn_) == DBUS_DISPATCH_DATA_REMAINS) {
        // Keep dispatching
    }
}

int DBusClient::getFileDescriptor() {
    if (!conn_) return -1;

    int fd = -1;
    if (!dbus_connection_get_unix_fd(conn_, &fd)) {
        FCITX_WARN() << "Failed to get D-Bus file descriptor";
        return -1;
    }

    return fd;
}

void DBusClient::callMethod(const char* method) {
    DBusMessage* msg = dbus_message_new_method_call(
        DBUS_SERVICE, DBUS_PATH, DBUS_INTERFACE, method);
    if (!msg) {
        throw std::runtime_error("Failed to create D-Bus message");
    }

    DBusError error;
    dbus_error_init(&error);

    DBusMessage* reply = dbus_connection_send_with_reply_and_block(
        conn_, msg, 1000, &error);
    dbus_message_unref(msg);

    if (dbus_error_is_set(&error)) {
        std::string err_msg = "D-Bus call failed: ";
        err_msg += error.message;
        dbus_error_free(&error);
        throw std::runtime_error(err_msg);
    }

    if (reply) {
        dbus_message_unref(reply);
    }
}

void DBusClient::handleMessage(DBusMessage* msg) {
    if (dbus_message_is_signal(msg, DBUS_INTERFACE, "TranscriptionComplete")) {
        const char* text = nullptr;
        int segment_num = 0;

        DBusError error;
        dbus_error_init(&error);

        if (dbus_message_get_args(msg, &error,
                                 DBUS_TYPE_STRING, &text,
                                 DBUS_TYPE_INT32, &segment_num,
                                 DBUS_TYPE_INVALID)) {
            if (transcription_cb_) {
                transcription_cb_(text, segment_num);
            }
        } else {
            FCITX_WARN() << "Failed to parse TranscriptionComplete: "
                        << error.message;
            dbus_error_free(&error);
        }
    } else if (dbus_message_is_signal(msg, DBUS_INTERFACE, "TranscriptionDelta")) {
        const char* text = nullptr;

        DBusError error;
        dbus_error_init(&error);

        if (dbus_message_get_args(msg, &error,
                                 DBUS_TYPE_STRING, &text,
                                 DBUS_TYPE_INVALID)) {
            if (transcription_delta_cb_) {
                transcription_delta_cb_(text);
            }
        } else {
            FCITX_WARN() << "Failed to parse TranscriptionDelta: "
                        << error.message;
            dbus_error_free(&error);
        }
    } else if (dbus_message_is_signal(msg, DBUS_INTERFACE, "Error")) {
        const char* message = nullptr;

        DBusError error;
        dbus_error_init(&error);

        if (dbus_message_get_args(msg, &error,
                                 DBUS_TYPE_STRING, &message,
                                 DBUS_TYPE_INVALID)) {
            if (error_cb_) {
                error_cb_(message);
            }
        } else {
            dbus_error_free(&error);
        }
    }
}

DBusHandlerResult DBusClient::messageFilter(DBusConnection* conn,
                                           DBusMessage* msg,
                                           void* user_data) {
    auto* client = static_cast<DBusClient*>(user_data);

    const char* interface = dbus_message_get_interface(msg);
    if (interface && std::strcmp(interface, DBUS_INTERFACE) == 0) {
        client->handleMessage(msg);
        return DBUS_HANDLER_RESULT_HANDLED;
    }

    return DBUS_HANDLER_RESULT_NOT_YET_HANDLED;
}

} // namespace fcitx
