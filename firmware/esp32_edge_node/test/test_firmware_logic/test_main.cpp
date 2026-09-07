#include <unity.h>

#include <cstring>
#include <string>

#include "firmware_logic.h"

using namespace FirmwareLogic;

namespace
{

constexpr const char* DEVICE_ID =
    "DEV-01-MOT-02";

constexpr const char* SITE_ID =
    "SITE-01";

constexpr const char* ASSET_ID =
    "SITE-01-MOT-02";

constexpr const char* TIMESTAMP =
    "2026-09-02T07:52:36Z";

#pragma pack(push, 1)
struct LegacyV1RingRecordFixture
{
    std::uint32_t magic = 0x4D445231UL;
    std::uint16_t schemaVersion = 1;
    std::uint16_t flags = 1;
    std::uint64_t ordinal = 821;
    std::uint32_t sequence = 4073;
    std::uint64_t epochSeconds = 0;
    float vibrationRmsRaw = 0.014f;
    float vibrationPeakHz = 40.42f;
    float acousticRmsRaw = 112325.0f;
    float acousticPeakHz = 7.81f;
    std::uint32_t crc32 = 0;
};
#pragma pack(pop)

static_assert(
    sizeof(LegacyV1RingRecordFixture) == 48,
    "Legacy v1 ring fixture must match the persisted 48-byte record layout."
);

void assertAckResult(
    AckValidationResult expected,
    int status,
    const char* body,
    std::uint32_t sequence = 1234
)
{
    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(expected),
        static_cast<int>(
            validateAckJson(
                status,
                body,
                DEVICE_ID,
                sequence
            )
        )
    );
}

void testAckAcceptsNew201()
{
    assertAckResult(
        AckValidationResult::VALID,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234,\"receivedAt\":\"2026-09-02T07:52:36Z\"}"
    );
}

void testAckAcceptsNestedNonAckMetadata()
{
    assertAckResult(
        AckValidationResult::VALID,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234,\"receivedAt\":\"2026-09-07T13:18:16.795Z\",\"lifecycleUpdates\":[{\"kind\":\"asset_event_updated\",\"eventId\":\"AI2-EVENT\",\"created\":false}]}"
    );
}

void testAckRejectsMalformedNestedMetadata()
{
    const char* malformedResponses[] = {
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234,\"lifecycleUpdates\":[not-json]}",
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234,\"lifecycleUpdates\":[{\"kind\":}]}",
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234,\"lifecycleUpdates\":[\"\\q\"]}"
    };

    for (const char* response : malformedResponses)
    {
        assertAckResult(
            AckValidationResult::MALFORMED_RESPONSE,
            201,
            response
        );
    }
}

void testAckAcceptsDuplicate200()
{
    assertAckResult(
        AckValidationResult::VALID,
        200,
        "{\"accepted\":true,\"duplicate\":true,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}"
    );
}

void testAckRejects204EvenThough2xx()
{
    assertAckResult(
        AckValidationResult::UNSUPPORTED_STATUS,
        204,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}"
    );
}

void testAckRejectsWrongDevice()
{
    assertAckResult(
        AckValidationResult::DEVICE_MISMATCH,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"OTHER\",\"sequence\":1234}"
    );
}

void testAckRejectsWrongSequence()
{
    assertAckResult(
        AckValidationResult::SEQUENCE_MISMATCH,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":9999}"
    );
}

void testAckRejectsAcceptedFalse()
{
    assertAckResult(
        AckValidationResult::NOT_ACCEPTED,
        201,
        "{\"accepted\":false,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}"
    );
}

void testAckRejects201DuplicateTrue()
{
    assertAckResult(
        AckValidationResult::DUPLICATE_CONTRACT_MISMATCH,
        201,
        "{\"accepted\":true,\"duplicate\":true,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}"
    );
}

void testAckRejects200DuplicateFalse()
{
    assertAckResult(
        AckValidationResult::DUPLICATE_CONTRACT_MISMATCH,
        200,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}"
    );
}

void testAckRejectsEmptyBody()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        ""
    );
}

void testAckRejectsMissingClosingBrace()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234"
    );
}

void testAckRejectsTrailingGarbage()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}garbage"
    );
}

void testAckRejectsNestedAckObject()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"wrapper\":{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}}"
    );
}

void testAckRejectsNestedRequiredField()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"meta\":{\"sequence\":1234}}"
    );
}

void testAckRejectsDuplicateRequiredKey()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"accepted\":true,\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}"
    );
}

void testAckRejectsSequenceAsString()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":\"1234\"}"
    );
}

void testAckRejectsSequenceAsFloat()
{
    assertAckResult(
        AckValidationResult::MALFORMED_RESPONSE,
        201,
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234.0}"
    );
}

void testRfc3339ZuluParses()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_TRUE(
        parseRfc3339ToEpochMs(
            "2026-09-02T07:52:36Z",
            epochMs
        )
    );

    TEST_ASSERT_EQUAL_INT64(
        1788335556000LL,
        epochMs
    );
}

void testRfc3339FractionParses()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_TRUE(
        parseRfc3339ToEpochMs(
            "2026-09-02T07:52:36.123Z",
            epochMs
        )
    );

    TEST_ASSERT_EQUAL_INT64(
        1788335556123LL,
        epochMs
    );
}

void testRfc3339OffsetParses()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_TRUE(
        parseRfc3339ToEpochMs(
            "2026-09-02T09:52:36+02:00",
            epochMs
        )
    );

    TEST_ASSERT_EQUAL_INT64(
        1788335556000LL,
        epochMs
    );
}

void testRfc3339RejectsInvalidCalendarDate()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_FALSE(
        parseRfc3339ToEpochMs(
            "2026-02-30T07:52:36Z",
            epochMs
        )
    );
}

void testHealthContractParsesActualV13Shape()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_TRUE(
        parseHealthTimestampJson(
            "{\n  \"ok\": true,\n  \"service\": \"Bind Edge AI backend\",\n  \"timestamp\": \"2026-09-02T07:52:36Z\"\n}",
            epochMs
        )
    );

    TEST_ASSERT_EQUAL_INT64(
        1788335556000LL,
        epochMs
    );
}

void testHealthContractRejectsLegacyNumericEpoch()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_FALSE(
        parseHealthTimestampJson(
            "{\"ok\":true,\"service\":\"Bind Edge AI backend\",\"timestamp\":1788335556.0}",
            epochMs
        )
    );
}

void testHealthContractRejectsNestedTimestamp()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_FALSE(
        parseHealthTimestampJson(
            "{\"ok\":true,\"meta\":{\"timestamp\":\"2026-09-02T07:52:36Z\"}}",
            epochMs
        )
    );
}

void testCanonicalPayloadStableAcrossFloatRoundTrip()
{
    CanonicalTelemetry firstSend;
    firstSend.sequence = 5001;
    firstSend.vibrationRmsRaw = canonicalizeMeasurement(0.015261123);
    firstSend.vibrationPeakHz = canonicalizeMeasurement(100.005001);
    firstSend.acousticRmsRaw = canonicalizeMeasurement(148243.081234);
    firstSend.acousticPeakHz = canonicalizeMeasurement(7.812345);

    CanonicalTelemetry replayed = firstSend;

    const std::string initialJson =
        buildCanonicalTelemetryPayload(
            TIMESTAMP,
            SITE_ID,
            ASSET_ID,
            DEVICE_ID,
            firstSend
        );

    const std::string replayJson =
        buildCanonicalTelemetryPayload(
            TIMESTAMP,
            SITE_ID,
            ASSET_ID,
            DEVICE_ID,
            replayed
        );

    TEST_ASSERT_EQUAL_STRING(
        initialJson.c_str(),
        replayJson.c_str()
    );
}

void testCanonicalPayloadKeepsTelemetryUnlabeled()
{
    CanonicalTelemetry telemetry;
    telemetry.sequence = 1;

    const std::string json =
        buildCanonicalTelemetryPayload(
            TIMESTAMP,
            SITE_ID,
            ASSET_ID,
            DEVICE_ID,
            telemetry
        );

    TEST_ASSERT_NOT_NULL(std::strstr(json.c_str(), "\"scenarioLabel\":null"));
    TEST_ASSERT_NOT_NULL(std::strstr(json.c_str(), "\"knownVibrationLabel\":null"));
    TEST_ASSERT_NOT_NULL(std::strstr(json.c_str(), "\"knownAcousticLabel\":null"));
}

void testFullBacklogReplayIsBounded()
{
    TEST_ASSERT_EQUAL_UINT32(
        4,
        calculateReplayBatchSize(
            24999,
            4
        )
    );
}

void testReplayBatchHandlesSmallQueue()
{
    TEST_ASSERT_EQUAL_UINT32(3, calculateReplayBatchSize(3, 4));
}

void testSequenceAdvancesOnlyAfterDurableReadback()
{
    TEST_ASSERT_TRUE(shouldAdvanceSequence(true, true, 2543, 2543));
    TEST_ASSERT_FALSE(shouldAdvanceSequence(false, false, 2543, 2542));
    TEST_ASSERT_FALSE(shouldAdvanceSequence(true, false, 2543, 2543));
    TEST_ASSERT_FALSE(shouldAdvanceSequence(true, true, 2543, 2542));
}

void testTransientRingIoFailureDoesNotConsume()
{
    TEST_ASSERT_FALSE(
        shouldConsumeAfterReadFailure(
            RingReadClass::IO_ERROR
        )
    );
}

void testCrcCorruptionCanBeConsumedAsUnrecoverable()
{
    TEST_ASSERT_TRUE(
        shouldConsumeAfterReadFailure(
            RingReadClass::CORRUPT
        )
    );
}

void testLegacyV1SerializedUnresolvedRecordIsRecognizedForMigration()
{
    LegacyV1RingRecordFixture previousFirmwareRecord;

    std::uint8_t serialized[
        sizeof(previousFirmwareRecord)
    ] = {};

    std::memcpy(
        serialized,
        &previousFirmwareRecord,
        sizeof(previousFirmwareRecord)
    );

    LegacyV1RingRecordFixture restored;

    std::memcpy(
        &restored,
        serialized,
        sizeof(restored)
    );

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            RingRecordTimeEncoding::LEGACY_UNRESOLVED_V1
        ),
        static_cast<int>(
            classifyRingRecordTimeEncoding(
                restored.schemaVersion,
                restored.flags,
                restored.epochSeconds,
                1,
                2,
                1,
                1700000000ULL
            )
        )
    );

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            RingRecoveryDisposition::LEGACY_ISOLATION
        ),
        static_cast<int>(
            classifyRingRecordForRecovery(
                restored.schemaVersion,
                restored.flags,
                restored.epochSeconds,
                true,
                true,
                1,
                2,
                1,
                1700000000ULL
            )
        )
    );
}

void testSchemaV1PackedTransitionAndV2PackedRecordsRemainReadable()
{
    const std::uint64_t packed =
        packOfflineCaptureMetadata(
            42,
            123456
        );

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            RingRecordTimeEncoding::PACKED_UNRESOLVED
        ),
        static_cast<int>(
            classifyRingRecordTimeEncoding(
                1,
                1,
                packed,
                1,
                2,
                1,
                1700000000ULL
            )
        )
    );

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            RingRecordTimeEncoding::PACKED_UNRESOLVED
        ),
        static_cast<int>(
            classifyRingRecordTimeEncoding(
                2,
                1,
                packed,
                1,
                2,
                1,
                1700000000ULL
            )
        )
    );
}

void testOfflineMetadataRoundTrip()
{
    const std::uint64_t packed =
        packOfflineCaptureMetadata(
            42,
            123456
        );

    std::uint32_t sessionId = 0;
    std::uint32_t captureMs = 0;

    TEST_ASSERT_TRUE(
        unpackOfflineCaptureMetadata(
            packed,
            sessionId,
            captureMs
        )
    );

    TEST_ASSERT_EQUAL_UINT32(42, sessionId);
    TEST_ASSERT_EQUAL_UINT32(123456, captureMs);
}

void testOfflineTimestampUsesMeasuredMonotonicDeltaNotFixedCadence()
{
    TimeAnchor anchor;
    anchor.sessionId = 42;
    anchor.monotonicMs = 100000;
    anchor.epochMs = 1788335556000LL;

    std::int64_t epochMs = 0;

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(OfflineTimestampResult::RESOLVED),
        static_cast<int>(
            resolveOfflineTimestampMs(
                42,
                93617,
                &anchor,
                epochMs
            )
        )
    );

    // Exact observed delta: 6383 ms, intentionally not a multiple of 3990.
    TEST_ASSERT_EQUAL_INT64(
        1788335549617LL,
        epochMs
    );
}

void testOfflineTimestampRejectsPreviousBootSession()
{
    TimeAnchor anchor;
    anchor.sessionId = 43;
    anchor.monotonicMs = 1000;
    anchor.epochMs = 1788335556000LL;

    std::int64_t epochMs = 0;

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(OfflineTimestampResult::SESSION_MISMATCH),
        static_cast<int>(
            resolveOfflineTimestampMs(
                42,
                900,
                &anchor,
                epochMs
            )
        )
    );
}

void testOfflineTimestampWaitsWithoutAnchor()
{
    std::int64_t epochMs = 0;

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(OfflineTimestampResult::WAITING_FOR_ANCHOR),
        static_cast<int>(
            resolveOfflineTimestampMs(
                42,
                900,
                nullptr,
                epochMs
            )
        )
    );
}

void testMultipleRebootsNeverApplyStaleAnchor()
{
    TimeAnchor boot1;
    boot1.sessionId = 100;
    boot1.monotonicMs = 10000;
    boot1.epochMs = 1788335556000LL;

    TimeAnchor boot3;
    boot3.sessionId = 102;
    boot3.monotonicMs = 5000;
    boot3.epochMs = 1788336556000LL;

    std::int64_t epochMs = 0;

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(OfflineTimestampResult::SESSION_MISMATCH),
        static_cast<int>(
            resolveOfflineTimestampMs(
                101,
                2500,
                &boot1,
                epochMs
            )
        )
    );

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(OfflineTimestampResult::SESSION_MISMATCH),
        static_cast<int>(
            resolveOfflineTimestampMs(
                101,
                2500,
                &boot3,
                epochMs
            )
        )
    );
}

void testDurableTimeAnchorCrcAcceptsIntactBlob()
{
    DurableTimeAnchorBlob blob;
    blob.magic = 0x4D445441UL;
    blob.version = 1;
    blob.sessionId = 42;
    blob.monotonicMs = 123456;
    blob.epochMs = 1788335556000LL;

    finalizeDurableTimeAnchor(
        blob
    );

    TEST_ASSERT_TRUE(
        isDurableTimeAnchorValid(
            blob,
            0x4D445441UL,
            1,
            1700000000000LL
        )
    );
}

void testDurableTimeAnchorCrcRejectsPartialOrCorruptBlob()
{
    DurableTimeAnchorBlob blob;
    blob.magic = 0x4D445441UL;
    blob.version = 1;
    blob.sessionId = 42;
    blob.monotonicMs = 123456;
    blob.epochMs = 1788335556000LL;

    finalizeDurableTimeAnchor(
        blob
    );

    blob.monotonicMs += 1;

    TEST_ASSERT_FALSE(
        isDurableTimeAnchorValid(
            blob,
            0x4D445441UL,
            1,
            1700000000000LL
        )
    );

    blob.crc32 = 0;

    TEST_ASSERT_FALSE(
        isDurableTimeAnchorValid(
            blob,
            0x4D445441UL,
            1,
            1700000000000LL
        )
    );
}

void testDurableTimeAnchorRejectsWrongGenerationFields()
{
    DurableTimeAnchorBlob blob;
    blob.magic = 0x4D445441UL;
    blob.version = 1;
    blob.sessionId = 0;
    blob.monotonicMs = 123456;
    blob.epochMs = 1788335556000LL;

    finalizeDurableTimeAnchor(
        blob
    );

    TEST_ASSERT_FALSE(
        isDurableTimeAnchorValid(
            blob,
            0x4D445441UL,
            1,
            1700000000000LL
        )
    );
}

void testRingKeepsOnePhysicalSpareSlot()
{
    TEST_ASSERT_EQUAL_UINT32(
        24999,
        calculateLogicalQueueCapacity(
            25000
        )
    );
}

void testFullRingDoesNotDropOldestBeforeNewWriteVerified()
{
    TEST_ASSERT_FALSE(
        shouldDropOldestAfterVerifiedWrite(
            true,
            false
        )
    );

    TEST_ASSERT_TRUE(
        shouldDropOldestAfterVerifiedWrite(
            true,
            true
        )
    );
}

void testPowerCutAfterSpareWriteRecoveryKeepsNewestWindow()
{
    // Simulate 25,000 valid ordinals in 25,000 physical slots after a
    // power cut that happened after writing the spare but before committing
    // the oldest-drop watermark. Logical capacity is 24,999, so recovery
    // must retain 2..25000 and not discard the newly durable record.
    TEST_ASSERT_EQUAL_UINT64(
        2,
        calculateRecoveryHeadOrdinal(
            1,
            25000,
            24999
        )
    );

    TEST_ASSERT_EQUAL_UINT32(
        24999,
        calculateRecoveredQueueCount(
            1,
            25000,
            24999,
            25000
        )
    );
}

void testWatermarkRecoveryAllowsAckLossReplay()
{
    TEST_ASSERT_TRUE(shouldIgnoreRecoveredOrdinal(276, 276));
    TEST_ASSERT_FALSE(shouldIgnoreRecoveredOrdinal(277, 276));
    TEST_ASSERT_FALSE(shouldIgnoreRecoveredOrdinal(290, 276));
}

void testWatermarkBatchPolicy()
{
    TEST_ASSERT_TRUE(shouldCommitWatermark(32, 32, false));
    TEST_ASSERT_FALSE(shouldCommitWatermark(31, 32, false));
    TEST_ASSERT_TRUE(shouldCommitWatermark(1, 32, true));
}

void testArchiveCapEvictsAtBound()
{
    TEST_ASSERT_FALSE(shouldEvictArchiveEntry(127, 128));
    TEST_ASSERT_TRUE(shouldEvictArchiveEntry(128, 128));
}

void testArchiveCleanupLeavesRoomForPendingWrite()
{
    TEST_ASSERT_EQUAL_UINT32(
        0,
        calculateArchiveEvictionCount(127, 128, 8)
    );
    TEST_ASSERT_EQUAL_UINT32(
        1,
        calculateArchiveEvictionCount(128, 128, 8)
    );
    TEST_ASSERT_EQUAL_UINT32(
        2,
        calculateArchiveEvictionCount(129, 128, 8)
    );
}

void testArchiveCleanupWorkIsBoundedPerPass()
{
    TEST_ASSERT_EQUAL_UINT32(
        8,
        calculateArchiveEvictionCount(300, 128, 8)
    );
    TEST_ASSERT_EQUAL_UINT32(
        0,
        calculateArchiveEvictionCount(300, 128, 0)
    );
}

void testImmediateRejectArchiveRequiresDurableRingSource()
{
    TEST_ASSERT_FALSE(
        shouldAttemptRejectedArchiveAfterDurableRingWrite(
            false
        )
    );

    TEST_ASSERT_TRUE(
        shouldAttemptRejectedArchiveAfterDurableRingWrite(
            true
        )
    );
}

void testPowerCutAfterIsolationDestinationDeleteKeepsRingSource()
{
    // Model the exact review cut point: the packet is already durable in
    // the ring, the archive replacement has not completed (for example,
    // power loss after deleting destination but before rename). The source
    // ring record must not be consumed.
    TEST_ASSERT_FALSE(
        shouldConsumeRejectedRingAfterArchive(
            true,
            false
        )
    );
}

void testSuccessfulIsolationReplacementAllowsRingConsume()
{
    TEST_ASSERT_TRUE(
        shouldConsumeRejectedRingAfterArchive(
            true,
            true
        )
    );

    TEST_ASSERT_FALSE(
        shouldConsumeRejectedRingAfterArchive(
            false,
            true
        )
    );
}

void testHttpsAllowedByDefault()
{
    TEST_ASSERT_TRUE(
        isBackendTransportAllowed(
            "https://backend.example.com/api/telemetry/ingest",
            false
        )
    );
}

void testPlainHttpRejectedByDefault()
{
    TEST_ASSERT_FALSE(
        isBackendTransportAllowed(
            "http://172.20.10.2:8787/api/telemetry/ingest",
            false
        )
    );
}

void testPlainHttpRequiresExplicitLocalDevOptIn()
{
    TEST_ASSERT_TRUE(
        isBackendTransportAllowed(
            "http://172.20.10.2:8787/api/telemetry/ingest",
            true
        )
    );
}

void testUnsupportedUrlSchemeRejected()
{
    TEST_ASSERT_FALSE(
        isBackendTransportAllowed(
            "ftp://backend.example.com/file",
            true
        )
    );
}

} // namespace

int main(
    int argc,
    char** argv
)
{
    (void)argc;
    (void)argv;

    UNITY_BEGIN();

    RUN_TEST(testAckAcceptsNew201);
    RUN_TEST(testAckAcceptsNestedNonAckMetadata);
    RUN_TEST(testAckRejectsMalformedNestedMetadata);
    RUN_TEST(testAckAcceptsDuplicate200);
    RUN_TEST(testAckRejects204EvenThough2xx);
    RUN_TEST(testAckRejectsWrongDevice);
    RUN_TEST(testAckRejectsWrongSequence);
    RUN_TEST(testAckRejectsAcceptedFalse);
    RUN_TEST(testAckRejects201DuplicateTrue);
    RUN_TEST(testAckRejects200DuplicateFalse);
    RUN_TEST(testAckRejectsEmptyBody);
    RUN_TEST(testAckRejectsMissingClosingBrace);
    RUN_TEST(testAckRejectsTrailingGarbage);
    RUN_TEST(testAckRejectsNestedAckObject);
    RUN_TEST(testAckRejectsNestedRequiredField);
    RUN_TEST(testAckRejectsDuplicateRequiredKey);
    RUN_TEST(testAckRejectsSequenceAsString);
    RUN_TEST(testAckRejectsSequenceAsFloat);

    RUN_TEST(testRfc3339ZuluParses);
    RUN_TEST(testRfc3339FractionParses);
    RUN_TEST(testRfc3339OffsetParses);
    RUN_TEST(testRfc3339RejectsInvalidCalendarDate);
    RUN_TEST(testHealthContractParsesActualV13Shape);
    RUN_TEST(testHealthContractRejectsLegacyNumericEpoch);
    RUN_TEST(testHealthContractRejectsNestedTimestamp);

    RUN_TEST(testCanonicalPayloadStableAcrossFloatRoundTrip);
    RUN_TEST(testCanonicalPayloadKeepsTelemetryUnlabeled);

    RUN_TEST(testFullBacklogReplayIsBounded);
    RUN_TEST(testReplayBatchHandlesSmallQueue);

    RUN_TEST(testSequenceAdvancesOnlyAfterDurableReadback);

    RUN_TEST(testTransientRingIoFailureDoesNotConsume);
    RUN_TEST(testCrcCorruptionCanBeConsumedAsUnrecoverable);
    RUN_TEST(testLegacyV1SerializedUnresolvedRecordIsRecognizedForMigration);
    RUN_TEST(testSchemaV1PackedTransitionAndV2PackedRecordsRemainReadable);

    RUN_TEST(testOfflineMetadataRoundTrip);
    RUN_TEST(testOfflineTimestampUsesMeasuredMonotonicDeltaNotFixedCadence);
    RUN_TEST(testOfflineTimestampRejectsPreviousBootSession);
    RUN_TEST(testOfflineTimestampWaitsWithoutAnchor);
    RUN_TEST(testMultipleRebootsNeverApplyStaleAnchor);
    RUN_TEST(testDurableTimeAnchorCrcAcceptsIntactBlob);
    RUN_TEST(testDurableTimeAnchorCrcRejectsPartialOrCorruptBlob);
    RUN_TEST(testDurableTimeAnchorRejectsWrongGenerationFields);

    RUN_TEST(testRingKeepsOnePhysicalSpareSlot);
    RUN_TEST(testFullRingDoesNotDropOldestBeforeNewWriteVerified);
    RUN_TEST(testPowerCutAfterSpareWriteRecoveryKeepsNewestWindow);

    RUN_TEST(testWatermarkRecoveryAllowsAckLossReplay);
    RUN_TEST(testWatermarkBatchPolicy);

    RUN_TEST(testArchiveCapEvictsAtBound);
    RUN_TEST(testArchiveCleanupLeavesRoomForPendingWrite);
    RUN_TEST(testArchiveCleanupWorkIsBoundedPerPass);
    RUN_TEST(testImmediateRejectArchiveRequiresDurableRingSource);
    RUN_TEST(testPowerCutAfterIsolationDestinationDeleteKeepsRingSource);
    RUN_TEST(testSuccessfulIsolationReplacementAllowsRingConsume);

    RUN_TEST(testHttpsAllowedByDefault);
    RUN_TEST(testPlainHttpRejectedByDefault);
    RUN_TEST(testPlainHttpRequiresExplicitLocalDevOptIn);
    RUN_TEST(testUnsupportedUrlSchemeRejected);

    return UNITY_END();
}
