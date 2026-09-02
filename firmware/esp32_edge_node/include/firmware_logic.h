#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace FirmwareLogic
{

enum class AckValidationResult
{
    VALID,
    UNSUPPORTED_STATUS,
    MALFORMED_RESPONSE,
    NOT_ACCEPTED,
    DEVICE_MISMATCH,
    SEQUENCE_MISMATCH,
    DUPLICATE_CONTRACT_MISMATCH
};

struct CanonicalTelemetry
{
    std::uint32_t sequence = 0;
    float vibrationRmsRaw = 0.0f;
    float vibrationPeakHz = 0.0f;
    float acousticRmsRaw = 0.0f;
    float acousticPeakHz = 0.0f;
};

AckValidationResult validateAckJson(
    int statusCode,
    const char* responseJson,
    const char* expectedDeviceId,
    std::uint32_t expectedSequence
);

float canonicalizeMeasurement(
    double value
);

std::string buildCanonicalTelemetryPayload(
    const char* timestamp,
    const char* siteId,
    const char* assetId,
    const char* deviceId,
    const CanonicalTelemetry& telemetry
);

std::size_t calculateReplayBatchSize(
    std::size_t queuedRecords,
    std::size_t maxRecordsPerLoop
);

bool shouldAdvanceSequence(
    bool nvsWriteSucceeded,
    bool nvsReadbackSucceeded,
    std::uint32_t writtenSequence,
    std::uint32_t readbackSequence
);

std::int64_t resolveTimestampMs(
    std::uint64_t recordOrdinal,
    std::uint64_t anchorOrdinal,
    std::int64_t anchorEpochMs,
    std::uint32_t acquisitionIntervalMs
);

bool shouldIgnoreRecoveredOrdinal(
    std::uint64_t recordOrdinal,
    std::uint64_t committedConsumedOrdinal
);

std::size_t calculateRecoveredQueueCount(
    std::uint64_t minimumActiveOrdinal,
    std::uint64_t maximumActiveOrdinal,
    std::size_t capacity,
    std::size_t activeValidCount
);

std::uint64_t calculateNextRingOrdinal(
    std::uint64_t maximumOrdinalSeen
);

bool shouldCommitWatermark(
    std::size_t pendingConsumedCount,
    std::size_t batchSize,
    bool forceCommit
);

} // namespace FirmwareLogic
