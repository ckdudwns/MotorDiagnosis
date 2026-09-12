#include <Arduino.h>
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

struct PeriodicSlot {
    bool initialized = false;
    bool hasValid = false;
    bool suppressed = false;
    uint64_t summaryAtUs = 0;
    uint32_t summarySequence = 0;
    uint32_t lastWindowIndex = 0;
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
    metadata.schemaVersion = 1;
    fillIdentity(metadata);
    metadata.windowIndex = index;
    metadata.windowMeasuredUptimeUs = measuredUptimeUs;
    metadata.startUptimeUs = startUptimeUs;
    metadata.sampleCount = pending.stats.sampleCount;
    metadata.featuresValid = pending.stats.featuresValid ? 1 : 0;
    copyText(metadata.quality, "valid");
    copyText(metadata.reason, "none");
    if (timestampValid) {
        metadata.windowMeasuredAtEpochUs = measuredEpochUs;
        metadata.summaryAtEpochUs = measuredEpochUs;
        metadata.windowMeasuredAtValid = 1;
        metadata.summaryAtValid = 1;
    }
}

void setInvalidReason(TelemetryMetadata& metadata, const char* reason) {
    copyText(metadata.quality, "invalid");
    copyText(metadata.reason, reason);
    metadata.featuresValid = 0;
    metadata.sampleCount = 0;
    metadata.windowMeasuredAtValid = 0;
}

const char* candidateType(CandidateReason reason) {
    return reason == CandidateReason::anomalyStart ? "anomaly_start"
           : reason == CandidateReason::anomalyActive ? "anomaly_active"
           : reason == CandidateReason::recovery ? "recovery"
                                                  : "periodic";
}

void resetPeriodicSlot(uint64_t summaryAtUs) {
    const uint32_t sequence = periodicSlot.summarySequence;
    periodicSlot = PeriodicSlot{};
    periodicSlot.initialized = true;
    periodicSlot.summaryAtUs = summaryAtUs;
    periodicSlot.summarySequence = sequence;
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
        metadata.schemaVersion = 1;
        fillIdentity(metadata);
        metadata.windowIndex = periodicSlot.lastWindowIndex;
        metadata.summaryAtEpochUs = periodicSlot.summaryAtUs;
        metadata.summaryAtValid = 1;
        setInvalidReason(metadata, periodicSlot.invalidReason);
        value.reason = CandidateReason::periodic;
    }

    value.window.metadata.summaryAtEpochUs = periodicSlot.summaryAtUs;
    value.window.metadata.summaryAtValid = 1;
    value.window.metadata.summarySequence = periodicSlot.summarySequence;
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
    while (periodicSlot.summaryAtUs < slotUs) {
        emitPeriodicSlot();
        ++periodicSlot.summarySequence;
        resetPeriodicSlot(periodicSlot.summaryAtUs + kPeriodicIntervalUs);
    }
}

void noteValidPeriodic(const TransmissionCandidate& value,
                       uint64_t measuredEpochUs,
                       const TransmissionPolicyDecision& decision) {
    advancePeriodicSlot(measuredEpochUs);
    periodicSlot.lastWindowIndex = value.window.metadata.windowIndex;
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
    periodicSlot.lastWindowIndex = windowIndex;
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

bool buildUploadPayload(const PendingWindow& pending, CandidateReason reason,
                        String& payload) {
    if (!pending.metadata.summaryAtValid ||
        (pending.metadata.featuresValid && !pending.stats.featuresValid)) {
        return false;
    }

    char summaryAt[40] = {};
    if (!formatTimestamp(pending.metadata.summaryAtEpochUs, summaryAt)) {
        return false;
    }
    payload.reserve(900);
    payload = "{\"schemaVersion\":";
    payload += String(pending.metadata.schemaVersion);
    payload += ",\"siteId\":\"";
    payload += pending.metadata.siteId;
    payload += "\",\"assetId\":\"";
    payload += pending.metadata.assetId;
    payload += "\",\"deviceId\":\"";
    payload += pending.metadata.deviceId;
    payload += "\",\"sensorId\":\"";
    payload += pending.metadata.sensorId;
    payload += "\",\"bootId\":\"";
    payload += pending.metadata.bootId;
    payload += "\",\"windowIndex\":";
    payload += String(pending.metadata.windowIndex);
    payload += ",\"recordType\":\"";
    payload += candidateType(reason);
    payload += "\",\"summaryAt\":\"";
    payload += summaryAt;
    payload += "\"";
    if (reason == CandidateReason::periodic) {
        payload += ",\"summarySequence\":";
        payload += String(pending.metadata.summarySequence);
    }
    payload += ",\"windowMeasuredAt\":";
    if (pending.metadata.windowMeasuredAtValid) {
        char measuredAt[40] = {};
        if (!formatTimestamp(pending.metadata.windowMeasuredAtEpochUs,
                             measuredAt)) {
            return false;
        }
        payload += "\"";
        payload += measuredAt;
        payload += "\"";
    } else {
        payload += "null";
    }
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
    if (!pending.metadata.featuresValid) {
        payload += "null";
    } else {
        const VibrationFeatures& f = pending.stats.features;
        payload += "{\"cf_a_1\":";
        payload += String(f.cfA1, 6);
        payload += ",\"cf_a_2\":";
        payload += String(f.cfA2, 6);
        payload += ",\"cf_a_3\":";
        payload += String(f.cfA3, 6);
        payload += ",\"sk_a_1\":";
        payload += String(f.skewnessA1, 6);
        payload += ",\"sk_a_2\":";
        payload += String(f.skewnessA2, 6);
        payload += ",\"sk_a_3\":";
        payload += String(f.skewnessA3, 6);
        payload += ",\"ku_a_1\":";
        payload += String(f.kurtosisA1, 6);
        payload += ",\"ku_a_2\":";
        payload += String(f.kurtosisA2, 6);
        payload += ",\"ku_a_3\":";
        payload += String(f.kurtosisA3, 6);
        payload += "}";
    }
    payload += "}";
    return true;
}

bool ackMatches(const String& response, const PendingWindow& pending,
                CandidateReason reason) {
    const int arrayStart = response.indexOf("\"acknowledged\"");
    if (arrayStart < 0) {
        return false;
    }
    const int arrayEnd = response.indexOf(']', arrayStart);
    const int objectStart = response.indexOf('{', arrayStart);
    if (arrayEnd < 0 || objectStart < 0 || objectStart > arrayEnd) {
        return false;
    }

    const int bootKey = response.indexOf("\"bootId\"", objectStart);
    const bool periodic = reason == CandidateReason::periodic;
    const int indexKey = response.indexOf(
        periodic ? "\"summarySequence\"" : "\"windowIndex\"",
        objectStart);
    const int objectEnd = response.indexOf('}', objectStart);
    if (bootKey < objectStart || bootKey > objectEnd ||
        indexKey < objectStart || indexKey > objectEnd) {
        return false;
    }

    const int bootColon = response.indexOf(':', bootKey);
    const int bootStart = response.indexOf('"', bootColon + 1);
    const int bootEnd = response.indexOf('"', bootStart + 1);
    if (bootColon < 0 || bootStart < 0 || bootEnd < 0 ||
        bootEnd > objectEnd) {
        return false;
    }
    const size_t bootLength = static_cast<size_t>(bootEnd - bootStart - 1);
    if (bootLength != strlen(pending.metadata.bootId) ||
        strncmp(response.c_str() + bootStart + 1, pending.metadata.bootId,
                bootLength) != 0) {
        return false;
    }

    const int indexColon = response.indexOf(':', indexKey);
    if (indexColon < 0) {
        return false;
    }
    int digit = indexColon + 1;
    while (digit < objectEnd && response.charAt(digit) == ' ') {
        ++digit;
    }
    if (digit >= objectEnd || response.charAt(digit) < '0' ||
        response.charAt(digit) > '9') {
        return false;
    }

    uint32_t windowIndex = 0;
    while (digit < objectEnd && response.charAt(digit) >= '0' &&
           response.charAt(digit) <= '9') {
        const uint32_t nextValue =
            windowIndex * 10U +
            static_cast<uint32_t>(response.charAt(digit) - '0');
        if (nextValue < windowIndex) {
            return false;
        }
        windowIndex = nextValue;
        ++digit;
    }
    return windowIndex == (periodic ? pending.metadata.summarySequence
                                    : pending.metadata.windowIndex);
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
    pendingNtpCandidate.window.metadata.summaryAtEpochUs = timestampEpochUs;
    pendingNtpCandidate.window.metadata.windowMeasuredAtValid = 1;
    pendingNtpCandidate.window.metadata.summaryAtValid = 1;
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

void setCandidateReason(TelemetryMetadata& metadata, CandidateReason reason) {
    if (reason == CandidateReason::anomalyStart) {
        copyText(metadata.reason,
                 "resultant_rms_or_strongest_peak_threshold");
    } else if (reason == CandidateReason::anomalyActive) {
        copyText(metadata.reason, "anomaly_active");
    } else if (reason == CandidateReason::recovery) {
        copyText(metadata.reason, "recovery");
    } else {
        copyText(metadata.reason, "none");
    }
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
        if (!buildUploadPayload(*psramUploadWindow, retryFile.reason,
                                payload)) {
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
            (statusCode == 200 || statusCode == 202) &&
            ackMatches(response, *psramUploadWindow, retryFile.reason);
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
        uint64_t captureEndEpochUs = 0;
        const bool captureEndTimeValid =
            wifiTime.utcAtUptimeUs(captureEndUs, captureEndEpochUs);

        if (!captureResult.ok()) {
            printCaptureResult(captureResult, window);
            TransmissionPolicyInput policyInput;
            policyInput.valid = false;
            policyInput.utcValid = captureEndTimeValid;
            policyInput.utcUs = captureEndEpochUs;
            policyInput.uptimeUs = captureEndUs;
            const TransmissionPolicyDecision policyDecision =
                transmissionPolicy.evaluate(policyInput);
            printPolicyDecision(policyDecision, windowIndex);
            if (captureEndTimeValid) {
                noteInvalidPeriodic(captureEndEpochUs, windowIndex,
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
                               captureEndUs, captureEndEpochUs,
                               captureEndTimeValid);
        TransmissionPolicyInput policyInput;
        policyInput.valid = true;
        policyInput.stats = stats;
        policyInput.utcValid = captureEndTimeValid;
        policyInput.utcUs = captureEndEpochUs;
        policyInput.uptimeUs = captureEndUs;
        const TransmissionPolicyDecision policyDecision =
            transmissionPolicy.evaluate(policyInput);

        printCaptureResult(captureResult, window);
        printPolicyDecision(policyDecision, windowIndex);
        if (captureEndTimeValid) {
            noteValidPeriodic(candidate, captureEndEpochUs, policyDecision);
        }
        flushPendingNtpCandidate();
        if (policyDecision.action != TransmissionPolicyAction::discard) {
            candidate.reason =
                candidateReasonFromAction(policyDecision.action);
            setCandidateReason(candidate.window.metadata, candidate.reason);
            if (!candidate.window.metadata.summaryAtValid) {
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
