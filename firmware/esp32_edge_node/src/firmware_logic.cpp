#include "firmware_logic.h"

#include <algorithm>
#include <cctype>
#include <cstdio>
#include <cstring>
#include <limits>
#include <string>

namespace FirmwareLogic
{
namespace
{

std::size_t skipWhitespace(
    const std::string& json,
    std::size_t index
)
{
    while (
        index < json.size() &&
        std::isspace(
            static_cast<unsigned char>(
                json[index]
            )
        )
    )
    {
        ++index;
    }

    return index;
}

bool hasJsonTerminator(
    const std::string& json,
    std::size_t index
)
{
    index =
        skipWhitespace(
            json,
            index
        );

    return (
        index >= json.size() ||
        json[index] == ',' ||
        json[index] == '}'
    );
}

bool findValueStart(
    const std::string& json,
    const char* key,
    std::size_t& valueStart
)
{
    if (
        key == nullptr ||
        *key == '\0'
    )
    {
        return false;
    }

    const std::string quotedKey =
        "\"" +
        std::string(key) +
        "\"";

    const std::size_t keyPosition =
        json.find(
            quotedKey
        );

    if (
        keyPosition ==
        std::string::npos
    )
    {
        return false;
    }

    std::size_t cursor =
        keyPosition +
        quotedKey.size();

    cursor =
        skipWhitespace(
            json,
            cursor
        );

    if (
        cursor >= json.size() ||
        json[cursor] != ':'
    )
    {
        return false;
    }

    cursor =
        skipWhitespace(
            json,
            cursor + 1
        );

    if (
        cursor >= json.size()
    )
    {
        return false;
    }

    valueStart =
        cursor;

    return true;
}

bool extractBool(
    const std::string& json,
    const char* key,
    bool& value
)
{
    std::size_t start =
        0;

    if (
        !findValueStart(
            json,
            key,
            start
        )
    )
    {
        return false;
    }

    if (
        json.compare(
            start,
            4,
            "true"
        ) == 0 &&
        hasJsonTerminator(
            json,
            start + 4
        )
    )
    {
        value =
            true;

        return true;
    }

    if (
        json.compare(
            start,
            5,
            "false"
        ) == 0 &&
        hasJsonTerminator(
            json,
            start + 5
        )
    )
    {
        value =
            false;

        return true;
    }

    return false;
}

bool extractString(
    const std::string& json,
    const char* key,
    std::string& value
)
{
    std::size_t start =
        0;

    if (
        !findValueStart(
            json,
            key,
            start
        ) ||
        json[start] != '"'
    )
    {
        return false;
    }

    std::string parsed;

    for (
        std::size_t cursor = start + 1;
        cursor < json.size();
        ++cursor
    )
    {
        const char c =
            json[cursor];

        if (
            c == '"'
        )
        {
            if (
                !hasJsonTerminator(
                    json,
                    cursor + 1
                )
            )
            {
                return false;
            }

            value =
                parsed;

            return true;
        }

        // deviceId is an identifier. Reject escapes instead of silently
        // normalizing an ambiguous identity string.
        if (
            c == '\\'
        )
        {
            return false;
        }

        parsed +=
            c;
    }

    return false;
}

bool extractUint32(
    const std::string& json,
    const char* key,
    std::uint32_t& value
)
{
    std::size_t start =
        0;

    if (
        !findValueStart(
            json,
            key,
            start
        )
    )
    {
        return false;
    }

    if (
        start >= json.size() ||
        json[start] < '0' ||
        json[start] > '9'
    )
    {
        return false;
    }

    std::uint64_t parsed =
        0;

    std::size_t cursor =
        start;

    while (
        cursor < json.size() &&
        json[cursor] >= '0' &&
        json[cursor] <= '9'
    )
    {
        parsed =
            parsed * 10ULL +
            static_cast<std::uint64_t>(
                json[cursor] -
                '0'
            );

        if (
            parsed >
            std::numeric_limits<std::uint32_t>::max()
        )
        {
            return false;
        }

        ++cursor;
    }

    if (
        !hasJsonTerminator(
            json,
            cursor
        )
    )
    {
        return false;
    }

    value =
        static_cast<std::uint32_t>(
            parsed
        );

    return true;
}

} // namespace

AckValidationResult validateAckJson(
    int statusCode,
    const char* responseJson,
    const char* expectedDeviceId,
    std::uint32_t expectedSequence
)
{
    if (
        statusCode != 200 &&
        statusCode != 201
    )
    {
        return AckValidationResult::UNSUPPORTED_STATUS;
    }

    if (
        responseJson == nullptr ||
        expectedDeviceId == nullptr
    )
    {
        return AckValidationResult::MALFORMED_RESPONSE;
    }

    const std::string response(
        responseJson
    );

    bool accepted =
        false;

    bool duplicate =
        false;

    std::string deviceId;

    std::uint32_t sequence =
        0;

    if (
        !extractBool(
            response,
            "accepted",
            accepted
        ) ||
        !extractBool(
            response,
            "duplicate",
            duplicate
        ) ||
        !extractString(
            response,
            "deviceId",
            deviceId
        ) ||
        !extractUint32(
            response,
            "sequence",
            sequence
        )
    )
    {
        return AckValidationResult::MALFORMED_RESPONSE;
    }

    if (
        !accepted
    )
    {
        return AckValidationResult::NOT_ACCEPTED;
    }

    if (
        deviceId !=
        expectedDeviceId
    )
    {
        return AckValidationResult::DEVICE_MISMATCH;
    }

    if (
        sequence !=
        expectedSequence
    )
    {
        return AckValidationResult::SEQUENCE_MISMATCH;
    }

    if (
        (
            statusCode == 201 &&
            duplicate
        ) ||
        (
            statusCode == 200 &&
            !duplicate
        )
    )
    {
        return AckValidationResult::DUPLICATE_CONTRACT_MISMATCH;
    }

    return AckValidationResult::VALID;
}

float canonicalizeMeasurement(
    double value
)
{
    return static_cast<float>(
        value
    );
}

std::string buildCanonicalTelemetryPayload(
    const char* timestamp,
    const char* siteId,
    const char* assetId,
    const char* deviceId,
    const CanonicalTelemetry& telemetry
)
{
    if (
        timestamp == nullptr ||
        siteId == nullptr ||
        assetId == nullptr ||
        deviceId == nullptr ||
        *timestamp == '\0'
    )
    {
        return {};
    }

    char buffer[1024];

    const int written =
        std::snprintf(
            buffer,
            sizeof(buffer),
            "{"
            "\"timestamp\":\"%s\","
            "\"sequence\":%lu,"
            "\"siteId\":\"%s\","
            "\"assetId\":\"%s\","
            "\"deviceId\":\"%s\","
            "\"rpm\":null,"
            "\"vibrationRmsRaw\":%.6f,"
            "\"vibrationRmsMmS\":null,"
            "\"vibrationPeakHz\":%.2f,"
            "\"acousticRmsRaw\":%.2f,"
            "\"acousticDb\":null,"
            "\"acousticPeakHz\":%.2f,"
            "\"scenarioLabel\":null,"
            "\"knownVibrationLabel\":null,"
            "\"knownAcousticLabel\":null,"
            "\"source\":\"esp32-s3\","
            "\"isSynthetic\":false,"
            "\"vibrationUnitNote\":\"ADXL345 acceleration RMS in g\","
            "\"acousticUnitNote\":\"INMP441 raw PCM RMS, uncalibrated\""
            "}",
            timestamp,
            static_cast<unsigned long>(
                telemetry.sequence
            ),
            siteId,
            assetId,
            deviceId,
            static_cast<double>(
                telemetry.vibrationRmsRaw
            ),
            static_cast<double>(
                telemetry.vibrationPeakHz
            ),
            static_cast<double>(
                telemetry.acousticRmsRaw
            ),
            static_cast<double>(
                telemetry.acousticPeakHz
            )
        );

    if (
        written < 0 ||
        static_cast<std::size_t>(
            written
        ) >= sizeof(buffer)
    )
    {
        return {};
    }

    return std::string(
        buffer,
        static_cast<std::size_t>(
            written
        )
    );
}

std::size_t calculateReplayBatchSize(
    std::size_t queuedRecords,
    std::size_t maxRecordsPerLoop
)
{
    return std::min(
        queuedRecords,
        maxRecordsPerLoop
    );
}

bool shouldAdvanceSequence(
    bool nvsWriteSucceeded,
    bool nvsReadbackSucceeded,
    std::uint32_t writtenSequence,
    std::uint32_t readbackSequence
)
{
    return (
        nvsWriteSucceeded &&
        nvsReadbackSucceeded &&
        writtenSequence ==
            readbackSequence
    );
}

std::int64_t resolveTimestampMs(
    std::uint64_t recordOrdinal,
    std::uint64_t anchorOrdinal,
    std::int64_t anchorEpochMs,
    std::uint32_t acquisitionIntervalMs
)
{
    if (
        anchorEpochMs <= 0 ||
        acquisitionIntervalMs == 0
    )
    {
        return -1;
    }

    const std::uint64_t intervalMs =
        static_cast<std::uint64_t>(
            acquisitionIntervalMs
        );

    if (
        recordOrdinal >=
        anchorOrdinal
    )
    {
        const std::uint64_t delta =
            recordOrdinal -
            anchorOrdinal;

        const std::uint64_t available =
            static_cast<std::uint64_t>(
                std::numeric_limits<std::int64_t>::max() -
                anchorEpochMs
            );

        if (
            delta >
            available /
                intervalMs
        )
        {
            return -1;
        }

        return (
            anchorEpochMs +
            static_cast<std::int64_t>(
                delta *
                intervalMs
            )
        );
    }

    const std::uint64_t delta =
        anchorOrdinal -
        recordOrdinal;

    const std::uint64_t anchorMagnitude =
        static_cast<std::uint64_t>(
            anchorEpochMs
        );

    if (
        delta >
        anchorMagnitude /
            intervalMs
    )
    {
        return -1;
    }

    return (
        anchorEpochMs -
        static_cast<std::int64_t>(
            delta *
            intervalMs
        )
    );
}

bool shouldIgnoreRecoveredOrdinal(
    std::uint64_t recordOrdinal,
    std::uint64_t committedConsumedOrdinal
)
{
    return (
        recordOrdinal <=
        committedConsumedOrdinal
    );
}

std::size_t calculateRecoveredQueueCount(
    std::uint64_t minimumActiveOrdinal,
    std::uint64_t maximumActiveOrdinal,
    std::size_t capacity,
    std::size_t activeValidCount
)
{
    if (
        activeValidCount == 0 ||
        capacity == 0 ||
        maximumActiveOrdinal <
            minimumActiveOrdinal
    )
    {
        return 0;
    }

    const std::uint64_t span =
        maximumActiveOrdinal -
        minimumActiveOrdinal +
        1ULL;

    return static_cast<std::size_t>(
        std::min<std::uint64_t>(
            span,
            capacity
        )
    );
}

std::uint64_t calculateNextRingOrdinal(
    std::uint64_t maximumOrdinalSeen
)
{
    if (
        maximumOrdinalSeen ==
        std::numeric_limits<std::uint64_t>::max()
    )
    {
        return 1;
    }

    const std::uint64_t next =
        maximumOrdinalSeen +
        1ULL;

    return (
        next == 0
            ? 1
            : next
    );
}

bool shouldCommitWatermark(
    std::size_t pendingConsumedCount,
    std::size_t batchSize,
    bool forceCommit
)
{
    if (
        forceCommit
    )
    {
        return true;
    }

    if (
        batchSize == 0
    )
    {
        return false;
    }

    return (
        pendingConsumedCount >=
        batchSize
    );
}

} // namespace FirmwareLogic
