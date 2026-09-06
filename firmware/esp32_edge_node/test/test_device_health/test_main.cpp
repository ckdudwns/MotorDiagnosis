#include <unity.h>
#include <ArduinoJson.h>
#include "device_health.h"
#include "firmware_logic.h"
#include <cstring>
#include <cstdio>
#include <string>

using namespace DeviceHealth;
namespace {
constexpr std::int64_t EPOCH = 1788656400000LL;
Metrics metrics() {
    Metrics value;
    value.rssiDbm = -61;
    value.rebootCount = 4;
    value.queuedRecords = 12500;
    value.queueCapacity = 25000;
    value.firmwareVersion = "test-\"version\"";
    return value;
}
JsonDocument decoded(const Journal& journal, std::uint32_t session = 1,
                     std::uint64_t now = 2000) {
    JsonDocument value;
    const auto text = payload(journal, metrics(), session, now, EPOCH);
    TEST_ASSERT_FALSE(text.empty());
    TEST_ASSERT_FALSE(deserializeJson(value, text));
    return value;
}
void testMetricsFollowBackendContract() {
    auto value = decoded(emptyJournal());
    TEST_ASSERT_EQUAL(-61, value["rssiDbm"].as<int>());
    TEST_ASSERT_EQUAL(4, value["rebootCount"].as<int>());
    TEST_ASSERT_EQUAL_FLOAT(50, value["bufferUsagePct"].as<float>());
    TEST_ASSERT_EQUAL_STRING("test-\"version\"", value["firmwareVersion"].as<const char*>());
    TEST_ASSERT_EQUAL_STRING(utcTimestamp(EPOCH).c_str(), value["reportedAt"].as<const char*>());
    TEST_ASSERT_FALSE(value["scenarioLabel"].is<const char*>());
    TEST_ASSERT_EQUAL(6, value.size());
}
void testPayloadRejectsInvalidMetrics() {
    auto value = metrics();
    value.rssiDbm = 1;
    TEST_ASSERT_TRUE(payload(emptyJournal(), value, 1, 0, EPOCH).empty());
    value = metrics(); value.queueCapacity = 0;
    TEST_ASSERT_TRUE(payload(emptyJournal(), value, 1, 0, EPOCH).empty());
    value = metrics(); value.queuedRecords = 25001;
    TEST_ASSERT_TRUE(payload(emptyJournal(), value, 1, 0, EPOCH).empty());
    value = metrics(); value.rebootCount = 2147483648U;
    TEST_ASSERT_TRUE(payload(emptyJournal(), value, 1, 0, EPOCH).empty());
    value = metrics(); value.firmwareVersion = "";
    TEST_ASSERT_TRUE(payload(emptyJournal(), value, 1, 0, EPOCH).empty());
}
void testPayloadRequiresKnownReportTime() {
    TEST_ASSERT_TRUE(payload(emptyJournal(), metrics(), 1, 0, 0).empty());
    TEST_ASSERT_TRUE(payload(emptyJournal(), metrics(), 0, 0, EPOCH).empty());
}
void testJournalRoundTripAndCorruption() {
    auto journal = emptyJournal();
    TEST_ASSERT_TRUE(observe(journal, Fault::ADXL_INIT, true, 1, 100, EPOCH));
    Journal recovered;
    std::memcpy(&recovered, &journal, sizeof(journal));
    TEST_ASSERT_TRUE(validJournal(recovered));
    recovered.pending[0].active = 0;
    TEST_ASSERT_FALSE(validJournal(recovered));
}
void testRepeatedFaultDoesNotRewriteOrChangeOccurrence() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_INIT, true, 1, 100, EPOCH);
    const auto original = journal;
    TEST_ASSERT_TRUE(observe(journal, Fault::ADXL_INIT, true, 1, 1000, EPOCH + 900));
    TEST_ASSERT_EQUAL_MEMORY(&original, &journal, sizeof(journal));
}
void testActiveRecoveryRemainsOrderedUntilAcknowledged() {
    auto journal = emptyJournal();
    observe(journal, Fault::I2S_CHANNEL, true, 1, 100, EPOCH - 1000);
    observe(journal, Fault::I2S_CHANNEL, false, 1, 200, EPOCH);
    auto value = decoded(journal);
    TEST_ASSERT_EQUAL(1, value["sensorFaults"].size());
    TEST_ASSERT_EQUAL_STRING("active", value["sensorFaults"][0]["status"].as<const char*>());
    TEST_ASSERT_TRUE(acknowledgeHead(journal));
    value = decoded(journal);
    TEST_ASSERT_EQUAL_STRING("recovered", value["sensorFaults"][0]["status"].as<const char*>());
}
void testAckLossOrFailedMarkerCannotAdvanceSuccessor() {
    auto durable = emptyJournal();
    observe(durable, Fault::I2S_CHANNEL, true, 1, 100, EPOCH - 1000);
    observe(durable, Fault::I2S_CHANNEL, false, 1, 200, EPOCH);
    auto uncommitted = durable;
    acknowledgeHead(uncommitted);
    // Power cut before storing ACK marker: reload original durable head.
    auto rebooted = durable;
    TEST_ASSERT_EQUAL_STRING("active", decoded(rebooted)["sensorFaults"][0]["status"].as<const char*>());
    TEST_ASSERT_EQUAL(2, rebooted.count);
    durable = uncommitted; // Verified NVS commit permits only then the recovery.
    TEST_ASSERT_EQUAL_STRING("recovered", decoded(durable)["sensorFaults"][0]["status"].as<const char*>());
}
void testCapacityNeverOverwritesOldestFault() {
    auto journal = emptyJournal();
    for (unsigned i = 0; i < JOURNAL_CAPACITY; ++i)
        TEST_ASSERT_TRUE(observe(journal, Fault::ADXL_CHANNEL, i % 2 == 0, 1, i, EPOCH + i));
    const auto original = journal;
    TEST_ASSERT_FALSE(observe(journal, Fault::ADXL_CHANNEL, true, 1, 1000, EPOCH + 1000));
    TEST_ASSERT_EQUAL_MEMORY(&original, &journal, sizeof(journal));
    TEST_ASSERT_TRUE(acknowledgeHead(journal));
    TEST_ASSERT_TRUE(observe(journal, Fault::ADXL_CHANNEL, true, 1, 1000, EPOCH + 1000));
}
void testInvalidObservationDoesNotMutateJournal() {
    auto journal = emptyJournal(); const auto original = journal;
    TEST_ASSERT_FALSE(observe(journal, Fault::COUNT, true, 1, 0, EPOCH));
    TEST_ASSERT_FALSE(observe(journal, Fault::ADXL_INIT, true, 0, 0, EPOCH));
    TEST_ASSERT_FALSE(observe(journal, Fault::ADXL_INIT, true, 1, 0, -1));
    TEST_ASSERT_EQUAL_MEMORY(&original, &journal, sizeof(journal));
    TEST_ASSERT_FALSE(acknowledgeHead(journal));
}
void testSameBootResolvesActualMonotonicTime() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_INIT, true, 1, 500, 0);
    auto value = decoded(journal, 1, 2000);
    TEST_ASSERT_EQUAL_STRING(utcTimestamp(EPOCH - 1500).c_str(), value["sensorFaults"][0]["occurredAt"].as<const char*>());
}
void testPreviousBootNeverReceivesCurrentBootAnchor() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_INIT, true, 1, 500, 0);
    auto value = decoded(journal, 2, 2000);
    TEST_ASSERT_TRUE(value["sensorFaults"][0]["occurredAt"].isNull());
    TEST_ASSERT_NOT_NULL(std::strstr(value["sensorFaults"][0]["detail"].as<const char*>(), "UTC unavailable"));
}
void testResolvedTimeSurvivesReboot() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_INIT, true, 1, 500, EPOCH - 10000);
    auto value = decoded(journal, 2, 2000);
    TEST_ASSERT_EQUAL_STRING(utcTimestamp(EPOCH - 10000).c_str(), value["sensorFaults"][0]["occurredAt"].as<const char*>());
}
void testRecoveryCannotPredateUnknownTimeActiveAcknowledgement() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_INIT, true, 1, 500, 0);
    observe(journal, Fault::ADXL_INIT, false, 2, 500, EPOCH - 1000);
    TEST_ASSERT_TRUE(acknowledgeHead(journal, 2, 2000, EPOCH));
    TEST_ASSERT_TRUE(decoded(journal, 2)["sensorFaults"][0]["occurredAt"].isNull());
    TEST_ASSERT_TRUE(validJournal(journal));
}
void testStuckDigitalAudioIsNotValidTelemetry() {
    std::int32_t silent[] = {0, 0, 0, 0};
    std::int32_t stuck[] = {-1, -1, -1, -1};
    std::int32_t quiet[] = {0, 1, 0, -1};
    TEST_ASSERT_FALSE(hasPcmVariation(silent, 4));
    TEST_ASSERT_FALSE(hasPcmVariation(stuck, 4));
    TEST_ASSERT_FALSE(hasPcmVariation(nullptr, 4));
    TEST_ASSERT_TRUE(hasPcmVariation(quiet, 4));
}
void testLongUnanchoredUptimeUsesFull64BitClock() {
    auto journal = emptyJournal(); observe(journal, Fault::ADXL_INIT, true, 1, 500, 0);
    const std::uint64_t now = 0x100000000ULL + 1500ULL;
    auto value = decoded(journal, 1, now);
    TEST_ASSERT_EQUAL_STRING(utcTimestamp(EPOCH - static_cast<std::int64_t>(now - 500)).c_str(),
                            value["sensorFaults"][0]["occurredAt"].as<const char*>());
    TEST_ASSERT_TRUE(decoded(journal, 1, 100)["sensorFaults"][0]["occurredAt"].isNull());
    TEST_ASSERT_TRUE(decoded(journal, 2, now)["sensorFaults"][0]["occurredAt"].isNull());
    auto late = emptyJournal(); observe(late, Fault::ADXL_INIT, true, 1, now, 0);
    TEST_ASSERT_EQUAL_UINT64(now, late.latest[0].monotonicMs);
    TEST_ASSERT_EQUAL_STRING(utcTimestamp(EPOCH - 1000).c_str(),
                            decoded(late, 1, now + 1000)["sensorFaults"][0]["occurredAt"].as<const char*>());
}
void testPeriodicSnapshotRetainsKnownCurrentStates() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_INIT, true, 1, 500, EPOCH - 2000);
    observe(journal, Fault::I2S_INIT, false, 1, 500, EPOCH - 2000);
    acknowledgeHead(journal); acknowledgeHead(journal);
    auto value = decoded(journal);
    TEST_ASSERT_EQUAL(2, value["sensorFaults"].size());
    TEST_ASSERT_EQUAL_STRING("active", value["sensorFaults"][0]["status"].as<const char*>());
    TEST_ASSERT_EQUAL_STRING("recovered", value["sensorFaults"][1]["status"].as<const char*>());
}
void testAckRequiresDeviceAndExactReportTime() {
    JsonDocument response;
    response["deviceId"] = "DEV-01";
    response["lastReceivedAt"] = utcTimestamp(EPOCH);
    response["activeSensorFaults"].to<JsonArray>();
    std::string text; serializeJson(response, text);
    TEST_ASSERT_TRUE(accepted(200, text.c_str(), "DEV-01", EPOCH));
    TEST_ASSERT_FALSE(accepted(200, text.c_str(), "DEV-02", EPOCH));
    TEST_ASSERT_FALSE(accepted(200, text.c_str(), "DEV-01", EPOCH + 1));
    TEST_ASSERT_FALSE(accepted(201, text.c_str(), "DEV-01", EPOCH));
    TEST_ASSERT_FALSE(accepted(204, "", "DEV-01", EPOCH));
    TEST_ASSERT_FALSE(accepted(200, "{", "DEV-01", EPOCH));
    TEST_ASSERT_FALSE(accepted(200, "{\"ok\":true}", "DEV-01", EPOCH));
}
void testReportSchedulePeriodicChangeAndMinimumInterval() {
    ReportSchedule schedule;
    TEST_ASSERT_TRUE(schedule.due(0, false, false));
    schedule.completed(0, true);
    TEST_ASSERT_FALSE(schedule.due(4999, true, true));
    TEST_ASSERT_TRUE(schedule.due(5000, true, false));
    TEST_ASSERT_FALSE(schedule.due(29999, false, false));
    TEST_ASSERT_TRUE(schedule.due(30000, false, false));
}
void testFailedReportingHasCappedBackoffAndRecovers() {
    ReportSchedule schedule;
    schedule.completed(0, false);
    TEST_ASSERT_FALSE(schedule.due(9999, true, true));
    TEST_ASSERT_TRUE(schedule.due(10000, true, true));
    schedule.completed(10000, false);
    TEST_ASSERT_FALSE(schedule.due(29999, true, true));
    schedule.completed(30000, false); schedule.completed(70000, false);
    TEST_ASSERT_FALSE(schedule.due(129999, true, true));
    TEST_ASSERT_TRUE(schedule.due(130000, true, true));
    schedule.completed(130000, true);
    TEST_ASSERT_TRUE(schedule.due(135000, false, true));
}
void testScheduleSurvivesMillisWrap() {
    ReportSchedule schedule; schedule.completed(0xFFFFFF00U, true);
    TEST_ASSERT_FALSE(schedule.due(0x100U, true, true));
    TEST_ASSERT_TRUE(schedule.due(0xFFFFFF00U + 30000U, false, false));
}
void testRemoteIntervalNeverDelaysFaultOrRetry() {
    ReportSchedule schedule;
    TEST_ASSERT_TRUE(schedule.setInterval(300000)); schedule.completed(0, true);
    TEST_ASSERT_FALSE(schedule.due(299999, false, false));
    TEST_ASSERT_TRUE(schedule.due(300000, false, false));
    TEST_ASSERT_TRUE(schedule.due(5000, true, false));
    TEST_ASSERT_TRUE(schedule.due(5000, false, true));
    TEST_ASSERT_TRUE(schedule.setInterval(10000));
    TEST_ASSERT_FALSE(schedule.due(9999, false, false));
    TEST_ASSERT_TRUE(schedule.due(10000, false, false));
    TEST_ASSERT_FALSE(schedule.setInterval(9999)); TEST_ASSERT_FALSE(schedule.setInterval(300001));
    schedule.completed(10000, false); schedule.setInterval(300000);
    TEST_ASSERT_FALSE(schedule.due(19999, true, true));
    TEST_ASSERT_TRUE(schedule.due(20000, true, true));
}
void testMetricChangesAreDebounced() {
    auto previous = metrics(); auto current = previous;
    current.rssiDbm -= 4; TEST_ASSERT_FALSE(metricsChanged(previous, current));
    current.rssiDbm -= 1; TEST_ASSERT_TRUE(metricsChanged(previous, current));
    current = previous; current.queuedRecords += 1250;
    TEST_ASSERT_TRUE(metricsChanged(previous, current));
    current = previous; ++current.rebootCount;
    TEST_ASSERT_TRUE(metricsChanged(previous, current));
}
void testSensorInitRetriesAreBoundedAndSpaced() {
    SensorRetry retry;
    TEST_ASSERT_TRUE(retry.due(0)); retry.attempted(0, false);
    TEST_ASSERT_FALSE(retry.due(4999)); TEST_ASSERT_TRUE(retry.due(5000));
    retry.attempted(5000, false);
    TEST_ASSERT_FALSE(retry.due(14999)); TEST_ASSERT_TRUE(retry.due(15000));
    retry.attempted(15000, false);
    TEST_ASSERT_FALSE(retry.due(1000000)); TEST_ASSERT_FALSE(retry.ready());
}
void testRuntimeRecoveryUsesSameBoundedBudget() {
    SensorRetry retry; retry.attempted(0, true);
    TEST_ASSERT_TRUE(retry.ready()); TEST_ASSERT_FALSE(retry.due(1000000));
    retry.failed(1000); TEST_ASSERT_FALSE(retry.ready());
    TEST_ASSERT_FALSE(retry.due(5999)); TEST_ASSERT_TRUE(retry.due(6000));
    retry.attempted(6000, true); retry.failed(7000);
    TEST_ASSERT_TRUE(retry.due(17000)); retry.attempted(17000, true); retry.failed(18000);
    TEST_ASSERT_FALSE(retry.due(1000000));
}
void testMultipleReplayBatchesCannotOvertakeFault() {
    auto journal = emptyJournal(); observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH);
    for (unsigned batch = 0; batch < 4; ++batch) {
        const unsigned begin = batch * 4;
        const unsigned end = static_cast<unsigned>(FirmwareLogic::calculateReplayBatchSize(15 - begin, 4)) + begin;
        for (unsigned index = begin; index < end; ++index) {
            auto order = dispatchOrder(journal, true, true, EPOCH - 15000 + index * 1000,
                                       1, 2000, EPOCH + 20000);
            TEST_ASSERT_TRUE(order.telemetry); TEST_ASSERT_FALSE(order.transition);
        }
        // A storage/HTTP retry or restart leaves the same journal head pending.
        Journal restarted = journal; TEST_ASSERT_TRUE(validJournal(restarted));
        TEST_ASSERT_EQUAL(1, restarted.count);
    }
    TEST_ASSERT_TRUE(dispatchOrder(journal, false, false, 0, 1, 2000, EPOCH + 20000).transition);
}
void testTimestampMergeEqualityRecoveryAndFailedAck() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH);
    observe(journal, Fault::ADXL_CHANNEL, false, 1, 1500, EPOCH + 5000);
    TEST_ASSERT_TRUE(dispatchOrder(journal, true, true, EPOCH, 1, 2000, EPOCH + 20000).telemetry);
    auto later = dispatchOrder(journal, true, true, EPOCH + 1000, 1, 2000, EPOCH + 20000);
    TEST_ASSERT_TRUE(later.transition); TEST_ASSERT_FALSE(later.telemetry);
    auto failedMarker = journal; acknowledgeHead(failedMarker, 1, 2000, EPOCH + 20000);
    TEST_ASSERT_FALSE(dispatchOrder(journal, true, true, EPOCH + 1000, 1, 2000, EPOCH + 20000).telemetry);
    journal = failedMarker;
    TEST_ASSERT_TRUE(dispatchOrder(journal, true, true, EPOCH + 1000, 1, 2000, EPOCH + 20000).telemetry);
    TEST_ASSERT_FALSE(dispatchOrder(journal, true, true, EPOCH + 6000, 1, 2000, EPOCH + 20000).telemetry);
    acknowledgeHead(journal, 1, 2000, EPOCH + 20000);
    TEST_ASSERT_TRUE(dispatchOrder(journal, true, true, EPOCH + 6000, 1, 2000, EPOCH + 20000).telemetry);
    TEST_ASSERT_EQUAL_INT64(EPOCH + 5000, acknowledgedBoundary(journal));
}
void testUnknownHeadAndMissingClockCannotAdvanceFault() {
    auto journal = emptyJournal(); observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH);
    auto order = dispatchOrder(journal, true, false, 0, 1, 2000, EPOCH);
    TEST_ASSERT_FALSE(order.transition); TEST_ASSERT_FALSE(order.telemetry);
    TEST_ASSERT_FALSE(dispatchOrder(journal, false, false, 0, 1, 2000, 0).transition);
    auto unresolved = emptyJournal(); observe(unresolved, Fault::ADXL_CHANNEL, true, 1, 500, 0);
    TEST_ASSERT_TRUE(dispatchOrder(unresolved, true, true, EPOCH - 1000, 2, 2000, EPOCH).telemetry);
    journal.crc ^= 1;
    TEST_ASSERT_FALSE(dispatchOrder(journal, false, false, 0, 1, 2000, EPOCH).transition);
}
void testMetricsOnlyDoesNotSendOrConsumeDeferredFault() {
    auto journal = emptyJournal(); observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH);
    const auto crc = journal.crc;
    JsonDocument value;
    TEST_ASSERT_FALSE(deserializeJson(value, payload(journal, metrics(), 1, 2000, EPOCH, false)));
    TEST_ASSERT_EQUAL(0, value["sensorFaults"].size());
    TEST_ASSERT_EQUAL(-61, value["rssiDbm"].as<int>());
    TEST_ASSERT_EQUAL(crc, journal.crc); TEST_ASSERT_EQUAL(1, journal.count);
    acknowledgeHead(journal, 1, 2000, EPOCH);
    auto pendingReplay = dispatchOrder(journal, true, true, EPOCH + 1000, 1, 2000, EPOCH + 2000);
    TEST_ASSERT_TRUE(pendingReplay.telemetry); TEST_ASSERT_FALSE(pendingReplay.snapshot);
    TEST_ASSERT_TRUE(dispatchOrder(journal, false, false, 0, 1, 2000, EPOCH + 2000).snapshot);
}
void testDifferentFaultCodesKeepSharedBoundary() {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH + 1000);
    observe(journal, Fault::I2S_CHANNEL, true, 1, 400, EPOCH);
    acknowledgeHead(journal, 1, 2000, EPOCH + 2000);
    TEST_ASSERT_TRUE(dispatchOrder(journal, true, true, EPOCH + 500, 1, 2000, EPOCH + 2000).telemetry);
    TEST_ASSERT_TRUE(dispatchOrder(journal, true, true, EPOCH + 1001, 1, 2000, EPOCH + 2000).transition);
}

// Deterministic host replay driver uses the SAME production dispatcher and
// payload builder as main.cpp. Python submits every emitted request to HTTP.
int emitReplayFixture(bool brokenHealthFirst) {
    auto journal = emptyJournal();
    observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH);
    observe(journal, Fault::ADXL_CHANNEL, false, 1, 1500, EPOCH + 5000);
    unsigned cursor = 0;
    for (unsigned batch = 0; batch < 40 && (cursor < 30 || journal.count); ++batch) {
        const auto reportTime = EPOCH + 60000 + batch * 5000;
        const auto pointTime = cursor < 15 ? EPOCH - 15000 + cursor * 1000 :
                              EPOCH + 6000 + (cursor - 15) * 1000;
        auto order = dispatchOrder(journal, cursor < 30, true, pointTime, 1, 2000, reportTime);
        if (brokenHealthFirst) order.transition = journal.count != 0;
        const bool includeFaults = order.transition || order.snapshot;
        std::printf("health\t%u\t%s\n", batch,
                    payload(journal, metrics(), 1, 2000, reportTime, includeFaults).c_str());
        if (order.transition) acknowledgeHead(journal, 1, 2000, reportTime);
        const auto limit = FirmwareLogic::calculateReplayBatchSize(30 - cursor, 4);
        for (unsigned item = 0; item < limit; ++item) {
            const auto time = cursor < 15 ? EPOCH - 15000 + cursor * 1000 :
                             EPOCH + 6000 + (cursor - 15) * 1000;
            auto replay = dispatchOrder(journal, true, true, time, 1, 2000, reportTime);
            if (!brokenHealthFirst && !replay.telemetry) break;
            FirmwareLogic::CanonicalTelemetry telemetry;
            telemetry.sequence = cursor + 1;
            telemetry.vibrationRmsRaw = 1.0f; telemetry.vibrationPeakHz = 1037.11f;
            telemetry.acousticRmsRaw = 0.007019f; telemetry.acousticPeakHz = 216.4f;
            std::printf("telemetry\t%u\t%s\n", batch,
                FirmwareLogic::buildCanonicalTelemetryPayload(utcTimestamp(time).c_str(),
                    "SITE-01", "SITE-01-MOT-02", "DEV-01-MOT-02", telemetry).c_str());
            ++cursor;
        }
    }
    return cursor == 30 && journal.count == 0 ? 0 : 1;
}
} // namespace

void setUp() {}
void tearDown() {}
int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--emit-replay-fixture") == 0) return emitReplayFixture(false);
    if (argc == 2 && std::strcmp(argv[1], "--emit-broken-replay-fixture") == 0) return emitReplayFixture(true);
    if (argc == 3 && std::strcmp(argv[1], "--validate-backend-ack") == 0)
        return accepted(200, argv[2], "DEV-01-MOT-02", EPOCH) ? 0 : 1;
    if (argc == 2 && std::strcmp(argv[1], "--emit-fixture") == 0) {
        auto journal = emptyJournal();
        observe(journal, Fault::ADXL_CHANNEL, true, 1, 500, EPOCH - 1000);
        observe(journal, Fault::ADXL_CHANNEL, false, 1, 1500, EPOCH);
        std::puts(payload(journal, metrics(), 1, 2000, EPOCH).c_str());
        acknowledgeHead(journal, 1, 2000, EPOCH);
        std::puts(payload(journal, metrics(), 1, 3000, EPOCH + 1000).c_str());
        FirmwareLogic::CanonicalTelemetry telemetry;
        telemetry.sequence = 1;
        telemetry.vibrationRmsRaw = 0.08f;
        telemetry.vibrationPeakHz = 45.0f;
        telemetry.acousticRmsRaw = 0.01f;
        telemetry.acousticPeakHz = 500.0f;
        std::puts(FirmwareLogic::buildCanonicalTelemetryPayload(
            utcTimestamp(EPOCH).c_str(), "SITE-01", "SITE-01-MOT-02",
            "DEV-01-MOT-02", telemetry).c_str());
        return 0;
    }
    UNITY_BEGIN();
    RUN_TEST(testMetricsFollowBackendContract);
    RUN_TEST(testPayloadRejectsInvalidMetrics);
    RUN_TEST(testPayloadRequiresKnownReportTime);
    RUN_TEST(testJournalRoundTripAndCorruption);
    RUN_TEST(testRepeatedFaultDoesNotRewriteOrChangeOccurrence);
    RUN_TEST(testActiveRecoveryRemainsOrderedUntilAcknowledged);
    RUN_TEST(testAckLossOrFailedMarkerCannotAdvanceSuccessor);
    RUN_TEST(testCapacityNeverOverwritesOldestFault);
    RUN_TEST(testInvalidObservationDoesNotMutateJournal);
    RUN_TEST(testSameBootResolvesActualMonotonicTime);
    RUN_TEST(testPreviousBootNeverReceivesCurrentBootAnchor);
    RUN_TEST(testResolvedTimeSurvivesReboot);
    RUN_TEST(testRecoveryCannotPredateUnknownTimeActiveAcknowledgement);
    RUN_TEST(testStuckDigitalAudioIsNotValidTelemetry);
    RUN_TEST(testLongUnanchoredUptimeUsesFull64BitClock);
    RUN_TEST(testPeriodicSnapshotRetainsKnownCurrentStates);
    RUN_TEST(testAckRequiresDeviceAndExactReportTime);
    RUN_TEST(testReportSchedulePeriodicChangeAndMinimumInterval);
    RUN_TEST(testFailedReportingHasCappedBackoffAndRecovers);
    RUN_TEST(testScheduleSurvivesMillisWrap);
    RUN_TEST(testRemoteIntervalNeverDelaysFaultOrRetry);
    RUN_TEST(testMetricChangesAreDebounced);
    RUN_TEST(testSensorInitRetriesAreBoundedAndSpaced);
    RUN_TEST(testRuntimeRecoveryUsesSameBoundedBudget);
    RUN_TEST(testMultipleReplayBatchesCannotOvertakeFault);
    RUN_TEST(testTimestampMergeEqualityRecoveryAndFailedAck);
    RUN_TEST(testUnknownHeadAndMissingClockCannotAdvanceFault);
    RUN_TEST(testMetricsOnlyDoesNotSendOrConsumeDeferredFault);
    RUN_TEST(testDifferentFaultCodesKeepSharedBoundary);
    return UNITY_END();
}
