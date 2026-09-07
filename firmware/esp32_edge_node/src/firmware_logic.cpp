#include "firmware_logic.h"

#include <ArduinoJson.h>
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

enum class PrimitiveType
{
    STRING,
    BOOLEAN,
    NUMBER,
    NULL_VALUE,
    NESTED
};

struct PrimitiveValue
{
    PrimitiveType type = PrimitiveType::NULL_VALUE;
    std::string text;
    bool booleanValue = false;
};

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

bool parseJsonString(
    const std::string& json,
    std::size_t& cursor,
    std::string& value
)
{
    if (
        cursor >= json.size() ||
        json[cursor] != '"'
    )
    {
        return false;
    }

    ++cursor;
    value.clear();

    while (
        cursor < json.size()
    )
    {
        const unsigned char c =
            static_cast<unsigned char>(
                json[cursor++]
            );

        if (
            c == '"'
        )
        {
            return true;
        }

        if (
            c < 0x20
        )
        {
            return false;
        }

        if (
            c != '\\'
        )
        {
            value +=
                static_cast<char>(c);

            continue;
        }

        if (
            cursor >= json.size()
        )
        {
            return false;
        }

        const char escaped =
            json[cursor++];

        switch (escaped)
        {
            case '"':
            case '\\':
            case '/':
                value += escaped;
                break;

            case 'b':
                value += '\b';
                break;

            case 'f':
                value += '\f';
                break;

            case 'n':
                value += '\n';
                break;

            case 'r':
                value += '\r';
                break;

            case 't':
                value += '\t';
                break;

            // ACK/device identifiers and health timestamps never require
            // JSON unicode escapes. Reject them rather than implementing a
            // partial Unicode decoder that could normalize identities.
            case 'u':
            default:
                return false;
        }
    }

    return false;
}

bool parseNumberToken(
    const std::string& json,
    std::size_t& cursor,
    std::string& token
)
{
    const std::size_t start =
        cursor;

    if (
        cursor < json.size() &&
        json[cursor] == '-'
    )
    {
        ++cursor;
    }

    if (
        cursor >= json.size()
    )
    {
        return false;
    }

    if (
        json[cursor] == '0'
    )
    {
        ++cursor;

        if (
            cursor < json.size() &&
            std::isdigit(
                static_cast<unsigned char>(
                    json[cursor]
                )
            )
        )
        {
            return false;
        }
    }
    else
    {
        if (
            !std::isdigit(
                static_cast<unsigned char>(
                    json[cursor]
                )
            )
        )
        {
            return false;
        }

        while (
            cursor < json.size() &&
            std::isdigit(
                static_cast<unsigned char>(
                    json[cursor]
                )
            )
        )
        {
            ++cursor;
        }
    }

    if (
        cursor < json.size() &&
        json[cursor] == '.'
    )
    {
        ++cursor;

        const std::size_t fractionStart =
            cursor;

        while (
            cursor < json.size() &&
            std::isdigit(
                static_cast<unsigned char>(
                    json[cursor]
                )
            )
        )
        {
            ++cursor;
        }

        if (
            cursor == fractionStart
        )
        {
            return false;
        }
    }

    if (
        cursor < json.size() &&
        (
            json[cursor] == 'e' ||
            json[cursor] == 'E'
        )
    )
    {
        ++cursor;

        if (
            cursor < json.size() &&
            (
                json[cursor] == '+' ||
                json[cursor] == '-'
            )
        )
        {
            ++cursor;
        }

        const std::size_t exponentStart =
            cursor;

        while (
            cursor < json.size() &&
            std::isdigit(
                static_cast<unsigned char>(
                    json[cursor]
                )
            )
        )
        {
            ++cursor;
        }

        if (
            cursor == exponentStart
        )
        {
            return false;
        }
    }

    token =
        json.substr(
            start,
            cursor - start
        );

    return true;
}

bool parsePrimitiveValue(
    const std::string& json,
    std::size_t& cursor,
    PrimitiveValue& value
)
{
    cursor =
        skipWhitespace(
            json,
            cursor
        );

    if (
        cursor >= json.size()
    )
    {
        return false;
    }

    if (
        json[cursor] == '{' ||
        json[cursor] == '['
    )
    {
        std::string nesting;
        nesting += json[cursor++];
        bool inString = false;
        bool escaped = false;

        while (cursor < json.size() && !nesting.empty())
        {
            const char c = json[cursor++];

            if (inString)
            {
                if (escaped)
                {
                    escaped = false;
                }
                else if (c == '\\')
                {
                    escaped = true;
                }
                else if (c == '"')
                {
                    inString = false;
                }

                continue;
            }

            if (c == '"')
            {
                inString = true;
            }
            else if (c == '{' || c == '[')
            {
                nesting += c;
            }
            else if (c == '}' || c == ']')
            {
                const char expected = nesting.back() == '{' ? '}' : ']';
                if (c != expected)
                {
                    return false;
                }
                nesting.pop_back();
            }
        }

        if (!nesting.empty() || inString)
        {
            return false;
        }

        value.type = PrimitiveType::NESTED;
        return true;
    }

    if (
        json[cursor] == '"'
    )
    {
        value.type =
            PrimitiveType::STRING;

        return parseJsonString(
            json,
            cursor,
            value.text
        );
    }

    if (
        json.compare(
            cursor,
            4,
            "true"
        ) == 0
    )
    {
        cursor += 4;
        value.type = PrimitiveType::BOOLEAN;
        value.booleanValue = true;
        value.text = "true";
        return true;
    }

    if (
        json.compare(
            cursor,
            5,
            "false"
        ) == 0
    )
    {
        cursor += 5;
        value.type = PrimitiveType::BOOLEAN;
        value.booleanValue = false;
        value.text = "false";
        return true;
    }

    if (
        json.compare(
            cursor,
            4,
            "null"
        ) == 0
    )
    {
        cursor += 4;
        value.type = PrimitiveType::NULL_VALUE;
        value.text = "null";
        return true;
    }

    value.type =
        PrimitiveType::NUMBER;

    return parseNumberToken(
        json,
        cursor,
        value.text
    );
}

template <typename Handler>
bool parseFlatTopLevelObject(
    const char* jsonText,
    Handler handler
)
{
    if (
        jsonText == nullptr
    )
    {
        return false;
    }

    const std::string json(
        jsonText
    );

    std::size_t cursor =
        skipWhitespace(
            json,
            0
        );

    if (
        cursor >= json.size() ||
        json[cursor] != '{'
    )
    {
        return false;
    }

    ++cursor;
    cursor = skipWhitespace(json, cursor);

    if (
        cursor < json.size() &&
        json[cursor] == '}'
    )
    {
        ++cursor;
        cursor = skipWhitespace(json, cursor);
        return cursor == json.size();
    }

    while (
        cursor < json.size()
    )
    {
        std::string key;

        if (
            !parseJsonString(
                json,
                cursor,
                key
            )
        )
        {
            return false;
        }

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

        ++cursor;

        PrimitiveValue value;

        if (
            !parsePrimitiveValue(
                json,
                cursor,
                value
            )
        )
        {
            return false;
        }

        if (
            !handler(
                key,
                value
            )
        )
        {
            return false;
        }

        cursor =
            skipWhitespace(
                json,
                cursor
            );

        if (
            cursor >= json.size()
        )
        {
            return false;
        }

        if (
            json[cursor] == ','
        )
        {
            ++cursor;
            cursor = skipWhitespace(json, cursor);

            if (
                cursor >= json.size() ||
                json[cursor] == '}'
            )
            {
                return false;
            }

            continue;
        }

        if (
            json[cursor] == '}'
        )
        {
            ++cursor;
            cursor = skipWhitespace(json, cursor);
            return cursor == json.size();
        }

        return false;
    }

    return false;
}

bool parseUint32Token(
    const PrimitiveValue& value,
    std::uint32_t& parsed
)
{
    if (
        value.type != PrimitiveType::NUMBER ||
        value.text.empty()
    )
    {
        return false;
    }

    std::uint64_t result =
        0;

    for (
        char c : value.text
    )
    {
        if (
            c < '0' ||
            c > '9'
        )
        {
            return false;
        }

        result =
            result * 10ULL +
            static_cast<std::uint64_t>(
                c - '0'
            );

        if (
            result >
            std::numeric_limits<std::uint32_t>::max()
        )
        {
            return false;
        }
    }

    parsed =
        static_cast<std::uint32_t>(
            result
        );

    return true;
}

bool isLeapYear(
    int year
)
{
    return (
        year % 4 == 0 &&
        (
            year % 100 != 0 ||
            year % 400 == 0
        )
    );
}

int daysInMonth(
    int year,
    int month
)
{
    static const int days[] =
    {
        31, 28, 31, 30, 31, 30,
        31, 31, 30, 31, 30, 31
    };

    if (
        month < 1 ||
        month > 12
    )
    {
        return 0;
    }

    if (
        month == 2 &&
        isLeapYear(year)
    )
    {
        return 29;
    }

    return days[month - 1];
}

std::int64_t daysFromCivil(
    int year,
    unsigned month,
    unsigned day
)
{
    year -=
        month <= 2;

    const int era =
        (
            year >= 0
                ? year
                : year - 399
        ) /
        400;

    const unsigned yearOfEra =
        static_cast<unsigned>(
            year - era * 400
        );

    const unsigned adjustedMonth =
        static_cast<unsigned>(
            static_cast<int>(month) +
            (
                month > 2
                    ? -3
                    : 9
            )
        );

    const unsigned dayOfYear =
        (
            153U * adjustedMonth +
            2U
        ) /
        5U +
        day -
        1U;

    const unsigned dayOfEra =
        yearOfEra * 365U +
        yearOfEra / 4U -
        yearOfEra / 100U +
        dayOfYear;

    return (
        static_cast<std::int64_t>(era) *
            146097LL +
        static_cast<std::int64_t>(dayOfEra) -
        719468LL
    );
}

bool parseFixedDigits(
    const std::string& value,
    std::size_t offset,
    std::size_t count,
    int& parsed
)
{
    if (
        offset + count > value.size()
    )
    {
        return false;
    }

    int result =
        0;

    for (
        std::size_t i = 0;
        i < count;
        ++i
    )
    {
        const char c =
            value[offset + i];

        if (
            c < '0' ||
            c > '9'
        )
        {
            return false;
        }

        result =
            result * 10 +
            (c - '0');
    }

    parsed =
        result;

    return true;
}

std::uint32_t crc32Bytes(
    const std::uint8_t* data,
    std::size_t length
)
{
    if (
        data == nullptr &&
        length != 0
    )
    {
        return 0;
    }

    std::uint32_t crc =
        0xFFFFFFFFUL;

    for (
        std::size_t i = 0;
        i < length;
        ++i
    )
    {
        crc ^=
            data[i];

        for (
            std::uint8_t bit = 0;
            bit < 8;
            ++bit
        )
        {
            const std::uint32_t mask =
                static_cast<std::uint32_t>(
                    -static_cast<std::int32_t>(
                        crc & 1U
                    )
                );

            crc =
                (crc >> 1U) ^
                (0xEDB88320UL & mask);
        }
    }

    return ~crc;
}

bool startsWith(
    const char* value,
    const char* prefix
)
{
    if (
        value == nullptr ||
        prefix == nullptr
    )
    {
        return false;
    }

    const std::size_t prefixLength =
        std::strlen(prefix);

    return (
        std::strncmp(
            value,
            prefix,
            prefixLength
        ) == 0
    );
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

    // Validate the complete response, including optional nested metadata.
    // The flat parser below extracts and type-checks the required ACK fields;
    // ArduinoJson prevents malformed optional values from being accepted.
    JsonDocument document;
    if (
        deserializeJson(
            document,
            responseJson
        )
    )
    {
        return AckValidationResult::MALFORMED_RESPONSE;
    }

    bool accepted = false;
    bool duplicate = false;
    std::string deviceId;
    std::uint32_t sequence = 0;

    bool acceptedSeen = false;
    bool duplicateSeen = false;
    bool deviceSeen = false;
    bool sequenceSeen = false;

    const bool parsed =
        parseFlatTopLevelObject(
            responseJson,
            [&](
                const std::string& key,
                const PrimitiveValue& value
            ) -> bool
            {
                if (
                    key == "accepted"
                )
                {
                    if (
                        acceptedSeen ||
                        value.type != PrimitiveType::BOOLEAN
                    )
                    {
                        return false;
                    }

                    acceptedSeen = true;
                    accepted = value.booleanValue;
                    return true;
                }

                if (
                    key == "duplicate"
                )
                {
                    if (
                        duplicateSeen ||
                        value.type != PrimitiveType::BOOLEAN
                    )
                    {
                        return false;
                    }

                    duplicateSeen = true;
                    duplicate = value.booleanValue;
                    return true;
                }

                if (
                    key == "deviceId"
                )
                {
                    if (
                        deviceSeen ||
                        value.type != PrimitiveType::STRING
                    )
                    {
                        return false;
                    }

                    deviceSeen = true;
                    deviceId = value.text;
                    return true;
                }

                if (
                    key == "sequence"
                )
                {
                    if (
                        sequenceSeen ||
                        !parseUint32Token(
                            value,
                            sequence
                        )
                    )
                    {
                        return false;
                    }

                    sequenceSeen = true;
                    return true;
                }

                // API may add extra primitive top-level metadata such as
                // receivedAt. Nested values are rejected by the parser.
                return true;
            }
        );

    if (
        !parsed ||
        !acceptedSeen ||
        !duplicateSeen ||
        !deviceSeen ||
        !sequenceSeen
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

bool parseHealthTimestampJson(
    const char* responseJson,
    std::int64_t& epochMs
)
{
    epochMs = 0;

    bool timestampSeen = false;
    std::string timestamp;

    const bool parsed =
        parseFlatTopLevelObject(
            responseJson,
            [&](
                const std::string& key,
                const PrimitiveValue& value
            ) -> bool
            {
                if (
                    key != "timestamp"
                )
                {
                    return true;
                }

                if (
                    timestampSeen ||
                    value.type != PrimitiveType::STRING
                )
                {
                    return false;
                }

                timestampSeen = true;
                timestamp = value.text;
                return true;
            }
        );

    if (
        !parsed ||
        !timestampSeen
    )
    {
        return false;
    }

    return parseRfc3339ToEpochMs(
        timestamp.c_str(),
        epochMs
    );
}

bool parseRfc3339ToEpochMs(
    const char* input,
    std::int64_t& epochMs
)
{
    epochMs = 0;

    if (
        input == nullptr
    )
    {
        return false;
    }

    const std::string value(
        input
    );

    if (
        value.size() < 20
    )
    {
        return false;
    }

    int year = 0;
    int month = 0;
    int day = 0;
    int hour = 0;
    int minute = 0;
    int second = 0;

    if (
        !parseFixedDigits(value, 0, 4, year) ||
        value[4] != '-' ||
        !parseFixedDigits(value, 5, 2, month) ||
        value[7] != '-' ||
        !parseFixedDigits(value, 8, 2, day) ||
        (
            value[10] != 'T' &&
            value[10] != 't'
        ) ||
        !parseFixedDigits(value, 11, 2, hour) ||
        value[13] != ':' ||
        !parseFixedDigits(value, 14, 2, minute) ||
        value[16] != ':' ||
        !parseFixedDigits(value, 17, 2, second)
    )
    {
        return false;
    }

    if (
        month < 1 ||
        month > 12 ||
        day < 1 ||
        day > daysInMonth(year, month) ||
        hour < 0 ||
        hour > 23 ||
        minute < 0 ||
        minute > 59 ||
        second < 0 ||
        second > 59
    )
    {
        return false;
    }

    std::size_t cursor =
        19;

    int milliseconds =
        0;

    if (
        cursor < value.size() &&
        value[cursor] == '.'
    )
    {
        ++cursor;

        const std::size_t fractionStart =
            cursor;

        int digitsUsed =
            0;

        while (
            cursor < value.size() &&
            std::isdigit(
                static_cast<unsigned char>(
                    value[cursor]
                )
            )
        )
        {
            if (
                digitsUsed < 3
            )
            {
                milliseconds =
                    milliseconds * 10 +
                    (value[cursor] - '0');

                ++digitsUsed;
            }

            ++cursor;
        }

        if (
            cursor == fractionStart
        )
        {
            return false;
        }

        while (
            digitsUsed < 3
        )
        {
            milliseconds *= 10;
            ++digitsUsed;
        }
    }

    int offsetSeconds =
        0;

    if (
        cursor >= value.size()
    )
    {
        return false;
    }

    if (
        value[cursor] == 'Z' ||
        value[cursor] == 'z'
    )
    {
        ++cursor;
    }
    else if (
        value[cursor] == '+' ||
        value[cursor] == '-'
    )
    {
        const int sign =
            value[cursor] == '+'
                ? 1
                : -1;

        ++cursor;

        int offsetHour = 0;
        int offsetMinute = 0;

        if (
            !parseFixedDigits(value, cursor, 2, offsetHour)
        )
        {
            return false;
        }

        cursor += 2;

        if (
            cursor >= value.size() ||
            value[cursor] != ':'
        )
        {
            return false;
        }

        ++cursor;

        if (
            !parseFixedDigits(value, cursor, 2, offsetMinute)
        )
        {
            return false;
        }

        cursor += 2;

        if (
            offsetHour > 23 ||
            offsetMinute > 59
        )
        {
            return false;
        }

        offsetSeconds =
            sign *
            (
                offsetHour * 3600 +
                offsetMinute * 60
            );
    }
    else
    {
        return false;
    }

    if (
        cursor != value.size()
    )
    {
        return false;
    }

    const std::int64_t days =
        daysFromCivil(
            year,
            static_cast<unsigned>(month),
            static_cast<unsigned>(day)
        );

    const std::int64_t secondsSinceEpoch =
        days * 86400LL +
        hour * 3600LL +
        minute * 60LL +
        second -
        offsetSeconds;

    if (
        secondsSinceEpoch < 0 ||
        secondsSinceEpoch >
            (
                std::numeric_limits<std::int64_t>::max() -
                milliseconds
            ) /
            1000LL
    )
    {
        return false;
    }

    epochMs =
        secondsSinceEpoch * 1000LL +
        milliseconds;

    return true;
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

bool shouldConsumeAfterReadFailure(
    RingReadClass readClass
)
{
    return (
        readClass ==
        RingReadClass::CORRUPT
    );
}

RingRecordTimeEncoding classifyRingRecordTimeEncoding(
    std::uint16_t schemaVersion,
    std::uint16_t flags,
    std::uint64_t epochSeconds,
    std::uint16_t legacySchemaVersion,
    std::uint16_t currentSchemaVersion,
    std::uint16_t unresolvedFlag,
    std::uint64_t minimumResolvedEpochSeconds
)
{
    if (
        schemaVersion != legacySchemaVersion &&
        schemaVersion != currentSchemaVersion
    )
    {
        return RingRecordTimeEncoding::INVALID;
    }

    if (
        (
            flags &
            static_cast<std::uint16_t>(
                ~unresolvedFlag
            )
        ) != 0
    )
    {
        return RingRecordTimeEncoding::INVALID;
    }

    if (
        (
            flags &
            unresolvedFlag
        ) != 0
    )
    {
        // PR #13 firmware before the session/monotonic migration used the
        // same schema version but persisted unresolved records as epoch=0.
        // Treat that exact legacy encoding as migratable data, not damage.
        if (
            schemaVersion == legacySchemaVersion &&
            epochSeconds == 0
        )
        {
            return RingRecordTimeEncoding::LEGACY_UNRESOLVED_V1;
        }

        std::uint32_t sessionId = 0;
        std::uint32_t captureMonotonicMs = 0;

        if (
            !unpackOfflineCaptureMetadata(
                epochSeconds,
                sessionId,
                captureMonotonicMs
            )
        )
        {
            return RingRecordTimeEncoding::INVALID;
        }

        return RingRecordTimeEncoding::PACKED_UNRESOLVED;
    }

    if (
        epochSeconds <
        minimumResolvedEpochSeconds
    )
    {
        return RingRecordTimeEncoding::INVALID;
    }

    return RingRecordTimeEncoding::RESOLVED;
}

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
)
{
    if (
        !crcMatches ||
        !measurementsFinite
    )
    {
        return RingRecoveryDisposition::CORRUPT;
    }

    const RingRecordTimeEncoding encoding =
        classifyRingRecordTimeEncoding(
            schemaVersion,
            flags,
            epochSeconds,
            legacySchemaVersion,
            currentSchemaVersion,
            unresolvedFlag,
            minimumResolvedEpochSeconds
        );

    if (
        encoding ==
        RingRecordTimeEncoding::INVALID
    )
    {
        return RingRecoveryDisposition::CORRUPT;
    }

    if (
        encoding ==
        RingRecordTimeEncoding::LEGACY_UNRESOLVED_V1
    )
    {
        return RingRecoveryDisposition::LEGACY_ISOLATION;
    }

    return RingRecoveryDisposition::ACTIVE;
}

std::uint64_t packOfflineCaptureMetadata(
    std::uint32_t sessionId,
    std::uint32_t captureMonotonicMs
)
{
    if (
        sessionId == 0
    )
    {
        return 0;
    }

    return (
        static_cast<std::uint64_t>(
            sessionId
        ) <<
            32U
    ) |
    static_cast<std::uint64_t>(
        captureMonotonicMs
    );
}

bool unpackOfflineCaptureMetadata(
    std::uint64_t packed,
    std::uint32_t& sessionId,
    std::uint32_t& captureMonotonicMs
)
{
    sessionId =
        static_cast<std::uint32_t>(
            packed >> 32U
        );

    captureMonotonicMs =
        static_cast<std::uint32_t>(
            packed & 0xFFFFFFFFULL
        );

    return (
        sessionId != 0
    );
}

OfflineTimestampResult resolveOfflineTimestampMs(
    std::uint32_t recordSessionId,
    std::uint32_t captureMonotonicMs,
    const TimeAnchor* anchor,
    std::int64_t& epochMs
)
{
    epochMs = 0;

    if (
        recordSessionId == 0
    )
    {
        return OfflineTimestampResult::INVALID_METADATA;
    }

    if (
        anchor == nullptr ||
        anchor->sessionId == 0 ||
        anchor->epochMs <= 0
    )
    {
        return OfflineTimestampResult::WAITING_FOR_ANCHOR;
    }

    if (
        recordSessionId !=
        anchor->sessionId
    )
    {
        return OfflineTimestampResult::SESSION_MISMATCH;
    }

    // uint32_t millis() wraps naturally. Casting the modular subtraction
    // to int32_t gives the correct signed delta as long as the capture is
    // within +/-24.8 days of the anchor. The ring retains ~27 h, so this
    // bound comfortably covers every active record without inventing a
    // fixed sampling cadence.
    const std::int32_t monotonicDeltaMs =
        static_cast<std::int32_t>(
            captureMonotonicMs -
            anchor->monotonicMs
        );

    if (
        monotonicDeltaMs > 0 &&
        anchor->epochMs >
            std::numeric_limits<std::int64_t>::max() -
            monotonicDeltaMs
    )
    {
        return OfflineTimestampResult::OUT_OF_RANGE;
    }

    if (
        monotonicDeltaMs < 0 &&
        anchor->epochMs <
            -static_cast<std::int64_t>(
                monotonicDeltaMs
            )
    )
    {
        return OfflineTimestampResult::OUT_OF_RANGE;
    }

    epochMs =
        anchor->epochMs +
        static_cast<std::int64_t>(
            monotonicDeltaMs
        );

    if (
        epochMs <= 0
    )
    {
        epochMs = 0;
        return OfflineTimestampResult::OUT_OF_RANGE;
    }

    return OfflineTimestampResult::RESOLVED;
}

std::uint32_t calculateDurableTimeAnchorCrc(
    const DurableTimeAnchorBlob& blob
)
{
    return crc32Bytes(
        reinterpret_cast<const std::uint8_t*>(
            &blob
        ),
        offsetof(
            DurableTimeAnchorBlob,
            crc32
        )
    );
}

void finalizeDurableTimeAnchor(
    DurableTimeAnchorBlob& blob
)
{
    blob.crc32 =
        calculateDurableTimeAnchorCrc(
            blob
        );
}

bool isDurableTimeAnchorValid(
    const DurableTimeAnchorBlob& blob,
    std::uint32_t expectedMagic,
    std::uint16_t expectedVersion,
    std::int64_t minimumEpochMs
)
{
    return (
        blob.magic == expectedMagic &&
        blob.version == expectedVersion &&
        blob.sessionId != 0 &&
        blob.epochMs >= minimumEpochMs &&
        blob.crc32 ==
            calculateDurableTimeAnchorCrc(
                blob
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

std::uint64_t calculateRecoveryHeadOrdinal(
    std::uint64_t minimumActiveOrdinal,
    std::uint64_t maximumActiveOrdinal,
    std::size_t capacity
)
{
    if (
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

    if (
        span <= capacity
    )
    {
        return minimumActiveOrdinal;
    }

    return (
        maximumActiveOrdinal -
        static_cast<std::uint64_t>(
            capacity
        ) +
        1ULL
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

std::size_t calculateLogicalQueueCapacity(
    std::size_t physicalRingSlots
)
{
    if (
        physicalRingSlots < 2
    )
    {
        return 0;
    }

    // Keep one physical slot permanently spare. A full logical queue can
    // therefore durably write+verify the new record before the oldest
    // record is logically consumed.
    return (
        physicalRingSlots -
        1
    );
}

bool shouldDropOldestAfterVerifiedWrite(
    bool queueWasFull,
    bool newRecordWriteVerified
)
{
    return (
        queueWasFull &&
        newRecordWriteVerified
    );
}

bool shouldEvictArchiveEntry(
    std::size_t currentEntryCount,
    std::size_t maximumEntryCount
)
{
    return (
        maximumEntryCount > 0 &&
        currentEntryCount >=
            maximumEntryCount
    );
}

std::size_t calculateArchiveEvictionCount(
    std::size_t currentEntryCount,
    std::size_t maximumEntryCount,
    std::size_t maximumEvictionsPerPass
)
{
    if (
        maximumEntryCount == 0 ||
        maximumEvictionsPerPass == 0 ||
        currentEntryCount <
            maximumEntryCount
    )
    {
        return 0;
    }

    const std::size_t requiredEvictions =
        currentEntryCount -
        maximumEntryCount +
        1;

    return (
        requiredEvictions <
                maximumEvictionsPerPass
            ? requiredEvictions
            : maximumEvictionsPerPass
    );
}

bool shouldAttemptRejectedArchiveAfterDurableRingWrite(
    bool ringWriteSucceeded
)
{
    return ringWriteSucceeded;
}

bool shouldConsumeRejectedRingAfterArchive(
    bool ringWriteSucceeded,
    bool archiveWriteSucceeded
)
{
    // The ring copy is the power-cut safety source. It may be consumed only
    // after the isolation archive has been durably written and verified.
    return (
        ringWriteSucceeded &&
        archiveWriteSucceeded
    );
}

bool isBackendTransportAllowed(
    const char* url,
    bool allowInsecureHttpForLocalDev
)
{
    if (
        startsWith(
            url,
            "https://"
        )
    )
    {
        return true;
    }

    if (
        startsWith(
            url,
            "http://"
        )
    )
    {
        return allowInsecureHttpForLocalDev;
    }

    return false;
}

} // namespace FirmwareLogic
