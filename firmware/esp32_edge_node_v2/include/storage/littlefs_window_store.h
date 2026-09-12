#pragma once

#include <FS.h>

#include <cstddef>
#include <cstdint>

#include "sampling/vibration_window.h"

struct TelemetryMetadata {
    uint32_t schemaVersion = 1;
    char deviceId[24] = {};
    char siteId[24] = {};
    char assetId[32] = {};
    char sensorId[24] = {};
    char bootId[33] = {};
    uint32_t measuredWindowIndex = 0;
    uint32_t windowIndex = 0;
    uint64_t startUptimeUs = 0;
    uint64_t windowMeasuredUptimeUs = 0;
    uint64_t windowMeasuredAtEpochUs = 0;
    uint64_t periodicSlotEpochUs = 0;
    uint8_t windowMeasuredAtValid = 0;
    uint8_t periodicSlotEpochValid = 0;
    uint8_t featuresValid = 0;
    uint16_t sampleCount = 0;
    uint8_t anomalyCount = 0;
    uint8_t normalCount = 0;
    char quality[24] = {};
    char reason[48] = {};
};

struct PendingWindow {
    WindowStats stats;
    TelemetryMetadata metadata;
};

enum class CandidateReason : uint8_t {
    periodic,
    anomalyStart,
    anomalyActive,
    recovery,
};

struct TransmissionCandidate {
    PendingWindow window;
    CandidateReason reason = CandidateReason::periodic;
};

#pragma pack(push, 1)
struct FeatureRecordHeader {
    uint32_t magic;
    uint16_t version;
    uint16_t headerBytes;
    uint32_t recordBytes;
    TelemetryMetadata metadata;
    uint32_t featureBytes;
};
#pragma pack(pop)

struct RetryFileRef {
    char path[80] = {};
    CandidateReason reason = CandidateReason::periodic;
    uint32_t sequence = 0;
};

struct WindowWriteResult {
    bool ok = false;
    size_t bytes = 0;
    uint32_t writeUs = 0;
    size_t fsUsedBytes = 0;
    size_t fsFreeBytes = 0;
    char path[80] = {};
};

class LittleFsWindowStore {
public:
    bool begin();
    WindowWriteResult saveCandidate(const TransmissionCandidate& candidate);
    bool claimNextRetryFile(RetryFileRef& result);
    bool readCandidate(const RetryFileRef& file, PendingWindow& result);
    bool removeCandidate(const RetryFileRef& file);
    void releaseCandidate(const RetryFileRef& file);

private:
    void lock();
    void unlock();
    bool evictOldest(CandidateReason reason);
    bool evictOldestCompletedPair();

    void* mutex_ = nullptr;
    uint32_t nextSequence_ = 1;
    char inFlightPath_[80] = {};
};
