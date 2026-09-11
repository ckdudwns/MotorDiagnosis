#include <unity.h>
#include "raw_ack.h"

namespace {
const char* DEVICE = "DEV-01-MOT-02";
const RawAck::Expected EXPECTED[] = {{"boot-a", 10}, {"boot-a", 11}};

void testAccepts202WithCompleteAck() {
    const char* json = R"({"accepted":2,"deviceId":"DEV-01-MOT-02","acknowledged":[{"bootId":"boot-a","windowIndex":10},{"bootId":"boot-a","windowIndex":11}]})";
    TEST_ASSERT_TRUE(RawAck::matches(202, json, DEVICE, EXPECTED, 2));
}

void testAccepts200DuplicateWithCompleteAck() {
    const char* json = R"({"accepted":0,"deviceId":"DEV-01-MOT-02","acknowledged":[{"bootId":"boot-a","windowIndex":10},{"bootId":"boot-a","windowIndex":11}]})";
    TEST_ASSERT_TRUE(RawAck::matches(200, json, DEVICE, EXPECTED, 2));
}

void testAcceptedSummaryDoesNotAuthorizeOrRejectDeletion() {
    const char* json = R"({"accepted":false,"deviceId":"DEV-01-MOT-02","acknowledged":[{"bootId":"boot-a","windowIndex":10},{"bootId":"boot-a","windowIndex":11}]})";
    TEST_ASSERT_TRUE(RawAck::matches(200, json, DEVICE, EXPECTED, 2));
}

void testRejectsPartialAck() {
    const char* json = R"({"accepted":1,"deviceId":"DEV-01-MOT-02","acknowledged":[{"bootId":"boot-a","windowIndex":10}]})";
    TEST_ASSERT_FALSE(RawAck::matches(202, json, DEVICE, EXPECTED, 2));
}

void testRejectsWrongBootId() {
    const char* json = R"({"accepted":2,"deviceId":"DEV-01-MOT-02","acknowledged":[{"bootId":"wrong","windowIndex":10},{"bootId":"boot-a","windowIndex":11}]})";
    TEST_ASSERT_FALSE(RawAck::matches(202, json, DEVICE, EXPECTED, 2));
}

void testRejectsBrokenJson() {
    TEST_ASSERT_FALSE(RawAck::matches(202, "{", DEVICE, EXPECTED, 2));
}
} // namespace

void setUp() {}
void tearDown() {}

int main() {
    UNITY_BEGIN();
    RUN_TEST(testAccepts202WithCompleteAck);
    RUN_TEST(testAccepts200DuplicateWithCompleteAck);
    RUN_TEST(testAcceptedSummaryDoesNotAuthorizeOrRejectDeletion);
    RUN_TEST(testRejectsPartialAck);
    RUN_TEST(testRejectsWrongBootId);
    RUN_TEST(testRejectsBrokenJson);
    return UNITY_END();
}
