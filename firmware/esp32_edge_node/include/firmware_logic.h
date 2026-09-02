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

enum class RingReadClass
{
    VALID,
    CORRUPT,
    IO_ERROR
};

enum class RingRecordTimeEncoding
{
    RESOLVED,
    LEGACY_UNRESOLVED_V1,
    PACKED_UNRESOLVED,
    INVALID
};

enum class RingRecoveryDisposition
{
    ACTIVE,
    LEGACY_ISOLATION,
    CORRUPT
};

enum class OfflineTimestampResult
{
    RESOLVED,
    WAITING_FOR_ANCHOR,
    SESSION_MISMATCH,
    INVALID_METADATA,
    OUT_OF_RANGE
};

struct CanonicalTelemetry
{
    std::uint32_t sequence = 0;
    float vibrationRmsRaw = 0.0f;
    float vibrationPeakHz = 0.0f;
    float acousticRmsRaw = 0.0f;
    float acousticPeakHz = 0.0f;
};

struct TimeAnchor
{
    std::uint32_t sessionId = 0;
    std::uint32_t monotonicMs = 0;
    std::int64_t epochMs = 0;
};

#pragma pack(push, 1)
struct DurableTimeAnchorBlob
{
    std::uint32_t magic = 0;
    std::uint16_t version = 0;
    std::uint16_t reserved = 0;
    std::uint32_t sessionId = 0;
    std::uint32_t monotonicMs = 0;
    std::int64_t epochMs = 0;
    std::uint32_t crc32 = 0;
};
#pragma pack(pop)

static_assert(
    sizeof(DurableTimeAnchorBlob) == 28,
    "DurableTimeAnchorBlob must remain 28 bytes."
);

AckValidationResult validateAckJson(
    int statusCode,
    const char* responseJson,
    const char* expectedDeviceId,
    std::uint32_t expectedSequence
);

bool parseHealthTimestampJson(
    const char* responseJson,
    std::int64_t& epochMs
);

bool parseRfc3339ToEpochMs(
    const char* value,
    std::int64_t& epochMs
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

bool shouldConsumeAfterReadFailure(
    RingReadClass readClass
);

RingRecordTimeEncoding classifyRingRecordTimeEncoding(
    std::uint16_t schemaVersion,
    std::uint16_t flags,
    std::uint64_t epochSeconds,
    std::uint16_t legacySchemaVersion,
    std::uint16_t currentSchemaVersion,
    std::uint16_t unresolvedFlag,
    std::uint64_t minimumResolvedEpochSeconds
);

RingRecoveryDisposition classifyRingRecordForRecovery(
    std::uint16_t schemaVersion,
    std::uint16_t flags,
    std::uint64_t epochSeconds,
    bool crcMatches,
    bool measurementsFinite,
    std::uint16_t legacySchemaVersion,
    std::uint16_t currentSchemaVersion,
    std::uint16_t unresolvedFlag,
    std::uint64_t minimumResolvedEpochSeconds
);

std::uint64_t packOfflineCaptureMetadata(
    std::uint32_t sessionId,
    std::uint32_t captureMonotonicMs
);

bool unpackOfflineCaptureMetadata(
    std::uint64_t packed,
    std::uint32_t& sessionId,
    std::uint32_t& captureMonotonicMs
);

OfflineTimestampResult resolveOfflineTimestampMs(
    std::uint32_t recordSessionId,
    std::uint32_t captureMonotonicMs,
    const TimeAnchor* anchor,
    std::int64_t& epochMs
);

std::uint32_t calculateDurableTimeAnchorCrc(
    const DurableTimeAnchorBlob& blob
);

void finalizeDurableTimeAnchor(
    DurableTimeAnchorBlob& blob
);

bool isDurableTimeAnchorValid(
    const DurableTimeAnchorBlob& blob,
    std::uint32_t expectedMagic,
    std::uint16_t expectedVersion,
    std::int64_t minimumEpochMs
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

std::uint64_t calculateRecoveryHeadOrdinal(
    std::uint64_t minimumActiveOrdinal,
    std::uint64_t maximumActiveOrdinal,
    std::size_t capacity
);

std::uint64_t calculateNextRingOrdinal(
    std::uint64_t maximumOrdinalSeen
);

bool shouldCommitWatermark(
    std::size_t pendingConsumedCount,
    std::size_t batchSize,
    bool forceCommit
);

std::size_t calculateLogicalQueueCapacity(
    std::size_t physicalRingSlots
);

bool shouldDropOldestAfterVerifiedWrite(
    bool queueWasFull,
    bool newRecordWriteVerified
);

bool shouldEvictArchiveEntry(
    std::size_t currentEntryCount,
    std::size_t maximumEntryCount
);

std::size_t calculateArchiveEvictionCount(
    std::size_t currentEntryCount,
    std::size_t maximumEntryCount,
    std::size_t maximumEvictionsPerPass
);

bool shouldAttemptRejectedArchiveAfterDurableRingWrite(
    bool ringWriteSucceeded
);

bool shouldConsumeRejectedRingAfterArchive(
    bool ringWriteSucceeded,
    bool archiveWriteSucceeded
);

bool isBackendTransportAllowed(
    const char* url,
    bool allowInsecureHttpForLocalDev
);

} // namespace FirmwareLogic
