#include <Arduino.h>
#include <cmath>
#include <cstdlib>
#include <cstdio>
#include <ctime>
#include <cstring>
#include <esp_heap_caps.h>
#include <esp_random.h>
#include <esp_timer.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include <HTTPClient.h>
#include <mbedtls/sha256.h>
#include <WiFiClientSecure.h>

#include "config/app_config.h"
#if __has_include("config/secrets.h")
#include "config/secrets.h"
#else
#include "config/secrets.example.h"
#endif
#include "diagnostics/serial_report.h"
#include "hardware/adxl345_sensor.h"
#include "network/wifi_time_sync.h"
#include "policy/transmission_policy.h"
#include "sampling/vibration_sampler.h"
#include "sampling/vibration_window.h"
#include "storage/littlefs_window_store.h"

namespace {

// Production periodic policy: one feature summary every 25 seconds.
constexpr uint64_t kPeriodicIntervalUs = 25000000ULL;
constexpr uint64_t kAnomalyIntervalUs = 10000000ULL;
constexpr UBaseType_t kCandidateQueueDepth = 4;
constexpr uint32_t kRetryDelayMs = 1000;

Adxl345Sensor adxl345;
WifiTimeSync wifiTime;
VibrationSampler sampler(adxl345);
VibrationWindow window;
LittleFsWindowStore windowStore;
const TransmissionPolicyConfig transmissionPolicyConfig{
    app::kAnomalyDetectionEnabled,
    app::kAnomalyResultantRmsThresholdG,
    app::kAnomalyStrongestAcPeakThresholdG,
    app::kAnomalyUseOr,
    app::kAnomalyConsecutiveWindows,
    app::kRecoveryConsecutiveWindows,
    kAnomalyIntervalUs,
};
TransmissionPolicy transmissionPolicy(transmissionPolicyConfig);

QueueHandle_t candidateQueue = nullptr;
QueueHandle_t uploadWakeQueue = nullptr;
TransmissionCandidate candidate;
TransmissionCandidate pendingNtpCandidate;
TransmissionCandidate storageCandidate;
TransmissionCandidate* psramCandidateQueueBuffer = nullptr;
PendingWindow* psramUploadWindow = nullptr;
StaticQueue_t candidateQueueControl;

char bootId[33] = {};
uint32_t nextWindowIndex = 0;
uint32_t nextReportIndex = 0;

struct PeriodicSlot {
    bool initialized = false;
    bool hasValid = false;
    bool suppressed = false;
    uint64_t periodicSlotEpochUs = 0;
    uint32_t lastMeasuredWindowIndex = 0;
    uint64_t lastMeasuredAtEpochUs = 0;
    bool lastMeasuredAtValid = false;
    uint8_t anomalyCount = 0;
    uint8_t normalCount = 0;
    char invalidReason[48] = "no_valid_window";
    TransmissionCandidate latest;
};

PeriodicSlot periodicSlot;

bool enqueueCandidate(const TransmissionCandidate& value);

void createBootId() {
    const uint32_t words[] = {esp_random(), esp_random(), esp_random(),
                              esp_random()};
    snprintf(bootId, sizeof(bootId), "%08lx%08lx%08lx%08lx",
             static_cast<unsigned long>(words[0]),
             static_cast<unsigned long>(words[1]),
             static_cast<unsigned long>(words[2]),
             static_cast<unsigned long>(words[3]));
}

template <size_t Size>
void copyText(char (&destination)[Size], const char* source) {
    snprintf(destination, Size, "%s", source);
}

void fillIdentity(TelemetryMetadata& metadata) {
    copyText(metadata.deviceId, app::kDeviceId);
    copyText(metadata.siteId, app::kSiteId);
    copyText(metadata.assetId, app::kAssetId);
    copyText(metadata.sensorId, app::kSensorId);
    copyText(metadata.bootId, bootId);
}

void preparePendingMetadata(PendingWindow& pending, uint32_t index,
                            uint64_t startUptimeUs, uint64_t measuredUptimeUs,
    uint64_t measuredEpochUs, bool timestampValid) {
    TelemetryMetadata& metadata = pending.metadata;
    metadata = TelemetryMetadata{};
    metadata.schemaVersion = 2;
    fillIdentity(metadata);
    metadata.measuredWindowIndex = index;
    metadata.windowMeasuredUptimeUs = measuredUptimeUs;
    metadata.startUptimeUs = startUptimeUs;
    metadata.sampleCount = pending.stats.sampleCount;
    metadata.featuresValid = pending.stats.featuresValid ? 1 : 0;
    copyText(metadata.quality, "valid");
    copyText(metadata.reason, "none");
    if (timestampValid) {
        metadata.windowMeasuredAtEpochUs = measuredEpochUs;
        metadata.windowMeasuredAtValid = 1;
    }
}

void setInvalidReason(TelemetryMetadata& metadata, const char* reason) {
    copyText(metadata.quality, "invalid");
    copyText(metadata.reason, reason);
    metadata.featuresValid = 0;
    metadata.sampleCount = 0;
}

const char* candidateType(CandidateReason reason) {
    return reason == CandidateReason::anomalyStart ? "anomaly_start"
           : reason == CandidateReason::anomalyActive ? "anomaly_active"
           : reason == CandidateReason::recovery ? "recovery"
                                                  : "periodic";
}

void resetPeriodicSlot(uint64_t periodicSlotEpochUs) {
    periodicSlot = PeriodicSlot{};
    periodicSlot.initialized = true;
    periodicSlot.periodicSlotEpochUs = periodicSlotEpochUs;
    copyText(periodicSlot.invalidReason, "no_valid_window");
}

void emitPeriodicSlot() {
    if (periodicSlot.suppressed) {
        return;
    }

    TransmissionCandidate value = periodicSlot.latest;
    if (!periodicSlot.hasValid) {
        value = TransmissionCandidate{};
        TelemetryMetadata& metadata = value.window.metadata;
        metadata.schemaVersion = 2;
        fillIdentity(metadata);
        metadata.measuredWindowIndex = periodicSlot.lastMeasuredWindowIndex;
        metadata.windowMeasuredAtEpochUs = periodicSlot.lastMeasuredAtEpochUs;
        metadata.windowMeasuredAtValid = periodicSlot.lastMeasuredAtValid;
        metadata.anomalyCount = periodicSlot.anomalyCount;
        metadata.normalCount = periodicSlot.normalCount;
        setInvalidReason(metadata, periodicSlot.invalidReason);
        value.reason = CandidateReason::periodic;
    }

    value.window.metadata.periodicSlotEpochUs = periodicSlot.periodicSlotEpochUs;
    value.window.metadata.periodicSlotEpochValid = 1;
    value.window.metadata.windowIndex = nextReportIndex++;
    if (periodicSlot.hasValid) {
        copyText(value.window.metadata.reason, "none");
    }
    enqueueCandidate(value);
}

void advancePeriodicSlot(uint64_t measuredEpochUs) {
    const uint64_t slotUs =
        (measuredEpochUs / kPeriodicIntervalUs) * kPeriodicIntervalUs;
    if (!periodicSlot.initialized) {
        resetPeriodicSlot(slotUs);
        return;
    }
    while (periodicSlot.periodicSlotEpochUs < slotUs) {
        emitPeriodicSlot();
        resetPeriodicSlot(periodicSlot.periodicSlotEpochUs + kPeriodicIntervalUs);
    }
}

void noteValidPeriodic(const TransmissionCandidate& value,
                       uint64_t measuredEpochUs,
                       const TransmissionPolicyDecision& decision) {
    advancePeriodicSlot(measuredEpochUs);
    periodicSlot.lastMeasuredWindowIndex =
        value.window.metadata.measuredWindowIndex;
    periodicSlot.lastMeasuredAtEpochUs =
        value.window.metadata.windowMeasuredAtEpochUs;
    periodicSlot.lastMeasuredAtValid = value.window.metadata.windowMeasuredAtValid;
    periodicSlot.anomalyCount = decision.anomalyCount;
    periodicSlot.normalCount = decision.normalCount;
    if (decision.state != TransmissionPolicyState::normal ||
        decision.action != TransmissionPolicyAction::discard) {
        periodicSlot.suppressed = true;
        return;
    }
    periodicSlot.latest = value;
    periodicSlot.hasValid = true;
}

void noteInvalidPeriodic(uint64_t measuredEpochUs, uint32_t windowIndex,
                         const char* reason,
    const TransmissionPolicyDecision& decision) {
    advancePeriodicSlot(measuredEpochUs);
    periodicSlot.lastMeasuredWindowIndex = windowIndex;
    periodicSlot.lastMeasuredAtEpochUs = measuredEpochUs;
    periodicSlot.lastMeasuredAtValid = true;
    periodicSlot.anomalyCount = decision.anomalyCount;
    periodicSlot.normalCount = decision.normalCount;
    copyText(periodicSlot.invalidReason, reason);
    if (decision.state == TransmissionPolicyState::anomalyActive) {
        periodicSlot.suppressed = true;
    }
}

bool formatTimestamp(uint64_t epochUs, char (&output)[40]) {
    const time_t seconds = static_cast<time_t>(epochUs / 1000000ULL);
    const uint32_t micros = static_cast<uint32_t>(epochUs % 1000000ULL);
    struct tm utc = {};
    if (gmtime_r(&seconds, &utc) == nullptr) {
        return false;
    }

    snprintf(output, sizeof(output),
             "%04d-%02d-%02dT%02d:%02d:%02d.%06luZ", utc.tm_year + 1900,
             utc.tm_mon + 1, utc.tm_mday, utc.tm_hour, utc.tm_min, utc.tm_sec,
             static_cast<unsigned long>(micros));
    return true;
}

void appendUint64(String& output, uint64_t value) {
    char number[24] = {};
    snprintf(number, sizeof(number), "%llu",
             static_cast<unsigned long long>(value));
    output += number;
}

struct WireFeatureValue {
    char text[24] = {};
    double value = 0.0;
};

bool encodeFeatureValues(const VibrationFeatures& features,
                         WireFeatureValue (&encoded)[9]) {
    const float sourceValues[9] = {
        features.cfA1, features.cfA2, features.cfA3,
        features.skewnessA1, features.skewnessA2, features.skewnessA3,
        features.kurtosisA1, features.kurtosisA2, features.kurtosisA3,
    };
    for (size_t index = 0; index < 9; ++index) {
        if (!isfinite(sourceValues[index])) {
            return false;
        }
        snprintf(encoded[index].text, sizeof(encoded[index].text), "%.6f",
                 static_cast<double>(sourceValues[index]));
        encoded[index].value = strtod(encoded[index].text, nullptr);
        if (!isfinite(encoded[index].value)) {
            return false;
        }
    }
    return true;
}

bool calculateFeatureDigest(const WireFeatureValue* values, size_t count,
                            char (&output)[65]) {
    static_assert(sizeof(double) == 8,
                  "The feature digest requires 8-byte doubles.");
    mbedtls_sha256_context context;
    mbedtls_sha256_init(&context);
    if (mbedtls_sha256_starts_ret(&context, 0) != 0) {
        mbedtls_sha256_free(&context);
        return false;
    }
    for (size_t index = 0; index < count; ++index) {
        if (mbedtls_sha256_update_ret(
                &context,
                reinterpret_cast<const unsigned char*>(&values[index].value),
                sizeof(values[index].value)) != 0) {
            mbedtls_sha256_free(&context);
            return false;
        }
    }
    unsigned char digest[32] = {};
    const int result = mbedtls_sha256_finish_ret(&context, digest);
    mbedtls_sha256_free(&context);
    if (result != 0) {
        return false;
    }
    for (size_t index = 0; index < sizeof(digest); ++index) {
        snprintf(output + index * 2, 3, "%02x", digest[index]);
    }
    output[64] = '\0';
    return true;
}

bool buildUploadPayload(const PendingWindow& pending, CandidateReason reason,
                        String& payload, char (&featureDigest)[65]) {
    const bool periodic = reason == CandidateReason::periodic;
    if (!pending.metadata.windowMeasuredAtValid ||
        (periodic && !pending.metadata.periodicSlotEpochValid) ||
        (pending.metadata.featuresValid && !pending.stats.featuresValid)) {
        return false;
    }

    char timestamp[40] = {};
    if (!formatTimestamp(pending.metadata.windowMeasuredAtEpochUs, timestamp)) {
        return false;
    }

    WireFeatureValue encoded[9] = {};
    size_t encodedCount = 0;
    if (pending.metadata.featuresValid) {
        if (!encodeFeatureValues(pending.stats.features, encoded)) {
            return false;
        }
        encodedCount = 9;
    }
    if (!calculateFeatureDigest(encoded, encodedCount, featureDigest)) {
        return false;
    }

    payload.reserve(1200);
    payload = "{\"window\":{\"schemaVersion\":";
    payload += String(pending.metadata.schemaVersion);
    payload += ",\"deviceId\":\"";
    payload += pending.metadata.deviceId;
    payload += "\",\"siteId\":\"";
    payload += pending.metadata.siteId;
    payload += "\",\"assetId\":\"";
    payload += pending.metadata.assetId;
    payload += "\",\"sensorId\":\"";
    payload += pending.metadata.sensorId;
    payload += "\",\"bootId\":\"";
    payload += pending.metadata.bootId;
    payload += "\",\"windowIndex\":";
    payload += String(pending.metadata.windowIndex);
    payload += ",\"timestamp\":\"";
    payload += timestamp;
    payload += "\",\"startUptimeUs\":";
    appendUint64(payload, pending.metadata.startUptimeUs);
    payload += ",\"sampleRateHz\":";
    payload += String(app::kVibrationSampleRateHz);
    payload += ",\"sampleCount\":";
    payload += String(pending.metadata.sampleCount);
    payload += ",\"profileId\":\"";
    payload += app::kFeatureProfileId;
    payload += "\",\"axes\":[\"X\",\"Y\",\"Z\"],\"unit\":\"dimensionless\"";
    payload += ",\"quality\":\"";
    payload += pending.metadata.quality;
    payload += "\",\"reason\":";
    if (strcmp(pending.metadata.reason, "none") == 0) {
        payload += "null";
    } else {
        payload += "\"";
        payload += pending.metadata.reason;
        payload += "\"";
    }
    payload += ",\"features\":";
    if (encodedCount == 0) {
        payload += "null";
    } else {
        payload += "{\"cf_a_1\":";
        payload += encoded[0].text;
        payload += ",\"cf_a_2\":";
        payload += encoded[1].text;
        payload += ",\"cf_a_3\":";
        payload += encoded[2].text;
        payload += ",\"sk_a_1\":";
        payload += encoded[3].text;
        payload += ",\"sk_a_2\":";
        payload += encoded[4].text;
        payload += ",\"sk_a_3\":";
        payload += encoded[5].text;
        payload += ",\"ku_a_1\":";
        payload += encoded[6].text;
        payload += ",\"ku_a_2\":";
        payload += encoded[7].text;
        payload += ",\"ku_a_3\":";
        payload += encoded[8].text;
        payload += "}";
    }
    payload += ",\"integrity\":{\"algorithm\":\"sha256\",\"digest\":\"";
    payload += featureDigest;
    payload += "\"},\"periodicSlotEpoch\":";
    if (periodic) {
        appendUint64(payload, pending.metadata.periodicSlotEpochUs / 1000000ULL);
    } else {
        payload += "null";
    }
    payload += "},\"transmission\":{\"policyId\":\"";
    payload += app::kPolicyId;
    payload += "\",\"eventType\":\"";
    payload += candidateType(reason);
    payload += "\",\"state\":\"";
    payload += (reason == CandidateReason::anomalyStart ||
                        reason == CandidateReason::anomalyActive
                    ? "ANOMALY_ACTIVE"
                    : "NORMAL");
    payload += "\",\"anomalyCount\":";
    payload += String(pending.metadata.anomalyCount);
    payload += ",\"normalCount\":";
    payload += String(pending.metadata.normalCount);
    payload += "}}";
    return true;
}

bool matchStringField(const String& response, const char* key,
                      const char* expected, int begin, int end) {
    String quotedKey = "\"";
    quotedKey += key;
    quotedKey += "\"";
    const int keyStart = response.indexOf(quotedKey, begin);
    if (keyStart < begin || keyStart >= end) {
        return false;
    }
    const int colon = response.indexOf(':', keyStart + quotedKey.length());
    if (colon < 0 || colon >= end) {
        return false;
    }
    int valueStart = colon + 1;
    while (valueStart < end && (response.charAt(valueStart) == ' ' ||
                                response.charAt(valueStart) == '\n' ||
                                response.charAt(valueStart) == '\r' ||
                                response.charAt(valueStart) == '\t')) {
        ++valueStart;
    }
    if (valueStart >= end || response.charAt(valueStart) != '"') {
        return false;
    }
    const int valueEnd = response.indexOf('"', valueStart + 1);
    if (valueEnd < 0 || valueEnd > end) {
        return false;
    }
    const size_t length = static_cast<size_t>(valueEnd - valueStart - 1);
    return length == strlen(expected) &&
           strncmp(response.c_str() + valueStart + 1, expected, length) == 0;
}

bool matchUnsignedField(const String& response, const char* key,
                        uint32_t expected, int begin, int end) {
    String quotedKey = "\"";
    quotedKey += key;
    quotedKey += "\"";
    const int keyStart = response.indexOf(quotedKey, begin);
    if (keyStart < begin || keyStart >= end) {
        return false;
    }
    const int colon = response.indexOf(':', keyStart + quotedKey.length());
    if (colon < 0 || colon >= end) {
        return false;
    }
    int digit = colon + 1;
    while (digit < end && (response.charAt(digit) == ' ' ||
                           response.charAt(digit) == '\n' ||
                           response.charAt(digit) == '\r' ||
                           response.charAt(digit) == '\t')) {
        ++digit;
    }
    if (digit >= end || response.charAt(digit) < '0' ||
        response.charAt(digit) > '9') {
        return false;
    }
    uint32_t value = 0;
    while (digit < end && response.charAt(digit) >= '0' &&
           response.charAt(digit) <= '9') {
        const uint32_t next = value * 10U +
                              static_cast<uint32_t>(response.charAt(digit) - '0');
        if (next < value) {
            return false;
        }
        value = next;
        ++digit;
    }
    return value == expected;
}

bool matchBoolField(const String& response, const char* key, bool expected,
                    int begin, int end) {
    String quotedKey = "\"";
    quotedKey += key;
    quotedKey += "\"";
    const int keyStart = response.indexOf(quotedKey, begin);
    if (keyStart < begin || keyStart >= end) {
        return false;
    }
    const int colon = response.indexOf(':', keyStart + quotedKey.length());
    if (colon < 0 || colon >= end) {
        return false;
    }
    int valueStart = colon + 1;
    while (valueStart < end && (response.charAt(valueStart) == ' ' ||
                                response.charAt(valueStart) == '\n' ||
                                response.charAt(valueStart) == '\r' ||
                                response.charAt(valueStart) == '\t')) {
        ++valueStart;
    }
    const char* literal = expected ? "true" : "false";
    const size_t length = strlen(literal);
    return valueStart >= 0 && valueStart + length <= static_cast<size_t>(end) &&
           response.substring(valueStart, valueStart + length) == literal;
}

bool ackMatches(const String& response, const PendingWindow& pending,
                CandidateReason reason, const char* featureDigest,
                int statusCode) {
    const int acknowledgedKey = response.indexOf("\"acknowledged\"");
    if (acknowledgedKey < 0) {
        return false;
    }
    const int arrayStart = response.indexOf('[', acknowledgedKey);
    const int arrayEnd = response.indexOf(']', arrayStart);
    const int objectStart = response.indexOf('{', arrayStart);
    const int objectEnd = response.indexOf('}', objectStart);
    if (arrayStart < 0 || arrayEnd < 0 ||
        objectStart < arrayStart || objectStart > arrayEnd || objectEnd < 0 ||
        objectEnd > arrayEnd) {
        return false;
    }

    const bool newRecord = statusCode == 202 &&
                           matchUnsignedField(response, "accepted", 1, 0,
                                               acknowledgedKey) &&
                           matchBoolField(response, "duplicate", false, 0,
                                          acknowledgedKey);
    const bool duplicateRecord = statusCode == 200 &&
                                 matchUnsignedField(response, "accepted", 0,
                                                    0, acknowledgedKey) &&
                                 matchBoolField(response, "duplicate", true, 0,
                                                acknowledgedKey);
    return (newRecord || duplicateRecord) &&
           matchStringField(response, "deviceId", pending.metadata.deviceId, 0,
                            acknowledgedKey) &&
           matchStringField(response, "policyId", app::kPolicyId, 0,
                            acknowledgedKey) &&
           matchStringField(response, "sensorId", pending.metadata.sensorId,
                            objectStart, objectEnd) &&
           matchStringField(response, "bootId", pending.metadata.bootId,
                            objectStart, objectEnd) &&
           matchUnsignedField(response, "windowIndex",
                              pending.metadata.windowIndex, objectStart,
                              objectEnd) &&
           matchStringField(response, "eventType", candidateType(reason),
                            objectStart, objectEnd) &&
           matchStringField(response, "featureDigest", featureDigest,
                            objectStart, objectEnd) &&
           matchBoolField(response, "durablyStored", true, objectStart,
                          objectEnd);
}

bool enqueueCandidate(const TransmissionCandidate& value) {
    if (xQueueSend(candidateQueue, &value, 0) != pdTRUE) {
        Serial.println("[POLICY] candidate_queue_full");
        return false;
    }

    Serial.print("[POLICY] candidate=");
    Serial.println(value.reason == CandidateReason::anomalyStart ? "anomaly_start"
                    : value.reason == CandidateReason::anomalyActive ? "anomaly_active"
                    : value.reason == CandidateReason::recovery ? "recovery"
                                                                  : "periodic");
    return true;
}

bool pendingNtpCandidateValid = false;

void flushPendingNtpCandidate() {
    if (!pendingNtpCandidateValid || !wifiTime.timeSynced()) {
        return;
    }

    uint64_t timestampEpochUs = 0;
    if (!wifiTime.utcAtUptimeUs(
            pendingNtpCandidate.window.metadata.windowMeasuredUptimeUs,
            timestampEpochUs)) {
        return;
    }

    pendingNtpCandidate.window.metadata.windowMeasuredAtEpochUs =
        timestampEpochUs;
    pendingNtpCandidate.window.metadata.windowMeasuredAtValid = 1;
    if (!enqueueCandidate(pendingNtpCandidate)) {
        return;
    }

    Serial.print("[POLICY] pending_candidate_released windowIndex=");
    Serial.println(pendingNtpCandidate.window.metadata.windowIndex);
    pendingNtpCandidateValid = false;
}

void printStorageResult(const WindowWriteResult& result) {
    Serial.print("[STORAGE] ok=");
    Serial.print(result.ok ? "true" : "false");
    Serial.print(" bytes=");
    Serial.print(result.bytes);
    Serial.print(" write_us=");
    Serial.print(result.writeUs);
    if (result.path[0] != '\0') {
        Serial.print(" path=");
        Serial.print(result.path);
    }
    Serial.print(" fs_used_bytes=");
    Serial.print(result.fsUsedBytes);
    Serial.print(" fs_free_bytes=");
    Serial.println(result.fsFreeBytes);
}

CandidateReason candidateReasonFromAction(TransmissionPolicyAction action) {
    if (action == TransmissionPolicyAction::recovery) {
        return CandidateReason::recovery;
    }
    if (action == TransmissionPolicyAction::anomalyStart) {
        return CandidateReason::anomalyStart;
    }
    if (action == TransmissionPolicyAction::anomalyActive) {
        return CandidateReason::anomalyActive;
    }
    return CandidateReason::periodic;
}

void applyPolicyMetadata(TelemetryMetadata& metadata,
                         const TransmissionPolicyDecision& decision) {
    metadata.anomalyCount = decision.anomalyCount;
    metadata.normalCount = decision.normalCount;
    if (decision.action == TransmissionPolicyAction::recovery) {
        metadata.normalCount = app::kRecoveryConsecutiveWindows;
    }
}

void assignReportIndex(TransmissionCandidate& value) {
    value.window.metadata.windowIndex = nextReportIndex++;
}

const char* invalidReason(CaptureQuality quality) {
    switch (quality) {
        case CaptureQuality::fifo_overrun:
            return "fifo_overrun";
        case CaptureQuality::sensor_unavailable:
            return "sensor_unavailable";
        case CaptureQuality::timeout:
            return "timeout";
        case CaptureQuality::valid:
        default:
            return "no_valid_window";
    }
}

void printPolicyDecision(const TransmissionPolicyDecision& decision,
                         uint32_t windowIndex) {
    Serial.print("[POLICY] state=");
    Serial.print(transmissionPolicyStateName(decision.state));
    Serial.print(" action=");
    Serial.print(transmissionPolicyActionName(decision.action));
    Serial.print(" anomalyCount=");
    Serial.print(decision.anomalyCount);
    Serial.print(" normalCount=");
    Serial.print(decision.normalCount);
    Serial.print(" windowIndex=");
    Serial.println(windowIndex);
}

void uploadTask(void*) {
    for (;;) {
        RetryFileRef retryFile;
        if (!windowStore.claimNextRetryFile(retryFile)) {
            uint8_t signal = 0;
            xQueueReceive(uploadWakeQueue, &signal, portMAX_DELAY);
            continue;
        }

        if (!windowStore.readCandidate(retryFile, *psramUploadWindow)) {
            Serial.print("[UPLOAD] retry_read_failed path=");
            Serial.println(retryFile.path);
            windowStore.releaseCandidate(retryFile);
            vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
            continue;
        }

        if (!wifiTime.connected() || !wifiTime.timeSynced()) {
            windowStore.releaseCandidate(retryFile);
            vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
            continue;
        }

        String payload;
        char featureDigest[65] = {};
        if (!buildUploadPayload(*psramUploadWindow, retryFile.reason,
                                payload, featureDigest)) {
            Serial.print("[UPLOAD] payload_build_failed path=");
            Serial.println(retryFile.path);
            windowStore.releaseCandidate(retryFile);
            vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
            continue;
        }

        WiFiClientSecure client;
        client.setCACert(BACKEND_CA_CERT_VALUE);
        HTTPClient http;
        if (!http.begin(client, INGEST_URL_VALUE)) {
            Serial.println("[UPLOAD] http_begin_failed");
            windowStore.releaseCandidate(retryFile);
            vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
            continue;
        }

        http.setTimeout(10000);
        http.addHeader("Authorization", "Bearer " INGEST_TOKEN_VALUE);
        http.addHeader("Content-Type", "application/json");
        const uint32_t requestStartedAt = micros();
        const int statusCode = http.POST(payload);
        const String response = http.getString();
        http.end();
        const uint32_t requestUs = micros() - requestStartedAt;

        const bool acknowledged =
            ackMatches(response, *psramUploadWindow, retryFile.reason,
                       featureDigest, statusCode);
        const char* candidateReason =
            retryFile.reason == CandidateReason::recovery ? "recovery"
            : retryFile.reason == CandidateReason::anomalyStart ? "anomaly_start"
            : retryFile.reason == CandidateReason::anomalyActive ? "anomaly_active"
                                                                  : "periodic";
        Serial.print("[UPLOAD] http_status=");
        Serial.print(statusCode);
        Serial.print(" reason=");
        Serial.print(candidateReason);
        Serial.print(" payload_bytes=");
        Serial.print(payload.length());
        Serial.print(" request_us=");
        Serial.print(requestUs);
        Serial.print(" ack=");
        Serial.println(acknowledged ? "true" : "false");
        if (statusCode < 200 || statusCode >= 300) {
            Serial.print("[UPLOAD] response=");
            Serial.println(response);
        }

        if (acknowledged) {
            const bool removed = windowStore.removeCandidate(retryFile);
            Serial.print("[UPLOAD] retry_removed=");
            Serial.println(removed ? "true" : "false");
            if (removed) {
                continue;
            }
        } else {
            windowStore.releaseCandidate(retryFile);
        }

        Serial.println("[UPLOAD] retry_kept");
        vTaskDelay(pdMS_TO_TICKS(kRetryDelayMs));
    }
}

void wifiTask(void*) {
    for (;;) {
        wifiTime.service();
        vTaskDelay(pdMS_TO_TICKS(10));
    }
}

void storageTask(void*) {
    for (;;) {
        if (xQueueReceive(candidateQueue, &storageCandidate,
                          portMAX_DELAY) != pdTRUE) {
            continue;
        }

        const WindowWriteResult result =
            windowStore.saveCandidate(storageCandidate);
        printStorageResult(result);
        if (result.ok) {
            uint8_t signal = 1;
            xQueueSend(uploadWakeQueue, &signal, 0);
        }
    }
}

void sensorTask(void*) {
    if (!adxl345.startMeasurement()) {
        Serial.println("[ADXL345] measurement_start_failed");
        vTaskDelete(nullptr);
        return;
    }
    Serial.println("[ADXL345] measurement_started");

    for (;;) {
        const uint64_t captureStartUs =
            static_cast<uint64_t>(esp_timer_get_time());
        const CaptureResult captureResult =
            sampler.capture(window, app::kWindowCaptureTimeoutMs);
        const uint64_t captureEndUs =
            static_cast<uint64_t>(esp_timer_get_time());

        if (!adxl345.stopMeasurement()) {
            Serial.println("[ADXL345] measurement_stop_failed");
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }

        const uint32_t windowIndex = nextWindowIndex++;
        uint64_t captureStartEpochUs = 0;
        const bool captureStartTimeValid =
            wifiTime.utcAtUptimeUs(captureStartUs, captureStartEpochUs);

        if (!captureResult.ok()) {
            printCaptureResult(captureResult, window);
            TransmissionPolicyInput policyInput;
            policyInput.valid = false;
            policyInput.utcValid = captureStartTimeValid;
            policyInput.utcUs = captureStartEpochUs;
            policyInput.uptimeUs = captureEndUs;
            const TransmissionPolicyDecision policyDecision =
                transmissionPolicy.evaluate(policyInput);
            printPolicyDecision(policyDecision, windowIndex);
            if (captureStartTimeValid) {
                noteInvalidPeriodic(captureStartEpochUs, windowIndex,
                                    invalidReason(captureResult.quality),
                                    policyDecision);
            }
            flushPendingNtpCandidate();
            if (!adxl345.startMeasurement()) {
                Serial.println("[ADXL345] measurement_restart_failed");
                vTaskDelete(nullptr);
                return;
            }
            continue;
        }

        const WindowStats stats = window.summarize();
        candidate = TransmissionCandidate{};
        candidate.window.stats = stats;
        preparePendingMetadata(candidate.window, windowIndex, captureStartUs,
                               captureStartUs, captureStartEpochUs,
                               captureStartTimeValid);
        TransmissionPolicyInput policyInput;
        policyInput.valid = true;
        policyInput.stats = stats;
        policyInput.utcValid = captureStartTimeValid;
        policyInput.utcUs = captureStartEpochUs;
        policyInput.uptimeUs = captureEndUs;
        const TransmissionPolicyDecision policyDecision =
            transmissionPolicy.evaluate(policyInput);
        applyPolicyMetadata(candidate.window.metadata, policyDecision);

        printCaptureResult(captureResult, window);
        printPolicyDecision(policyDecision, windowIndex);
        if (captureStartTimeValid) {
            noteValidPeriodic(candidate, captureStartEpochUs, policyDecision);
        }
        flushPendingNtpCandidate();
        if (policyDecision.action != TransmissionPolicyAction::discard) {
            candidate.reason =
                candidateReasonFromAction(policyDecision.action);
            assignReportIndex(candidate);
            if (!candidate.window.metadata.windowMeasuredAtValid) {
                if (!pendingNtpCandidateValid) {
                    pendingNtpCandidate = candidate;
                    pendingNtpCandidateValid = true;
                    Serial.println("[POLICY] candidate_pending_ntp");
                } else {
                    Serial.println("[POLICY] candidate_pending_ntp_full");
                }
            } else {
                enqueueCandidate(candidate);
            }
        }

        if (!adxl345.startMeasurement()) {
            Serial.println("[ADXL345] measurement_restart_failed");
            vTaskDelete(nullptr);
            return;
        }
    }
}

}  // namespace

void setup() {
    Serial.begin(app::kSerialBaudRate);
    delay(300);
    Serial.println("[BOOT] motor2 firmware phase=edge-policy");
    createBootId();
    Serial.print("[BOOT] boot_id=");
    Serial.println(bootId);

    wifiTime.begin();

    if (!adxl345.begin()) {
        Serial.println("[ADXL345] init_failed");
        return;
    }

    Serial.print("[ADXL345] device_id=0x");
    Serial.println(adxl345.deviceId(), HEX);

    if (!windowStore.begin()) {
        Serial.println("[STORAGE] init_failed");
        return;
    }

    const size_t candidateQueueBytes =
        sizeof(TransmissionCandidate) * kCandidateQueueDepth;
    psramCandidateQueueBuffer = static_cast<TransmissionCandidate*>(
        heap_caps_malloc(candidateQueueBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    psramUploadWindow = static_cast<PendingWindow*>(
        heap_caps_malloc(sizeof(PendingWindow), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (psramCandidateQueueBuffer == nullptr || psramUploadWindow == nullptr) {
        Serial.println("[PSRAM] allocation_failed");
        return;
    }

    candidateQueue = xQueueCreateStatic(
        kCandidateQueueDepth, sizeof(TransmissionCandidate),
        reinterpret_cast<uint8_t*>(psramCandidateQueueBuffer),
        &candidateQueueControl);
    uploadWakeQueue = xQueueCreate(1, sizeof(uint8_t));
    if (candidateQueue == nullptr || uploadWakeQueue == nullptr) {
        Serial.println("[QUEUE] create_failed");
        return;
    }

    if (xTaskCreatePinnedToCore(storageTask, "storage", 8192, nullptr, 1,
                                nullptr, 0) != pdPASS) {
        Serial.println("[STORAGE] task_start_failed");
        return;
    }
    if (xTaskCreatePinnedToCore(wifiTask, "wifi", 4096, nullptr, 1, nullptr,
                                0) != pdPASS) {
        Serial.println("[WIFI] task_start_failed");
        return;
    }
    if (xTaskCreatePinnedToCore(uploadTask, "upload", 8192, nullptr, 1,
                                nullptr, 0) != pdPASS) {
        Serial.println("[UPLOAD] task_start_failed");
        return;
    }
    if (xTaskCreatePinnedToCore(sensorTask, "sensor", 8192, nullptr, 2,
                                nullptr, 1) != pdPASS) {
        Serial.println("[SENSOR] task_start_failed");
    }
}

void loop() {
    vTaskDelay(pdMS_TO_TICKS(10));
}
