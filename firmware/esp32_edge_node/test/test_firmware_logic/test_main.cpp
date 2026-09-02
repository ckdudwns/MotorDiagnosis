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
    "2026-09-02T06:29:30Z";

void testAckAcceptsNew201()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::VALID
        ),
        static_cast<int>(
            validateAckJson(
                201,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckAcceptsDuplicate200()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":true,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::VALID
        ),
        static_cast<int>(
            validateAckJson(
                200,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejectsEmptyBody()
{
    TEST_ASSERT_NOT_EQUAL(
        static_cast<int>(
            AckValidationResult::VALID
        ),
        static_cast<int>(
            validateAckJson(
                201,
                "",
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejectsWrongDevice()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"OTHER\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::DEVICE_MISMATCH
        ),
        static_cast<int>(
            validateAckJson(
                201,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejectsWrongSequence()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":9999}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::SEQUENCE_MISMATCH
        ),
        static_cast<int>(
            validateAckJson(
                201,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejectsAcceptedFalse()
{
    const char* body =
        "{\"accepted\":false,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::NOT_ACCEPTED
        ),
        static_cast<int>(
            validateAckJson(
                201,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejects204EvenThough2xx()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::UNSUPPORTED_STATUS
        ),
        static_cast<int>(
            validateAckJson(
                204,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejects201DuplicateTrue()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":true,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::DUPLICATE_CONTRACT_MISMATCH
        ),
        static_cast<int>(
            validateAckJson(
                201,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testAckRejects200DuplicateFalse()
{
    const char* body =
        "{\"accepted\":true,\"duplicate\":false,\"deviceId\":\"DEV-01-MOT-02\",\"sequence\":1234}";

    TEST_ASSERT_EQUAL_INT(
        static_cast<int>(
            AckValidationResult::DUPLICATE_CONTRACT_MISMATCH
        ),
        static_cast<int>(
            validateAckJson(
                200,
                body,
                DEVICE_ID,
                1234
            )
        )
    );
}

void testCanonicalPayloadIsStableAcrossRingFloatRoundTrip()
{
    CanonicalTelemetry firstSend;

    firstSend.sequence =
        5001;

    firstSend.vibrationRmsRaw =
        canonicalizeMeasurement(
            0.015261123
        );

    firstSend.vibrationPeakHz =
        canonicalizeMeasurement(
            100.005001
        );

    firstSend.acousticRmsRaw =
        canonicalizeMeasurement(
            148243.081234
        );

    firstSend.acousticPeakHz =
        canonicalizeMeasurement(
            7.812345
        );

    // Simulate the exact values persisted by BinaryTelemetryRecord.
    CanonicalTelemetry replayed =
        firstSend;

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

    TEST_ASSERT_FALSE(
        initialJson.empty()
    );

    TEST_ASSERT_EQUAL_STRING(
        initialJson.c_str(),
        replayJson.c_str()
    );
}

void testCanonicalPayloadKeepsTelemetryUnlabeled()
{
    CanonicalTelemetry telemetry;

    telemetry.sequence =
        1;

    const std::string json =
        buildCanonicalTelemetryPayload(
            TIMESTAMP,
            SITE_ID,
            ASSET_ID,
            DEVICE_ID,
            telemetry
        );

    TEST_ASSERT_NOT_NULL(
        std::strstr(
            json.c_str(),
            "\"scenarioLabel\":null"
        )
    );

    TEST_ASSERT_NOT_NULL(
        std::strstr(
            json.c_str(),
            "\"knownVibrationLabel\":null"
        )
    );

    TEST_ASSERT_NOT_NULL(
        std::strstr(
            json.c_str(),
            "\"knownAcousticLabel\":null"
        )
    );
}

void testCanonicalPayloadRejectsMissingTimestamp()
{
    CanonicalTelemetry telemetry;

    TEST_ASSERT_TRUE(
        buildCanonicalTelemetryPayload(
            "",
            SITE_ID,
            ASSET_ID,
            DEVICE_ID,
            telemetry
        ).empty()
    );
}

void testFullBacklogReplayIsBounded()
{
    TEST_ASSERT_EQUAL_UINT32(
        4,
        calculateReplayBatchSize(
            25000,
            4
        )
    );
}

void testReplayBatchUsesWholeSmallQueue()
{
    TEST_ASSERT_EQUAL_UINT32(
        3,
        calculateReplayBatchSize(
            3,
            4
        )
    );
}

void testReplayBatchHandlesEmptyQueue()
{
    TEST_ASSERT_EQUAL_UINT32(
        0,
        calculateReplayBatchSize(
            0,
            4
        )
    );
}

void testSequenceAdvancesAfterDurableWriteAndMatchingReadback()
{
    TEST_ASSERT_TRUE(
        shouldAdvanceSequence(
            true,
            true,
            2543,
            2543
        )
    );
}

void testSequenceDoesNotAdvanceWhenNvsWriteFails()
{
    TEST_ASSERT_FALSE(
        shouldAdvanceSequence(
            false,
            false,
            2543,
            2542
        )
    );
}

void testSequenceDoesNotAdvanceWhenReadbackFails()
{
    TEST_ASSERT_FALSE(
        shouldAdvanceSequence(
            true,
            false,
            2543,
            2543
        )
    );
}

void testSequenceDoesNotAdvanceWhenReadbackMismatches()
{
    TEST_ASSERT_FALSE(
        shouldAdvanceSequence(
            true,
            true,
            2543,
            2542
        )
    );
}

void testColdBootAnchorSameOrdinal()
{
    constexpr std::int64_t anchorEpochMs =
        1788330570000LL;

    TEST_ASSERT_EQUAL_INT64(
        anchorEpochMs,
        resolveTimestampMs(
            701,
            701,
            anchorEpochMs,
            3990
        )
    );
}

void testColdBootAnchorResolvesOlderRecord()
{
    constexpr std::int64_t anchorEpochMs =
        1788330570000LL;

    TEST_ASSERT_EQUAL_INT64(
        anchorEpochMs -
            3LL * 3990LL,
        resolveTimestampMs(
            698,
            701,
            anchorEpochMs,
            3990
        )
    );
}

void testColdBootAnchorResolvesNewerRecord()
{
    constexpr std::int64_t anchorEpochMs =
        1788330570000LL;

    TEST_ASSERT_EQUAL_INT64(
        anchorEpochMs +
            2LL * 3990LL,
        resolveTimestampMs(
            703,
            701,
            anchorEpochMs,
            3990
        )
    );
}

void testColdBootAnchorRejectsInvalidTime()
{
    TEST_ASSERT_EQUAL_INT64(
        -1,
        resolveTimestampMs(
            700,
            701,
            0,
            3990
        )
    );
}

void testColdBootAnchorRejectsZeroCadence()
{
    TEST_ASSERT_EQUAL_INT64(
        -1,
        resolveTimestampMs(
            700,
            701,
            1788330570000LL,
            0
        )
    );
}

void testWatermarkRecoveryIgnoresDurablyConsumedOrdinal()
{
    TEST_ASSERT_TRUE(
        shouldIgnoreRecoveredOrdinal(
            276,
            276
        )
    );
}

void testAckLossPowerCutReplaysOnlyAfterDurableWatermark()
{
    // Simulates a crash after RAM consumption reached 290 while NVS
    // watermark was only 276. 277..290 are allowed to replay safely.
    TEST_ASSERT_FALSE(
        shouldIgnoreRecoveredOrdinal(
            277,
            276
        )
    );

    TEST_ASSERT_FALSE(
        shouldIgnoreRecoveredOrdinal(
            290,
            276
        )
    );
}

void testWatermarkCommitsAtBatchBoundary()
{
    TEST_ASSERT_TRUE(
        shouldCommitWatermark(
            32,
            32,
            false
        )
    );
}

void testWatermarkDoesNotCommitEarly()
{
    TEST_ASSERT_FALSE(
        shouldCommitWatermark(
            31,
            32,
            false
        )
    );
}

void testWatermarkForceCommitWorksOnReplayPause()
{
    TEST_ASSERT_TRUE(
        shouldCommitWatermark(
            1,
            32,
            true
        )
    );
}

void testRecoveredQueueCountUsesOrdinalSpan()
{
    TEST_ASSERT_EQUAL_UINT32(
        14,
        calculateRecoveredQueueCount(
            277,
            290,
            25000,
            14
        )
    );
}

void testRecoveredQueueCountCapsAtCapacity()
{
    TEST_ASSERT_EQUAL_UINT32(
        25000,
        calculateRecoveredQueueCount(
            1,
            30000,
            25000,
            25000
        )
    );
}

void testRecoveredQueueCountIsZeroWithoutActiveRecords()
{
    TEST_ASSERT_EQUAL_UINT32(
        0,
        calculateRecoveredQueueCount(
            0,
            0,
            25000,
            0
        )
    );
}

void testNextOrdinalFollowsMaximumSeen()
{
    TEST_ASSERT_EQUAL_UINT64(
        683,
        calculateNextRingOrdinal(
            682
        )
    );
}

void testNextOrdinalNeverReturnsZeroAfterOverflow()
{
    TEST_ASSERT_EQUAL_UINT64(
        1,
        calculateNextRingOrdinal(
            UINT64_MAX
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
    RUN_TEST(testAckAcceptsDuplicate200);
    RUN_TEST(testAckRejectsEmptyBody);
    RUN_TEST(testAckRejectsWrongDevice);
    RUN_TEST(testAckRejectsWrongSequence);
    RUN_TEST(testAckRejectsAcceptedFalse);
    RUN_TEST(testAckRejects204EvenThough2xx);
    RUN_TEST(testAckRejects201DuplicateTrue);
    RUN_TEST(testAckRejects200DuplicateFalse);

    RUN_TEST(testCanonicalPayloadIsStableAcrossRingFloatRoundTrip);
    RUN_TEST(testCanonicalPayloadKeepsTelemetryUnlabeled);
    RUN_TEST(testCanonicalPayloadRejectsMissingTimestamp);

    RUN_TEST(testFullBacklogReplayIsBounded);
    RUN_TEST(testReplayBatchUsesWholeSmallQueue);
    RUN_TEST(testReplayBatchHandlesEmptyQueue);

    RUN_TEST(testSequenceAdvancesAfterDurableWriteAndMatchingReadback);
    RUN_TEST(testSequenceDoesNotAdvanceWhenNvsWriteFails);
    RUN_TEST(testSequenceDoesNotAdvanceWhenReadbackFails);
    RUN_TEST(testSequenceDoesNotAdvanceWhenReadbackMismatches);

    RUN_TEST(testColdBootAnchorSameOrdinal);
    RUN_TEST(testColdBootAnchorResolvesOlderRecord);
    RUN_TEST(testColdBootAnchorResolvesNewerRecord);
    RUN_TEST(testColdBootAnchorRejectsInvalidTime);
    RUN_TEST(testColdBootAnchorRejectsZeroCadence);

    RUN_TEST(testWatermarkRecoveryIgnoresDurablyConsumedOrdinal);
    RUN_TEST(testAckLossPowerCutReplaysOnlyAfterDurableWatermark);
    RUN_TEST(testWatermarkCommitsAtBatchBoundary);
    RUN_TEST(testWatermarkDoesNotCommitEarly);
    RUN_TEST(testWatermarkForceCommitWorksOnReplayPause);

    RUN_TEST(testRecoveredQueueCountUsesOrdinalSpan);
    RUN_TEST(testRecoveredQueueCountCapsAtCapacity);
    RUN_TEST(testRecoveredQueueCountIsZeroWithoutActiveRecords);
    RUN_TEST(testNextOrdinalFollowsMaximumSeen);
    RUN_TEST(testNextOrdinalNeverReturnsZeroAfterOverflow);

    return UNITY_END();
}
