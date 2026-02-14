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
    dbus_bus_add_match(conn_,
        "type='signal',interface='org.fcitx.Fcitx5.Voice'",
        &error);
    if (dbus_error_is_set(&error)) {
        FCITX_WARN() << "Failed to add D-Bus match: " << error.message;
        dbus_error_free(&error);
    }

    dbus_connection_add_filter(conn_, messageFilter, this, nullptr);

    connected_ = true;
    FCITX_INFO() << "Connected to fcitx5-voice daemon via D-Bus";
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
    FCITX_INFO() << "Called StartRecording on daemon";
}

void DBusClient::stopRecording() {
    if (!connected_) {
        throw std::runtime_error("Not connected to D-Bus");
    }
    callMethod("StopRecording");
    FCITX_INFO() << "Called StopRecording on daemon";
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

void DBusClient::setErrorCallback(ErrorCallback cb) {
    error_cb_ = std::move(cb);
}

void DBusClient::processEvents() {
    if (!conn_) return;

    while (dbus_connection_dispatch(conn_) == DBUS_DISPATCH_DATA_REMAINS) {
        // Process all pending messages
    }
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
            FCITX_INFO() << "Received transcription: " << text;
            if (transcription_cb_) {
                transcription_cb_(text, segment_num);
            }
        } else {
            FCITX_WARN() << "Failed to parse TranscriptionComplete: "
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
            FCITX_WARN() << "Daemon error: " << message;
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
    client->handleMessage(msg);
    return DBUS_HANDLER_RESULT_HANDLED;
}

} // namespace fcitx
