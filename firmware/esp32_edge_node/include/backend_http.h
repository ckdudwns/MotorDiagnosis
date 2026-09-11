#pragma once
#include <HTTPClient.h>
#include <WiFi.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_timer.h"

constexpr uint32_t HTTP_DIAG_LOG_INTERVAL_MS = 1000U;

struct HttpDiagState {
    uint64_t beginMs = 0;
    uint64_t postMs = 0;
    bool reuseCandidate = false;
    bool socketConnected = false;
    int errorCode = 0;
    String errorString = "none";
};

inline HttpDiagState& httpDiagState() {
    static HttpDiagState state;
    return state;
}

inline uint64_t httpDiagNowMs() {
    return static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;
}

inline bool shouldLogHttpDiag() {
    static uint32_t lastLogMs = 0;
    static bool logged = false;
    const uint32_t now = millis();
    if (logged && now - lastLogMs < HTTP_DIAG_LOG_INTERVAL_MS) return false;
    lastLogMs = now;
    logged = true;
    return true;
}

inline uint32_t nextHttpDiagEventId() {
    static uint32_t eventId = 0;
    return ++eventId;
}

inline String httpDiagHost(const char* url) {
    if (!url || !url[0]) return String();
    String host(url);
    const int scheme = host.indexOf("://");
    if (scheme >= 0) host = host.substring(scheme + 3);
    int authorityEnd = host.length();
    for (const char delimiter : {'/', '?', '#'}) {
        const int position = host.indexOf(delimiter);
        if (position >= 0 && position < authorityEnd) authorityEnd = position;
    }
    host.remove(authorityEnd);
    const int userInfo = host.lastIndexOf('@');
    if (userInfo >= 0) host = host.substring(userInfo + 1);
    if (host.startsWith("[")) {
        const int closing = host.indexOf(']');
        if (closing > 1) host = host.substring(1, closing);
    } else {
        const int port = host.indexOf(':');
        if (port >= 0) host.remove(port);
    }
    return host;
}

inline void logDnsFailureContext(const char* url, uint32_t eventId) {
    const String host = httpDiagHost(url);
    if (!host.length() || host.length() > 253) return;
    IPAddress resolved;
    const uint32_t startedMs = millis();
    const int rc = WiFi.hostByName(host.c_str(), resolved);
    Serial.printf("[TLS-DNS] event=%lu rc=%d elapsed_ms=%lu ip_present=%s wifi_status=%d rssi_dbm=%d\n",
                  static_cast<unsigned long>(eventId), rc,
                  static_cast<unsigned long>(millis() - startedMs),
                  rc == 1 ? "yes" : "no", static_cast<int>(WiFi.status()),
                  WiFi.status() == WL_CONNECTED ? static_cast<int>(WiFi.RSSI()) : 0);
}

struct HttpDiag {
    uint64_t startedMs = httpDiagNowMs();
    uint64_t beginMs = 0;
    uint64_t requestMs = 0;
    uint64_t bodyMs = 0;
};

inline void logHttpDiag(const char* op, const char* url, bool beginOk,
                        const HttpDiag& d, int status, int responseBytes,
                        const char* failStage = "none") {
    if (!shouldLogHttpDiag()) return;
    const HttpDiagState& state = httpDiagState();
    const wl_status_t wifiStatus = WiFi.status();
    const bool wifiConnected = wifiStatus == WL_CONNECTED;
    const IPAddress ip = WiFi.localIP();
    const bool ipPresent = ip[0] != 0 || ip[1] != 0 || ip[2] != 0 || ip[3] != 0;
    const int errorCode = status < 0 ? status : state.errorCode;
    const String errorString = errorCode < 0 && state.errorString.length()
        ? state.errorString
        : (errorCode < 0 ? HTTPClient::errorToString(errorCode) : String("none"));
    const uint64_t beginMs = d.beginMs ? d.beginMs : state.beginMs;
    const uint64_t postMs = d.requestMs ? d.requestMs : state.postMs;
    const uint32_t eventId = status <= 0 ? nextHttpDiagEventId() : 0;
    Serial.printf("[HTTP-DIAG] event=%lu op=%s endpoint=redacted begin=%s begin_ms=%llu post_ms=%llu body_read_ms=%llu status=%d response_bytes=%d fail_stage=%s error_code=%d error=%s wifi_connected=%s wifi_status=%d rssi_dbm=%d ip_present=%s heap_free=%u heap_min=%u psram_free=%u psram_min=%u reuse_candidate=%s socket_connected=%s total_ms=%llu\n",
        static_cast<unsigned long>(eventId), op ? op : "unknown", beginOk ? "ok" : "fail",
        static_cast<unsigned long long>(beginMs),
        static_cast<unsigned long long>(postMs),
        static_cast<unsigned long long>(d.bodyMs), status, responseBytes,
        failStage ? failStage : "none", errorCode,
        errorString.length() ? errorString.c_str() : "unknown",
        wifiConnected ? "yes" : "no", static_cast<int>(wifiStatus),
        wifiConnected ? static_cast<int>(WiFi.RSSI()) : 0,
        ipPresent ? "yes" : "no", static_cast<unsigned>(ESP.getFreeHeap()),
        static_cast<unsigned>(ESP.getMinFreeHeap()),
        static_cast<unsigned>(ESP.getFreePsram()),
        static_cast<unsigned>(ESP.getMinFreePsram()),
        state.reuseCandidate ? "yes" : "no",
        state.socketConnected ? "yes" : "no",
        static_cast<unsigned long long>(httpDiagNowMs() - d.startedMs));
    if (eventId) logDnsFailureContext(url, eventId);
}

// One TLS connection at a time on the no-PSRAM board. Sensor tasks never wait.
class BackendHttp : public HTTPClient {
    bool held_ = false;
public:
    static SemaphoreHandle_t gate;
    uint32_t lastGateWaitMs = 0;

    using HTTPClient::begin;
    using HTTPClient::POST;

private:
    void startBeginDiagnostic() {
        HttpDiagState& state = httpDiagState();
        state.beginMs = 0;
        state.postMs = 0;
        state.errorCode = 0;
        state.errorString = "none";
        state.reuseCandidate = connected();
        state.socketConnected = state.reuseCandidate;
    }

    bool finishBeginDiagnostic(uint64_t startedMs, bool result) {
        HttpDiagState& state = httpDiagState();
        state.beginMs = httpDiagNowMs() - startedMs;
        state.socketConnected = connected();
        if (!result) {
            state.errorCode = -1;
            state.errorString = "begin failed";
        }
        return result;
    }

    int finishPostDiagnostic(uint64_t startedMs, int status) {
        HttpDiagState& state = httpDiagState();
        state.postMs = httpDiagNowMs() - startedMs;
        state.socketConnected = connected();
        state.errorCode = status < 0 ? status : 0;
        state.errorString = status < 0 ? HTTPClient::errorToString(status) : String("none");
        if (status < 0 && !state.errorString.length()) state.errorString = "unknown";
        return status;
    }

public:
    bool begin(WiFiClient& client, String url) {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs, HTTPClient::begin(client, url));
    }

    bool begin(WiFiClient& client, String host, uint16_t port, String uri = "/", bool https = false) {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs, HTTPClient::begin(client, host, port, uri, https));
    }

#ifdef HTTPCLIENT_1_1_COMPATIBLE
    bool begin(String url) {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs, HTTPClient::begin(url));
    }

    bool begin(String url, const char* caCert) {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs, HTTPClient::begin(url, caCert));
    }

    bool begin(String host, uint16_t port, String uri = "/") {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs, HTTPClient::begin(host, port, uri));
    }

    bool begin(String host, uint16_t port, String uri, const char* caCert) {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs, HTTPClient::begin(host, port, uri, caCert));
    }

    bool begin(String host, uint16_t port, String uri, const char* caCert,
               const char* cliCert, const char* cliKey) {
        startBeginDiagnostic();
        const uint64_t startedMs = httpDiagNowMs();
        return finishBeginDiagnostic(startedMs,
            HTTPClient::begin(host, port, uri, caCert, cliCert, cliKey));
    }
#endif

    int POST(uint8_t* payload, size_t size) {
        const uint64_t startedMs = httpDiagNowMs();
        return finishPostDiagnostic(startedMs, HTTPClient::POST(payload, size));
    }

    int POST(String payload) {
        const uint64_t startedMs = httpDiagNowMs();
        return finishPostDiagnostic(startedMs, HTTPClient::POST(payload));
    }

    bool acquire() {
        if (held_) return true;
        const uint32_t started = millis();
        held_ = gate && xSemaphoreTake(gate, pdMS_TO_TICKS(250)) == pdTRUE;
        lastGateWaitMs = millis() - started;
        return held_;
    }
    void end() {
        HTTPClient::end();
        if (held_) {held_=false; xSemaphoreGive(gate);}
    }
    ~BackendHttp() {end();}
};
