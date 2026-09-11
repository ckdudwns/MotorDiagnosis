#include <unity.h>
#include <ArduinoJson.h>
#include "communication_quality.h"
#include <cstring>
#include <cstdlib>
#include <iostream>
#include <iterator>

using namespace CommunicationQuality;
namespace {
const Identity ID{"DEV-01-MOT-02", "SITE-01", "SITE-01-MOT-02"};
const char* BOOT = "00000000000000000000000000000001";
constexpr std::int64_t EPOCH = 1788696000000LL;
void start(Collector& collector, std::int64_t epoch = EPOCH) {
    TEST_ASSERT_TRUE(collector.begin(ID, BOOT, 0, epoch, 24999, 10));
    collector.sampleBuffer(3, 10);
}
std::string ack(const Window& window, bool duplicate = false, const char* disposition = "stored") {
    JsonDocument doc;
    doc["accepted"] = true; doc["deviceId"] = window.deviceId;
    doc["bootId"] = window.bootId; doc["windowId"] = window.windowId;
    doc["duplicate"] = duplicate; doc["disposition"] = disposition;
    std::string result; serializeJson(doc, result); return result;
}
struct DropEvidence {
    unsigned events = 0, unique = 0, actual = 0;
    std::uint32_t lastIndex = 0;
    char lastBoot[33] = {};
    bool hasLast = false;
};
void pendingFull(DropEvidence& evidence, const char* bootId, std::uint32_t index, bool owned) {
    ++evidence.events;
    if (!evidence.hasLast || evidence.lastIndex != index ||
        std::strcmp(evidence.lastBoot, bootId) != 0) {
        ++evidence.unique; evidence.lastIndex = index;
        std::strncpy(evidence.lastBoot, bootId, sizeof(evidence.lastBoot) - 1);
        evidence.hasLast = true;
    }
    if (!owned) ++evidence.actual;
}
void testDropClassificationSeparatesPressureFromOwnershipLoss() {
    DropEvidence evidence;
    pendingFull(evidence, BOOT, 7, true);
    pendingFull(evidence, BOOT, 7, true);
    pendingFull(evidence, BOOT, 7, true);
    TEST_ASSERT_EQUAL_UINT(3, evidence.events);
    TEST_ASSERT_EQUAL_UINT(1, evidence.unique);
    TEST_ASSERT_EQUAL_UINT(0, evidence.actual);
    pendingFull(evidence, BOOT, 8, false);
    TEST_ASSERT_EQUAL_UINT(2, evidence.unique);
    TEST_ASSERT_EQUAL_UINT(1, evidence.actual);
}
struct Storage { Window value; bool writeSuccess = true, eraseSuccess = true, present = false; unsigned writes = 0, erases = 0; };
bool persist(const Window& window, void* raw) {
    auto& storage = *static_cast<Storage*>(raw);
    storage.writes++; storage.value = window; storage.present = true;
    return storage.writeSuccess && validWindow(storage.value, ID);
}
bool erase(void* raw) {
    auto& storage = *static_cast<Storage*>(raw);
    storage.erases++;
    if (!storage.eraseSuccess) return false;
    storage.present = false;
    return true;
}
void testNoClockOrScopeCannotCreateInventedWindow() {
    Collector c; Window w;
    TEST_ASSERT_FALSE(c.ready(60000));
    TEST_ASSERT_FALSE(c.begin(ID, BOOT, 0, 0, 24999, 0));
    TEST_ASSERT_FALSE(c.begin({"BAD/ID", ID.siteId, ID.assetId}, BOOT, 0, EPOCH, 24999, 0));
    TEST_ASSERT_FALSE(c.begin(ID, "bad", 0, EPOCH, 24999, 0));
    TEST_ASSERT_FALSE(c.begin(ID, BOOT, 0, EPOCH, 0, 0));
    TEST_ASSERT_FALSE(c.freeze(60000, w));
}
void testMonotonicMinuteIncludesTrueStopAndIdleSamples() {
    Collector c; start(c); Window w;
    c.sampleBuffer(0, 10); c.sampleWifi(false);
    TEST_ASSERT_FALSE(c.ready(59999));
    TEST_ASSERT_TRUE(c.freeze(60000, w));
    TEST_ASSERT_EQUAL_UINT64(0, w.metrics[ATTEMPTS]);
    TEST_ASSERT_EQUAL_UINT64(0, w.metrics[BUFFER_DEPTH_LAST]);
    TEST_ASSERT_EQUAL_UINT64(1, w.metrics[OFFLINE_SAMPLES]);
    TEST_ASSERT_EQUAL_INT64(EPOCH + 60000, w.endedAtMs);
    TEST_ASSERT_TRUE(validWindow(w, ID));
}
void testAttemptsAndFailureCategoriesAreMutuallyExclusive() {
    Collector c; start(c); Window w;
    c.attempt(1, false, Outcome::TRANSPORT_FAILURE, 5000);
    c.attempt(1, true, Outcome::RETRYABLE_RESPONSE, 5000);
    c.attempt(1, true, Outcome::CONFIGURATION_FAILURE, 5000);
    c.attempt(1, true, Outcome::PACKET_REJECT, 5000);
    c.attempt(2, false, Outcome::ACK, 100);
    c.attempt(3, false, Outcome::ACK, 300);
    TEST_ASSERT_TRUE(c.freeze(60000, w));
    TEST_ASSERT_EQUAL_UINT64(6, w.metrics[ATTEMPTS]);
    TEST_ASSERT_EQUAL_UINT64(3, w.metrics[RETRIES]);
    TEST_ASSERT_EQUAL_UINT64(3, w.metrics[REPLAY_ATTEMPTS]);
    TEST_ASSERT_EQUAL_UINT64(2, w.metrics[ACKNOWLEDGED]);
    for (auto counter : {TRANSPORT_FAILURES, RETRYABLE_RESPONSES, CONFIGURATION_FAILURES, REJECTED_PACKETS}) TEST_ASSERT_EQUAL_UINT64(1, w.metrics[counter]);
    TEST_ASSERT_EQUAL_UINT64(400, w.metrics[ACK_LATENCY_TOTAL_MS]);
    TEST_ASSERT_EQUAL_UINT64(300, w.metrics[ACK_LATENCY_MAX_MS]);
}
void testFirstReplayIsNotAutomaticallyARepeatedAttempt() {
    Collector c; start(c); Window w;
    c.attempt(1, true, Outcome::ACK, 0);
    c.attempt(2, true, Outcome::ACK, 1);
    TEST_ASSERT_TRUE(c.freeze(60000, w));
    TEST_ASSERT_EQUAL_UINT64(2, w.metrics[REPLAY_ATTEMPTS]);
    TEST_ASSERT_EQUAL_UINT64(0, w.metrics[RETRIES]);
}
void testRetriesCrossWindowsButNotBoots() {
    Collector c; start(c); Window w;
    c.attempt(1, true, Outcome::TRANSPORT_FAILURE, 1);
    c.freeze(60000, w);
    c.sampleBuffer(1, 10); c.attempt(1, true, Outcome::ACK, 1);
    TEST_ASSERT_TRUE(c.freeze(120000, w));
    TEST_ASSERT_EQUAL_UINT64(1, w.metrics[RETRIES]);
    Collector rebooted; start(rebooted);
    rebooted.attempt(1, true, Outcome::ACK, 1);
    rebooted.freeze(60000, w);
    TEST_ASSERT_EQUAL_UINT64(0, w.metrics[RETRIES]);
}
void testDropsExcludeHistoricalTotalAndDoNotUnderflowOnReset() {
    Collector c; start(c); Window w;
    c.sampleBuffer(5, 13); c.sampleBuffer(4, 13); c.sampleBuffer(4, 0);
    TEST_ASSERT_TRUE(c.freeze(60000, w));
    TEST_ASSERT_EQUAL_UINT64(3, w.metrics[BUFFER_DROPPED]);
    TEST_ASSERT_EQUAL_UINT64(5, w.metrics[BUFFER_DEPTH_MAX]);
    TEST_ASSERT_EQUAL_UINT64(16, w.metrics[BUFFER_DEPTH_SUM]);
    TEST_ASSERT_EQUAL_UINT64(4, w.metrics[BUFFER_SAMPLES]);
}
void testEveryStoredByteCorruptionAndScopeMismatchIsRejected() {
    Collector c; start(c); Window w; c.freeze(60000, w);
    for (std::size_t i = 0; i < sizeof(w); ++i) {
        Window bad = w; reinterpret_cast<unsigned char*>(&bad)[i] ^= 0x20;
        TEST_ASSERT_FALSE(validWindow(bad, ID));
    }
    TEST_ASSERT_FALSE(validWindow(w, {ID.deviceId, "SITE-OTHER", ID.assetId}));
}
void testLargeUptimeDoesNotWrapAtMillisBoundary() {
    Collector c; Window w;
    const std::uint64_t beginning = (1ULL << 32) - 20;
    c.begin(ID, BOOT, beginning, EPOCH, 24999, 0); c.sampleBuffer(1, 0);
    TEST_ASSERT_TRUE(c.freeze(beginning + 60000, w));
    TEST_ASSERT_EQUAL_UINT64(beginning + 60000, w.endUptimeMs);
    TEST_ASSERT_EQUAL_INT64(EPOCH + 60000, w.endedAtMs);
}
void testOverflowDoesNotPublishFalseNumbers() {
    Collector c; start(c); Window w;
    c.attempt(1, false, Outcome::ACK, MAX_INTEGER);
    c.attempt(2, false, Outcome::ACK, 1);
    TEST_ASSERT_TRUE(c.overflowed());
    TEST_ASSERT_FALSE(c.freeze(60000, w));
}
void testPendingWindowIsImmutableWhileOfflineAccumulatorContinues() {
    Collector c; start(c); Outbox box; Storage storage;
    c.attempt(1, false, Outcome::TRANSPORT_FAILURE, 100);
    TEST_ASSERT_TRUE(box.capture(c, 60000));
    const auto frozen = payload(box.window());
    c.sampleBuffer(4, 11); c.attempt(1, true, Outcome::ACK, 200);
    TEST_ASSERT_FALSE(box.capture(c, 300000));
    TEST_ASSERT_EQUAL_STRING(frozen.c_str(), payload(box.window()).c_str());
    TEST_ASSERT_TRUE(box.checkpoint(persist, &storage));
    const auto response = ack(box.window());
    TEST_ASSERT_TRUE(box.acknowledge(201, response.c_str(), erase, &storage));
    TEST_ASSERT_TRUE(box.capture(c, 300000));
    TEST_ASSERT_EQUAL_UINT64(60000, box.window().startUptimeMs);
    TEST_ASSERT_EQUAL_UINT64(300000, box.window().endUptimeMs);
    TEST_ASSERT_EQUAL_UINT64(1, box.window().metrics[ATTEMPTS]);
    TEST_ASSERT_EQUAL_UINT64(1, box.window().metrics[BUFFER_DROPPED]);
}
void testCheckpointFailureCannotSendAndRetryUsesSameWindow() {
    Collector c; start(c); Outbox box; Storage storage;
    box.capture(c, 60000); const auto original = payload(box.window());
    storage.writeSuccess = false;
    TEST_ASSERT_FALSE(box.checkpoint(persist, &storage));
    TEST_ASSERT_FALSE(box.sendable());
    TEST_ASSERT_FALSE(box.acknowledge(201, ack(box.window()).c_str(), erase, &storage));
    TEST_ASSERT_EQUAL(0, storage.erases);
    storage.writeSuccess = true;
    TEST_ASSERT_TRUE(box.checkpoint(persist, &storage));
    TEST_ASSERT_TRUE(box.checkpoint(persist, &storage));
    TEST_ASSERT_EQUAL(2, storage.writes);
    TEST_ASSERT_EQUAL_STRING(original.c_str(), payload(box.window()).c_str());
}
void testCrashAfterCheckpointOrServerAckRestoresExactIdentity() {
    Collector c; start(c); Outbox box, rebooted; Storage storage;
    box.capture(c, 60000); box.checkpoint(persist, &storage);
    TEST_ASSERT_TRUE(rebooted.restore(storage.value, ID));
    TEST_ASSERT_EQUAL_STRING(payload(box.window()).c_str(), payload(rebooted.window()).c_str());
    TEST_ASSERT_TRUE(rebooted.acknowledge(200, ack(rebooted.window(), true).c_str(), erase, &storage));
    TEST_ASSERT_FALSE(rebooted.pending());
}
void testAmbiguousWriteCanRestoreAndFailedRemovalRemainsRetryable() {
    Collector c; start(c); Outbox box, rebooted; Storage storage;
    box.capture(c, 60000); storage.writeSuccess = false; box.checkpoint(persist, &storage);
    TEST_ASSERT_TRUE(rebooted.restore(storage.value, ID));
    storage.eraseSuccess = false;
    TEST_ASSERT_FALSE(rebooted.acknowledge(201, ack(rebooted.window()).c_str(), erase, &storage));
    TEST_ASSERT_TRUE(rebooted.pending());
    storage.eraseSuccess = true;
    TEST_ASSERT_TRUE(rebooted.acknowledge(200, ack(rebooted.window(), true).c_str(), erase, &storage));
}
void testAckMustMatchDeviceBootWindowDispositionAndStatus() {
    Collector c; start(c); Window w; c.freeze(60000, w);
    TEST_ASSERT_TRUE(accepted(201, ack(w).c_str(), w));
    TEST_ASSERT_TRUE(accepted(200, ack(w, true).c_str(), w));
    TEST_ASSERT_TRUE(accepted(200, ack(w, false, "expired").c_str(), w));
    TEST_ASSERT_FALSE(accepted(201, ack(w, false, "expired").c_str(), w));
    TEST_ASSERT_FALSE(accepted(200, ack(w).c_str(), w));
    TEST_ASSERT_FALSE(accepted(201, ack(w, true).c_str(), w));
    TEST_ASSERT_FALSE(accepted(204, ack(w).c_str(), w));
    for (const char* key : {"accepted", "deviceId", "bootId", "windowId", "duplicate", "disposition"}) {
        JsonDocument bad; deserializeJson(bad, ack(w)); bad[key] = "wrong";
        std::string raw; serializeJson(bad, raw);
        TEST_ASSERT_FALSE(accepted(201, raw.c_str(), w));
    }
    TEST_ASSERT_FALSE(accepted(201, "{", w));
    TEST_ASSERT_FALSE(accepted(201, std::string(1025, 'x').c_str(), w));
}
void testScheduleIsBoundedAndMillisWrapSafe() {
    Schedule schedule; std::uint32_t now = 0xfffffff0;
    TEST_ASSERT_TRUE(schedule.due(now));
    for (unsigned wait : {5000U, 10000U, 20000U, 40000U, 60000U, 60000U}) {
        schedule.completed(now, false);
        TEST_ASSERT_FALSE(schedule.due(now + wait - 1));
        now += wait; TEST_ASSERT_TRUE(schedule.due(now));
    }
    schedule.completed(now, true);
    TEST_ASSERT_FALSE(schedule.due(now + 59999));
    TEST_ASSERT_TRUE(schedule.due(now + 60000));
}
Window fixtureWindow(std::int64_t epoch) {
    Collector collector;
    collector.begin(ID, BOOT, 0, epoch, 24999, 10);
    collector.sampleBuffer(3, 10); collector.sampleWifi(false);
    collector.attempt(1, false, Outcome::TRANSPORT_FAILURE, 1000);
    collector.attempt(1, true, Outcome::ACK, 125);
    collector.sampleBuffer(2, 11); collector.sampleWifi(true);
    Window window; collector.freeze(60000, window); return window;
}
} // namespace
void setUp() {}
void tearDown() {}
int main(int argc, char** argv) {
    if (argc == 3 && std::strcmp(argv[1], "--payload") == 0) {
        std::cout << payload(fixtureWindow(std::strtoll(argv[2], nullptr, 10))) << '\n'; return 0;
    }
    if (argc == 4 && std::strcmp(argv[1], "--ack") == 0) {
        const std::string input{std::istreambuf_iterator<char>(std::cin), std::istreambuf_iterator<char>()};
        return accepted(std::atoi(argv[3]), input.c_str(), fixtureWindow(std::strtoll(argv[2], nullptr, 10))) ? 0 : 2;
    }
    UNITY_BEGIN();
    RUN_TEST(testNoClockOrScopeCannotCreateInventedWindow);
    RUN_TEST(testDropClassificationSeparatesPressureFromOwnershipLoss);
    RUN_TEST(testMonotonicMinuteIncludesTrueStopAndIdleSamples);
    RUN_TEST(testAttemptsAndFailureCategoriesAreMutuallyExclusive);
    RUN_TEST(testFirstReplayIsNotAutomaticallyARepeatedAttempt);
    RUN_TEST(testRetriesCrossWindowsButNotBoots);
    RUN_TEST(testDropsExcludeHistoricalTotalAndDoNotUnderflowOnReset);
    RUN_TEST(testEveryStoredByteCorruptionAndScopeMismatchIsRejected);
    RUN_TEST(testLargeUptimeDoesNotWrapAtMillisBoundary);
    RUN_TEST(testOverflowDoesNotPublishFalseNumbers);
    RUN_TEST(testPendingWindowIsImmutableWhileOfflineAccumulatorContinues);
    RUN_TEST(testCheckpointFailureCannotSendAndRetryUsesSameWindow);
    RUN_TEST(testCrashAfterCheckpointOrServerAckRestoresExactIdentity);
    RUN_TEST(testAmbiguousWriteCanRestoreAndFailedRemovalRemainsRetryable);
    RUN_TEST(testAckMustMatchDeviceBootWindowDispositionAndStatus);
    RUN_TEST(testScheduleIsBoundedAndMillisWrapSafe);
    return UNITY_END();
}
