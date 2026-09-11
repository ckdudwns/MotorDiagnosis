#include <unity.h>
#include "raw_status.h"

using RawStatus::Kind;

void testTransportAndServerRetryStatusesAreRetryable() {
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::Retryable), static_cast<int>(RawStatus::classify(-1, nullptr, false)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::Retryable), static_cast<int>(RawStatus::classify(408, nullptr, false)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::Retryable), static_cast<int>(RawStatus::classify(500, nullptr, false)));
}

void testStrictAckControlsSuccess() {
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::Accepted), static_cast<int>(RawStatus::classify(202, "{}", true)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::ProtocolHold), static_cast<int>(RawStatus::classify(200, "{}", false)));
}

void testConflictBodiesAreQuarantinedByType() {
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::PermanentQuarantine),
                          static_cast<int>(RawStatus::classify(409, "WINDOW_CONFLICT", false)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::OrderingQuarantine),
                          static_cast<int>(RawStatus::classify(409, "WINDOW_SEQUENCE_CONFLICT", false)));
    TEST_ASSERT_FALSE(RawStatus::retryable(Kind::PermanentQuarantine));
    TEST_ASSERT_FALSE(RawStatus::retryable(Kind::OrderingQuarantine));
}

void testConfigurationAndUnknownStatusesStayHeld() {
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::ConfigurationHold),
                          static_cast<int>(RawStatus::classify(400, nullptr, false)));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Kind::UnknownHold),
                          static_cast<int>(RawStatus::classify(418, nullptr, false)));
    TEST_ASSERT_TRUE(RawStatus::retryable(Kind::ConfigurationHold));
}

void setUp() {}
void tearDown() {}

int main() {
    UNITY_BEGIN();
    RUN_TEST(testTransportAndServerRetryStatusesAreRetryable);
    RUN_TEST(testStrictAckControlsSuccess);
    RUN_TEST(testConflictBodiesAreQuarantinedByType);
    RUN_TEST(testConfigurationAndUnknownStatusesStayHeld);
    return UNITY_END();
}
