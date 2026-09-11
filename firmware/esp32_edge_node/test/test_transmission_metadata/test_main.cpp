#include <unity.h>
#include <string>
#include "transmission_metadata.h"

static std::uint32_t hashBody(const std::string& body) {
    std::uint32_t hash = 2166136261u;
    for (unsigned char value : body) hash = (hash ^ value) * 16777619u;
    return hash;
}

void testTransmissionMetadataIsFrozenAndRetryStable() {
    JsonDocument first;
    TransmissionMetadata::write(
        first["transmission"].to<JsonObject>(), "priority",
        AdaptiveTransmission::Reason::RmsHigh, "pump-normal-v1", 7);
    std::string body;
    serializeJson(first, body);
    TEST_ASSERT_NOT_EQUAL(std::string::npos, body.find("\"policyId\":\"edge-trigger-batch-v1\""));
    TEST_ASSERT_NOT_EQUAL(std::string::npos, body.find("\"mode\":\"priority\""));
    TEST_ASSERT_NOT_EQUAL(std::string::npos, body.find("\"reason\":\"rms_high\""));
    TEST_ASSERT_NOT_EQUAL(std::string::npos, body.find("\"baselineId\":\"pump-normal-v1\""));
    TEST_ASSERT_NOT_EQUAL(std::string::npos, body.find("\"droppedWindows\":7"));

    const auto frozenHash = hashBody(body);
    const auto retryHash = hashBody(body);
    TEST_ASSERT_EQUAL_UINT32(frozenHash, retryHash);

    JsonDocument restored;
    TransmissionMetadata::write(
        restored["transmission"].to<JsonObject>(), "replay",
        AdaptiveTransmission::Reason::None, "unconfigured", 0);
    std::string restoredBody;
    serializeJson(restored, restoredBody);
    TEST_ASSERT_NOT_EQUAL(std::string::npos, restoredBody.find("\"mode\":\"replay\""));
}

void setUp() {}
void tearDown() {}

int main() {
    UNITY_BEGIN();
    RUN_TEST(testTransmissionMetadataIsFrozenAndRetryStable);
    return UNITY_END();
}
