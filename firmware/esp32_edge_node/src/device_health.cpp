#include "device_health.h"
#include "firmware_logic.h"
#include <ArduinoJson.h>
#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <ctime>

namespace DeviceHealth {
namespace {
constexpr std::uint32_t MAGIC = 0x4D444831U; // MDH1
constexpr std::int64_t MIN_EPOCH = 1700000000000LL;
constexpr std::int64_t MAX_EPOCH = 4102444800000LL;

std::uint32_t checksum(const Journal& journal) {
    const auto* bytes = reinterpret_cast<const unsigned char*>(&journal);
    std::uint32_t crc = 0xFFFFFFFFU;
    for (std::size_t i = 0; i < offsetof(Journal, crc); ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320U : 0);
    }
    return ~crc;
}
bool validTransition(const Transition& value) {
    return value.code < FAULT_COUNT && value.active <= 1 && value.reserved == 0 &&
           value.session != 0 && (value.epochMs == 0 ||
           (value.epochMs >= MIN_EPOCH && value.epochMs < MAX_EPOCH));
}
double usage(const Metrics& metrics) {
    return 100.0 * static_cast<double>(metrics.queuedRecords) / metrics.queueCapacity;
}
std::int64_t observationTime(const Transition& value, std::uint32_t session,
                             std::uint64_t monotonicMs, std::int64_t epochMs) {
    std::int64_t observed = value.epochMs;
    if (!observed && value.session == session && monotonicMs >= value.monotonicMs) {
        // ESP's 64-bit uptime avoids millis() wrapping during a long outage.
        const std::uint64_t elapsed = monotonicMs - value.monotonicMs;
        if (epochMs >= MIN_EPOCH && elapsed <= static_cast<std::uint64_t>(epochMs - MIN_EPOCH))
            observed = epochMs - static_cast<std::int64_t>(elapsed);
    }
    return observed >= MIN_EPOCH && observed < MAX_EPOCH ? observed : 0;
}
void addFault(JsonArray faults, const Transition& value, std::uint32_t session,
              std::uint64_t monotonicMs, std::int64_t epochMs, std::int64_t timeFloor) {
    JsonObject fault = faults.add<JsonObject>();
    fault["code"] = faultCode(static_cast<Fault>(value.code));
    fault["status"] = value.active ? "active" : "recovered";
    fault["severity"] = "warning";
    auto observed = observationTime(value, session, monotonicMs, epochMs);
    if (observed < timeFloor) observed = 0;
    const std::string timestamp = utcTimestamp(observed);
    if (!timestamp.empty()) {
        fault["occurredAt"] = timestamp;
        fault["detail"] = "ESP32 sensor self-report; not a training label.";
    } else {
        // Backend assigns receive time when occurredAt is omitted. Do not map a
        // previous boot's monotonic timestamp onto this boot's UTC anchor.
        fault["detail"] = "Observation UTC unavailable/older than acknowledged clock boundary; backend receive time used. Not a training label.";
    }
}
} // namespace

Journal emptyJournal() {
    Journal journal;
    journal.magic = MAGIC;
    journal.version = 1;
    journal.crc = checksum(journal);
    return journal;
}
bool validJournal(const Journal& journal) {
    if (journal.magic != MAGIC || journal.version != 1 ||
        journal.count > JOURNAL_CAPACITY || journal.known >> FAULT_COUNT ||
        journal.crc != checksum(journal)) return false;
    for (std::size_t i = 0; i < journal.count; ++i)
        if (!validTransition(journal.pending[i])) return false;
    for (std::size_t i = 0; i < FAULT_COUNT; ++i)
        if ((journal.acknowledgedTime[i] != 0 &&
             (journal.acknowledgedTime[i] < MIN_EPOCH || journal.acknowledgedTime[i] >= MAX_EPOCH)) ||
            ((journal.known & (1U << i)) &&
            (!validTransition(journal.latest[i]) || journal.latest[i].code != i))) return false;
    return true;
}
bool observe(Journal& journal, Fault fault, bool active, std::uint32_t session,
             std::uint64_t monotonicMs, std::int64_t epochMs) {
    Transition change;
    change.code = static_cast<std::uint8_t>(fault);
    change.active = active;
    change.session = session;
    change.monotonicMs = monotonicMs;
    change.epochMs = epochMs;
    if (!validJournal(journal) || !validTransition(change)) return false;
    const std::uint32_t bit = 1U << change.code;
    if ((journal.known & bit) && journal.latest[change.code].active == change.active)
        return true; // Keep the original transition time and avoid flash wear.
    if (journal.count == JOURNAL_CAPACITY) return false;
    journal.pending[journal.count++] = change;
    journal.latest[change.code] = change;
    journal.known |= bit;
    journal.crc = checksum(journal);
    return true;
}
bool acknowledgeHead(Journal& journal, std::uint32_t session,
                     std::uint64_t monotonicMs, std::int64_t reportedAtMs) {
    if (!validJournal(journal) || !journal.count) return false;
    if (reportedAtMs) {
        if (reportedAtMs < MIN_EPOCH || reportedAtMs >= MAX_EPOCH) return false;
        const auto& head = journal.pending[0];
        auto time = observationTime(head, session, monotonicMs, reportedAtMs);
        if (!time || time < journal.acknowledgedTime[head.code]) time = reportedAtMs;
        journal.acknowledgedTime[head.code] = time;
    }
    for (std::size_t i = 1; i < journal.count; ++i) journal.pending[i - 1] = journal.pending[i];
    journal.pending[--journal.count] = Transition{};
    journal.crc = checksum(journal);
    return true;
}
const char* faultCode(Fault fault) {
    static const char* codes[] = {"adxl345_init_failed", "inmp441_init_failed",
        "adxl345_channel_error", "inmp441_channel_error", "vibration_acquisition_timeout",
        "acoustic_window_unavailable", "sensor_features_invalid"};
    const auto index = static_cast<std::size_t>(fault);
    return index < FAULT_COUNT ? codes[index] : "unknown_sensor_fault";
}
bool hasPcmVariation(const std::int32_t* samples, std::size_t count) {
    if (!samples || count < 2) return false;
    for (std::size_t i = 1; i < count; ++i)
        if (samples[i] != samples[0]) return true;
    return false;
}
std::string utcTimestamp(std::int64_t epochMs) {
    if (epochMs < MIN_EPOCH || epochMs >= MAX_EPOCH) return {};
    const std::time_t seconds = static_cast<std::time_t>(epochMs / 1000);
    if (static_cast<std::int64_t>(seconds) != epochMs / 1000) return {};
    std::tm utc = {};
#ifdef _WIN32
    if (gmtime_s(&utc, &seconds) != 0) return {};
#else
    if (!gmtime_r(&seconds, &utc)) return {};
#endif
    char buffer[32];
    std::snprintf(buffer, sizeof(buffer), "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ",
                  utc.tm_year + 1900, utc.tm_mon + 1, utc.tm_mday, utc.tm_hour,
                  utc.tm_min, utc.tm_sec, static_cast<int>(epochMs % 1000));
    return buffer;
}
std::string payload(const Journal& journal, const Metrics& metrics,
                    std::uint32_t session, std::uint64_t monotonicMs, std::int64_t epochMs) {
    const auto timestamp = utcTimestamp(epochMs);
    if (!validJournal(journal) || !session || timestamp.empty() ||
        metrics.rssiDbm < -120 || metrics.rssiDbm > 0 || metrics.rebootCount > 2147483647U ||
        !metrics.queueCapacity || metrics.queuedRecords > metrics.queueCapacity ||
        !metrics.firmwareVersion || !*metrics.firmwareVersion || std::strlen(metrics.firmwareVersion) > 100)
        return {};
    JsonDocument document;
    document["reportedAt"] = timestamp;
    document["rssiDbm"] = metrics.rssiDbm;
    document["rebootCount"] = metrics.rebootCount;
    document["bufferUsagePct"] = usage(metrics);
    document["firmwareVersion"] = metrics.firmwareVersion;
    JsonArray faults = document["sensorFaults"].to<JsonArray>();
    if (journal.count) {
        addFault(faults, journal.pending[0], session, monotonicMs, epochMs,
                 journal.acknowledgedTime[journal.pending[0].code]);
    } else {
        for (std::size_t i = 0; i < FAULT_COUNT; ++i)
            if (journal.known & (1U << i)) addFault(faults, journal.latest[i], session, monotonicMs, epochMs, journal.acknowledgedTime[i]);
    }
    if (document.overflowed()) return {};
    std::string result;
    serializeJson(document, result);
    return result;
}
bool accepted(int httpStatus, const char* response, const char* deviceId, std::int64_t reportedAtMs) {
    if (httpStatus != 200 || !response || !deviceId) return false;
    JsonDocument document;
    if (deserializeJson(document, response) || !document.is<JsonObject>() ||
        !document["deviceId"].is<const char*>() || !document["lastReceivedAt"].is<const char*>() ||
        std::strcmp(document["deviceId"].as<const char*>(), deviceId) != 0) return false;
    std::int64_t acceptedAt = 0;
    return FirmwareLogic::parseRfc3339ToEpochMs(document["lastReceivedAt"].as<const char*>(), acceptedAt) &&
           acceptedAt == reportedAtMs;
}
bool metricsChanged(const Metrics& previous, const Metrics& current) {
    return std::abs(previous.rssiDbm - current.rssiDbm) >= 5 ||
        previous.rebootCount != current.rebootCount ||
        (previous.queueCapacity && current.queueCapacity && std::abs(usage(previous) - usage(current)) >= 5.0);
}
bool ReportSchedule::due(std::uint32_t now, bool changed, bool pending) const {
    if (!attempted_) return true;
    if (now - lastAttempt_ < retryMs_) return false;
    return !succeeded_ || changed || pending || now - lastSuccess_ >= 30000U;
}
void ReportSchedule::completed(std::uint32_t now, bool success) {
    attempted_ = true;
    lastAttempt_ = now;
    if (success) {
        succeeded_ = true;
        lastSuccess_ = now;
        retryMs_ = 5000;
    } else {
        retryMs_ = std::min<std::uint32_t>(retryMs_ * 2U, 60000U);
        succeeded_ = false;
    }
}
bool SensorRetry::due(std::uint32_t now) const {
    return !ready_ && attempts_ < 3 && (attempts_ == 0 || now - lastAttempt_ >= 5000U * attempts_);
}
void SensorRetry::attempted(std::uint32_t now, bool success) {
    if (attempts_ < 3) ++attempts_;
    lastAttempt_ = now;
    ready_ = success;
}
void SensorRetry::failed(std::uint32_t now) { ready_ = false; lastAttempt_ = now; }
} // namespace DeviceHealth
