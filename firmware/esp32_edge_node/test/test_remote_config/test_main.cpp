#include <unity.h>
#include <ArduinoJson.h>
#include "remote_config.h"
#include <cstring>
#include <iostream>
#include <iterator>
#include <string>

using namespace RemoteConfig;
namespace {
const Identity ID{"DEV-01-MOT-02", "SITE-01", "SITE-01-MOT-02"};
const char* COMMAND = "0123456789abcdef0123456789abcdef";
struct Storage {
    Identity identity = ID;
    Blob durable;
    unsigned writes = 0;
    bool verifySuccess = true;
};
bool persist(const Blob& value, void* context) {
    auto& storage = *static_cast<Storage*>(context);
    storage.writes++;
    storage.durable = value;
    return storage.verifySuccess && validBlob(storage.durable, storage.identity);
}
JsonDocument command(unsigned version = 1, unsigned interval = 6000, unsigned batch = 2, const Identity& identity = ID) {
    JsonDocument doc;
    doc["schemaVersion"] = 1;
    doc["deviceId"] = identity.deviceId;
    doc["siteId"] = identity.siteId;
    doc["assetId"] = identity.assetId;
    doc["desired"]["version"] = version;
    doc["desired"]["commandId"] = COMMAND;
    doc["desired"]["settings"]["measurementIntervalMs"] = interval;
    doc["desired"]["settings"]["replayBatchSize"] = batch;
    return doc;
}
std::string text(const JsonDocument& doc) {
    std::string result;
    serializeJson(doc, result);
    return result;
}
Result receive(Controller& controller, Storage& storage, const JsonDocument& doc) {
    return controller.receive(text(doc).c_str(), ID, persist, &storage);
}
void testDefaultAndUnconfiguredDoNotClaimApplied() {
    Controller controller;
    TEST_ASSERT_EQUAL(3000, controller.active().measurementIntervalMs);
    TEST_ASSERT_EQUAL(4, controller.active().replayBatchSize);
    TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(controller.appliedResult().status));
    TEST_ASSERT_TRUE(resultPayload(controller.appliedResult()).empty());
}
void testApplyCommitsWholeVersionSettingsAndIdentity() {
    Controller controller; Storage storage;
    const auto result = receive(controller, storage, command());
    TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(result.status));
    TEST_ASSERT_EQUAL(1, storage.writes);
    TEST_ASSERT_EQUAL(6000, controller.active().measurementIntervalMs);
    TEST_ASSERT_EQUAL(2, controller.active().replayBatchSize);
    TEST_ASSERT_TRUE(validBlob(storage.durable, ID));
    TEST_ASSERT_EQUAL_MEMORY(&storage.durable, &controller.active(), sizeof(Blob));
}
void testRebootRestoresSettingsAndOriginalResult() {
    Controller original, rebooted; Storage storage;
    const auto result = receive(original, storage, command(9, 12000, 1));
    TEST_ASSERT_TRUE(rebooted.restore(storage.durable, ID));
    TEST_ASSERT_EQUAL_STRING(resultPayload(result).c_str(), resultPayload(rebooted.appliedResult()).c_str());
    TEST_ASSERT_EQUAL(9, rebooted.active().version);
}
void testSameCommandAndAckLossDoNotRewriteFlash() {
    Controller controller; Storage storage;
    for (unsigned i = 0; i < 10; ++i) {
        TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(receive(controller, storage, command()).status));
    }
    TEST_ASSERT_EQUAL(1, storage.writes);
}
void testFailedPersistenceDoesNotApplyCandidate() {
    Controller controller; Storage storage;
    receive(controller, storage, command());
    storage.verifySuccess = false;
    const auto result = receive(controller, storage, command(2, 9000, 1));
    TEST_ASSERT_EQUAL(static_cast<int>(Status::FAILED), static_cast<int>(result.status));
    TEST_ASSERT_EQUAL_STRING("storage_failure", result.errorCode.c_str());
    TEST_ASSERT_EQUAL(1, controller.active().version);
    TEST_ASSERT_EQUAL(6000, controller.active().measurementIntervalMs);
    JsonDocument doc;
    TEST_ASSERT_FALSE(deserializeJson(doc, resultPayload(result)));
    TEST_ASSERT_TRUE(doc["settings"].isNull());
}
void testWriteSucceededReadbackFailedThenRebootReconcilesPersistedVersion() {
    Controller controller, rebooted; Storage storage;
    storage.verifySuccess = false;
    const auto result = receive(controller, storage, command());
    TEST_ASSERT_EQUAL(static_cast<int>(Status::FAILED), static_cast<int>(result.status));
    TEST_ASSERT_EQUAL(0, controller.active().version);
    TEST_ASSERT_TRUE(rebooted.restore(storage.durable, ID));
    TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(rebooted.appliedResult().status));
    TEST_ASSERT_EQUAL(6000, rebooted.active().measurementIntervalMs);
}
void testStorageRecoveryRetriesExactVersion() {
    Controller controller; Storage storage;
    storage.verifySuccess = false;
    receive(controller, storage, command());
    storage.verifySuccess = true;
    TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(receive(controller, storage, command()).status));
    TEST_ASSERT_EQUAL(1, controller.active().version);
}
void testEveryCorruptBlobByteIsRejectedWithoutChangingActiveState() {
    Controller controller; Storage storage;
    receive(controller, storage, command());
    for (std::size_t i = 0; i < sizeof(Blob); ++i) {
        Blob bad = storage.durable;
        reinterpret_cast<unsigned char*>(&bad)[i] ^= 0x10;
        TEST_ASSERT_FALSE(controller.restore(bad, ID));
        TEST_ASSERT_EQUAL_MEMORY(&controller.active(), &storage.durable, sizeof(Blob));
    }
}
void testStoredScopeAndIncomingScopeMustMatchLocalIdentity() {
    Controller controller; Storage storage;
    receive(controller, storage, command());
    TEST_ASSERT_FALSE(controller.restore(storage.durable, Identity{"DEV-01-GEN-01", ID.siteId, ID.assetId}));
    for (const char* field : {"deviceId", "siteId", "assetId"}) {
        auto doc = command(2);
        doc[field] = "OTHER";
        TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(receive(controller, storage, doc).status));
    }
    TEST_ASSERT_EQUAL(1, storage.writes);
}
void testIdentifierDotsAndLengthBoundariesApplyAndRestore() {
    for (const std::string& value : {std::string("A"), std::string("SITE.DOT-MOT_01"), std::string("9") + std::string(62, '.')}) {
        Controller controller, rebooted; Storage storage;
        storage.identity = {value.c_str(), value.c_str(), value.c_str()};
        const auto doc = command(1, 6000, 2, storage.identity);
        const auto result = controller.receive(text(doc).c_str(), storage.identity, persist, &storage);
        TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(result.status));
        TEST_ASSERT_EQUAL(1, storage.writes);
        TEST_ASSERT_TRUE(rebooted.restore(storage.durable, storage.identity));
        TEST_ASSERT_EQUAL_STRING(resultPayload(result).c_str(), resultPayload(rebooted.appliedResult()).c_str());
    }
}
void testUnsupportedLocalIdentifiersNeverPersist() {
    for (const std::string& value : {std::string(""), std::string(64, 'X'), std::string("-LEADING"), std::string("_LEADING"),
            std::string(".LEADING"), std::string("lower"), std::string("BAD/ID"), std::string("BAD ID"), std::string("BAD\nID"), std::string("BAD\xc3\x89")}) {
        for (unsigned field = 0; field < 3; ++field) {
            Controller controller; Storage storage;
            if (field == 0) storage.identity.deviceId = value.c_str();
            if (field == 1) storage.identity.siteId = value.c_str();
            if (field == 2) storage.identity.assetId = value.c_str();
            const auto doc = command(1, 6000, 2, storage.identity);
            const auto result = controller.receive(text(doc).c_str(), storage.identity, persist, &storage);
            TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(result.status));
            TEST_ASSERT_EQUAL(0, storage.writes);
            TEST_ASSERT_EQUAL(0, controller.active().version);
        }
    }
}
void testEmbeddedNullInWireIdentityCannotMatchLocalPrefix() {
    Controller controller; Storage storage;
    for (const char* field : {"deviceId", "siteId", "assetId"}) {
        auto doc = command();
        const std::string original = doc[field].as<const char*>();
        doc[field] = "PLACEHOLDER";
        std::string raw = text(doc);
        raw.replace(raw.find("\"PLACEHOLDER\""), std::strlen("\"PLACEHOLDER\""), "\"" + original + "\\u0000OTHER\"");
        TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(controller.receive(raw.c_str(), ID, persist, &storage).status));
    }
    TEST_ASSERT_EQUAL(0, storage.writes);
}
void testOlderVersionAndConflictingEqualVersionNeverApply() {
    Controller controller; Storage storage;
    receive(controller, storage, command(5));
    auto old = receive(controller, storage, command(4));
    TEST_ASSERT_EQUAL_STRING("stale_version", old.errorCode.c_str());
    auto equal = receive(controller, storage, command(5, 9000));
    TEST_ASSERT_EQUAL_STRING("version_conflict", equal.errorCode.c_str());
    auto alteredId = command(5);
    alteredId["desired"]["commandId"] = "ffffffffffffffffffffffffffffffff";
    equal = receive(controller, storage, alteredId);
    TEST_ASSERT_EQUAL_STRING("version_conflict", equal.errorCode.c_str());
    TEST_ASSERT_EQUAL(1, storage.writes);
}
void testBoundsAndUnsupportedFieldsAreRejected() {
    Controller controller; Storage storage;
    for (const auto interval : {0U, 2999U, 60001U, 0xffffffffU}) {
        TEST_ASSERT_EQUAL(static_cast<int>(Status::REJECTED), static_cast<int>(receive(controller, storage, command(1, interval)).status));
    }
    for (const auto batch : {0U, 5U, 0xffffffffU}) {
        TEST_ASSERT_EQUAL(static_cast<int>(Status::REJECTED), static_cast<int>(receive(controller, storage, command(1, 3000, batch)).status));
    }
    auto extra = command();
    extra["desired"]["settings"]["wifiPassword"] = "not-allowed";
    TEST_ASSERT_EQUAL(static_cast<int>(Status::REJECTED), static_cast<int>(receive(controller, storage, extra).status));
    TEST_ASSERT_EQUAL(0, storage.writes);
    TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(receive(controller, storage, command(1, 60000, 1)).status));
}
void testSettingsRequireJsonIntegersNotBooleanOrStrings() {
    Controller controller; Storage storage;
    for (const char* field : {"measurementIntervalMs", "replayBatchSize"}) {
        for (const char* encoded : {"null", "true", "false", "\"3000\"", "3000.0", "[]", "{}", "-1"}) {
            auto doc = command();
            doc["desired"]["settings"][field] = "PLACEHOLDER";
            std::string raw = text(doc);
            const auto index = raw.find("\"PLACEHOLDER\"");
            raw.replace(index, std::strlen("\"PLACEHOLDER\""), encoded);
            // Keep the exact wire spelling: reserializing a floating JSON value
            // could turn 3000.0 into 3000 before it reaches the parser.
            TEST_ASSERT_EQUAL(static_cast<int>(Status::REJECTED), static_cast<int>(controller.receive(raw.c_str(), ID, persist, &storage).status));
        }
    }
    TEST_ASSERT_EQUAL(0, storage.writes);
}
void testMalformedMissingOrFutureEnvelopeCannotApply() {
    Controller controller; Storage storage;
    for (const char* encoded : {"null", "[]", "{", "{}", "true"})
        TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(controller.receive(encoded, ID, persist, &storage).status));
    for (const char* field : {"schemaVersion", "deviceId", "siteId", "assetId", "desired"}) {
        auto doc = command();
        doc.remove(field);
        TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(receive(controller, storage, doc).status));
    }
    auto newer = command(); newer["schemaVersion"] = 3;
    TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(receive(controller, storage, newer).status));
    auto none = command(); none["desired"] = nullptr;
    TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(receive(controller, storage, none).status));
    std::string oversized(MAX_RESPONSE_BYTES + 1, 'x');
    controller.receive(oversized.c_str(), ID, persist, &storage);
    TEST_ASSERT_EQUAL(0, storage.writes);
}
void testVersionAndCommandIdentityValidation() {
    Controller controller; Storage storage;
    for (const char* encoded : {"null", "true", "\"1\"", "0", "-1", "2147483648", "1.0"}) {
        auto doc = command(); doc["desired"]["version"] = "PLACEHOLDER";
        std::string raw = text(doc);
        raw.replace(raw.find("\"PLACEHOLDER\""), std::strlen("\"PLACEHOLDER\""), encoded);
        TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(controller.receive(raw.c_str(), ID, persist, &storage).status));
    }
    for (const char* id : {"", "abc", "FFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFF", "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"}) {
        auto doc = command(); doc["desired"]["commandId"] = id;
        TEST_ASSERT_EQUAL(static_cast<int>(Status::NONE), static_cast<int>(receive(controller, storage, doc).status));
    }
    TEST_ASSERT_EQUAL(0, storage.writes);
}
void testResultAckRequiresExactVersionCommandDeviceAndStatus() {
    Controller controller; Storage storage;
    const auto result = receive(controller, storage, command());
    JsonDocument ack;
    ack["accepted"] = true; ack["deviceId"] = ID.deviceId;
    ack["version"] = 1; ack["commandId"] = COMMAND; ack["status"] = "applied";
    TEST_ASSERT_TRUE(resultAccepted(200, text(ack).c_str(), ID.deviceId, result));
    TEST_ASSERT_FALSE(resultAccepted(201, text(ack).c_str(), ID.deviceId, result));
    for (const char* key : {"accepted", "deviceId", "version", "commandId", "status"}) {
        JsonDocument wrong = ack; wrong[key] = "wrong";
        TEST_ASSERT_FALSE(resultAccepted(200, text(wrong).c_str(), ID.deviceId, result));
    }
}
void testScheduleHasBoundedFailureBackoffAndNoBusyPolling() {
    Schedule schedule;
    TEST_ASSERT_TRUE(schedule.due(100));
    schedule.completed(100, true, false);
    TEST_ASSERT_FALSE(schedule.due(30099));
    TEST_ASSERT_TRUE(schedule.due(30100));
    schedule.completed(30100, true, true);
    TEST_ASSERT_FALSE(schedule.due(31099));
    TEST_ASSERT_TRUE(schedule.due(31100));
    std::uint32_t now = 31100;
    for (unsigned delay : {5000U, 10000U, 20000U, 40000U, 60000U, 60000U}) {
        schedule.completed(now, false, false);
        TEST_ASSERT_FALSE(schedule.due(now + delay - 1));
        now += delay;
        TEST_ASSERT_TRUE(schedule.due(now));
    }
}
void testScheduleSurvivesMonotonicWrap() {
    Schedule schedule;
    schedule.completed(0xfffffff0U, false, false);
    TEST_ASSERT_FALSE(schedule.due(100));
    TEST_ASSERT_TRUE(schedule.due(5000));
}
void testHealthIntervalV2PersistsAndLegacyCommandsPreserveIt() {
    Controller controller, rebooted; Storage storage;
    auto doc = command(); doc["schemaVersion"] = 2;
    doc["desired"]["settings"]["healthReportIntervalMs"] = 120000;
    auto result = receive(controller, storage, doc);
    TEST_ASSERT_EQUAL(static_cast<int>(Status::APPLIED), static_cast<int>(result.status));
    TEST_ASSERT_EQUAL(120000, controller.active().healthReportIntervalMs);
    TEST_ASSERT_TRUE(rebooted.restore(storage.durable, ID));
    TEST_ASSERT_EQUAL_STRING(resultPayload(result).c_str(), resultPayload(rebooted.appliedResult()).c_str());
    auto legacy = receive(controller, storage, command(2));
    TEST_ASSERT_EQUAL(120000, controller.active().healthReportIntervalMs);
    TEST_ASSERT_TRUE(resultPayload(legacy).find("healthReportIntervalMs") == std::string::npos);
    for (unsigned bad : {0U, 9999U, 300001U}) {
        doc["desired"]["version"] = 3;
        doc["desired"]["settings"]["healthReportIntervalMs"] = bad;
        TEST_ASSERT_EQUAL(static_cast<int>(Status::REJECTED), static_cast<int>(receive(controller, storage, doc).status));
    }
    TEST_ASSERT_EQUAL(2, storage.writes);
}
void testLegacyStorageMigrationKeepsVersionAndDoesNotInventAppliedHealth() {
    Controller controller, migrated; Storage storage;
    receive(controller, storage, command(9));
    LegacyBlob old;
    std::memcpy(&old, &storage.durable, offsetof(LegacyBlob, crc));
    old.schema = 1;
    std::uint32_t crc = 0xffffffffU;
    const auto* bytes = reinterpret_cast<const unsigned char*>(&old);
    for (std::size_t i=0; i<offsetof(LegacyBlob, crc); ++i) {
        crc ^= bytes[i];
        for (unsigned bit=0; bit<8; ++bit) crc=(crc>>1)^((crc&1)?0xedb88320U:0);
    }
    old.crc = ~crc;
    TEST_ASSERT_TRUE(migrated.restoreLegacy(old, ID));
    TEST_ASSERT_EQUAL(9, migrated.active().version);
    TEST_ASSERT_EQUAL(30000, migrated.active().healthReportIntervalMs);
    TEST_ASSERT_TRUE(resultPayload(migrated.appliedResult()).find("healthReportIntervalMs") == std::string::npos);
    old.crc ^= 1;
    TEST_ASSERT_FALSE(migrated.restoreLegacy(old, ID));
    TEST_ASSERT_EQUAL(9, migrated.active().version);
}
} // namespace

void setUp() {}
void tearDown() {}
int main(int argc, char** argv) {
    if ((argc == 2 || argc == 5) && std::strcmp(argv[1], "--roundtrip-json") == 0) {
        const std::string input{std::istreambuf_iterator<char>(std::cin), std::istreambuf_iterator<char>()};
        Controller controller, rebooted; Storage storage;
        if (argc == 5) storage.identity = {argv[2], argv[3], argv[4]};
        const auto result = controller.receive(input.c_str(), storage.identity, persist, &storage);
        if (result.status != Status::APPLIED || !rebooted.restore(storage.durable, storage.identity)) return 2;
        std::cout << resultPayload(result) << '\n' << resultPayload(rebooted.appliedResult()) << '\n';
        return 0;
    }
    UNITY_BEGIN();
    RUN_TEST(testDefaultAndUnconfiguredDoNotClaimApplied);
    RUN_TEST(testApplyCommitsWholeVersionSettingsAndIdentity);
    RUN_TEST(testRebootRestoresSettingsAndOriginalResult);
    RUN_TEST(testSameCommandAndAckLossDoNotRewriteFlash);
    RUN_TEST(testFailedPersistenceDoesNotApplyCandidate);
    RUN_TEST(testWriteSucceededReadbackFailedThenRebootReconcilesPersistedVersion);
    RUN_TEST(testStorageRecoveryRetriesExactVersion);
    RUN_TEST(testEveryCorruptBlobByteIsRejectedWithoutChangingActiveState);
    RUN_TEST(testStoredScopeAndIncomingScopeMustMatchLocalIdentity);
    RUN_TEST(testIdentifierDotsAndLengthBoundariesApplyAndRestore);
    RUN_TEST(testUnsupportedLocalIdentifiersNeverPersist);
    RUN_TEST(testEmbeddedNullInWireIdentityCannotMatchLocalPrefix);
    RUN_TEST(testOlderVersionAndConflictingEqualVersionNeverApply);
    RUN_TEST(testBoundsAndUnsupportedFieldsAreRejected);
    RUN_TEST(testSettingsRequireJsonIntegersNotBooleanOrStrings);
    RUN_TEST(testMalformedMissingOrFutureEnvelopeCannotApply);
    RUN_TEST(testVersionAndCommandIdentityValidation);
    RUN_TEST(testResultAckRequiresExactVersionCommandDeviceAndStatus);
    RUN_TEST(testScheduleHasBoundedFailureBackoffAndNoBusyPolling);
    RUN_TEST(testScheduleSurvivesMonotonicWrap);
    RUN_TEST(testHealthIntervalV2PersistsAndLegacyCommandsPreserveIt);
    RUN_TEST(testLegacyStorageMigrationKeepsVersionAndDoesNotInventAppliedHealth);
    return UNITY_END();
}
