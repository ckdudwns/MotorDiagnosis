#include "network/wifi_time_sync.h"

#include <Arduino.h>
#include <WiFi.h>
#if __has_include("config/secrets.h")
#include <config/secrets.h>
#else
#include <config/secrets.example.h>
#endif
#include <esp_timer.h>
#include <sys/time.h>

namespace {

constexpr uint32_t kRetryIntervalMs = 10000;
constexpr uint32_t kTimeCheckIntervalMs = 1000;
constexpr time_t kMinimumValidEpoch = 1700000000;
constexpr char kPrimaryNtpServer[] = "pool.ntp.org";
constexpr char kFallbackNtpServer[] = "time.nist.gov";

bool deadlineReached(uint32_t now, uint32_t deadline) {
    return static_cast<int32_t>(now - deadline) >= 0;
}

void printUtc(time_t value) {
    struct tm utc = {};
    gmtime_r(&value, &utc);

    char formatted[32] = {};
    strftime(formatted, sizeof(formatted), "%Y-%m-%dT%H:%M:%SZ", &utc);
    Serial.print("[NTP] synced utc=");
    Serial.println(formatted);
}

}  // namespace

void WifiTimeSync::begin() {
    WiFi.mode(WIFI_STA);
    WiFi.persistent(false);
    WiFi.setAutoReconnect(true);

    Serial.println("[WIFI] connecting");
    WiFi.begin(WIFI_SSID_VALUE, WIFI_PASSWORD_VALUE);
    nextRetryAt_ = millis() + kRetryIntervalMs;
}

void WifiTimeSync::service() {
    const uint32_t now = millis();
    const bool isConnected = WiFi.status() == WL_CONNECTED;

    if (!isConnected) {
        if (wasConnected_) {
            Serial.println("[WIFI] disconnected");
            wasConnected_ = false;
            timeSyncRequested_ = false;
        }

        if (deadlineReached(now, nextRetryAt_)) {
            Serial.println("[WIFI] retry");
            WiFi.disconnect();
            WiFi.begin(WIFI_SSID_VALUE, WIFI_PASSWORD_VALUE);
            nextRetryAt_ = now + kRetryIntervalMs;
        }
        return;
    }

    if (!wasConnected_) {
        wasConnected_ = true;
        timeSyncRequested_ = true;
        nextTimeCheckAt_ = now;

        Serial.print("[WIFI] connected ip=");
        Serial.print(WiFi.localIP());
        Serial.print(" rssi=");
        Serial.println(WiFi.RSSI());

        configTime(0, 0, kPrimaryNtpServer, kFallbackNtpServer);
        Serial.println("[NTP] syncing");
    }

    if (timeSyncRequested_ && deadlineReached(now, nextTimeCheckAt_)) {
        const time_t current = time(nullptr);
        if (current >= kMinimumValidEpoch) {
            timeval currentTime = {};
            gettimeofday(&currentTime, nullptr);
            timeSyncRequested_ = false;
            timeSynced_ = true;
            syncEpochUs_ = static_cast<uint64_t>(currentTime.tv_sec) *
                               1000000ULL +
                           static_cast<uint64_t>(currentTime.tv_usec);
            syncUptimeUs_ = esp_timer_get_time();
            printUtc(current);
        } else {
            nextTimeCheckAt_ = now + kTimeCheckIntervalMs;
        }
    }
}

bool WifiTimeSync::connected() const {
    return WiFi.status() == WL_CONNECTED;
}

bool WifiTimeSync::utcNow(time_t& value) const {
    uint64_t valueUs = 0;
    if (!utcNowUs(valueUs)) {
        return false;
    }

    value = static_cast<time_t>(valueUs / 1000000ULL);
    return true;
}

bool WifiTimeSync::utcNowUs(uint64_t& value) const {
    return utcAtUptimeUs(static_cast<uint64_t>(esp_timer_get_time()), value);
}

bool WifiTimeSync::utcAtUptimeUs(uint64_t uptimeUs, uint64_t& value) const {
    if (!timeSynced_) {
        return false;
    }

    const int64_t deltaUs = static_cast<int64_t>(uptimeUs) - syncUptimeUs_;
    if (deltaUs >= 0) {
        value = syncEpochUs_ + static_cast<uint64_t>(deltaUs);
    } else {
        const uint64_t elapsedBeforeSync = static_cast<uint64_t>(-deltaUs);
        if (syncEpochUs_ < elapsedBeforeSync) {
            return false;
        }
        value = syncEpochUs_ - elapsedBeforeSync;
    }
    return true;
}
