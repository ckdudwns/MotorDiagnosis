#include <unity.h>
#include "adaptive_transmission.h"

using namespace AdaptiveTransmission;
static Config config() { return {"pump-normal-v1", 1.0, 2.0, 0.5, 3.0, 4.0, 30000}; }
static Sample sample(std::uint32_t index, std::uint64_t timestamp, double rms,
                     bool valid = true, bool pressure = false) {
    Sample value{"boot", index, timestamp, {}, valid, pressure};
    value.values[0] = value.values[7] = value.values[14] = rms / 1.7320508075688772;
    value.values[1] = value.values[8] = value.values[15] = 0.1;
    return value;
}

void testRejectsUnconfiguredBaseline() {
    TEST_ASSERT_FALSE(AdaptiveTransmission::valid({"unconfigured", 1.0, 2.0, 0.5, 3.0, 4.0, 30000}));
}
void testAcceptsOrderedMeasuredConfig() {
    TEST_ASSERT_TRUE(AdaptiveTransmission::valid({"pump-normal-v1", 1.0, 2.0, 0.5, 3.0, 4.0, 30000}));
}
void testRejectsInvalidOrdering() {
    TEST_ASSERT_FALSE(AdaptiveTransmission::valid({"pump-normal-v1", 2.0, 1.0, 0.5, 3.0, 4.0, 30000}));
    TEST_ASSERT_FALSE(AdaptiveTransmission::valid({"pump-normal-v1", 1.0, 2.0, 2.0, 3.0, 4.0, 30000}));
}
void testRejectsNonFiniteAndZeroRecovery() {
    TEST_ASSERT_FALSE(AdaptiveTransmission::valid({"pump-normal-v1", NAN, 2.0, 0.5, 3.0, 4.0, 30000}));
    TEST_ASSERT_FALSE(AdaptiveTransmission::valid({"pump-normal-v1", 1.0, 2.0, 0.5, 3.0, 4.0, 0}));
}
void testCompositeRmsAndPeakDriveSevere() {
    History history;
    auto value = sample(1, 640000, 1.0);
    value.values[1] = value.values[8] = value.values[15] = 4.0;
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::Severe), static_cast<int>(evaluate(config(), value, history).reason));
}
void testTwoOfThreeLowAndHigh() {
    History history;
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::None), static_cast<int>(evaluate(config(), sample(1, 640000, 0.8), history).reason));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::None), static_cast<int>(evaluate(config(), sample(2, 1280000, 1.5), history).reason));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::RmsLow), static_cast<int>(evaluate(config(), sample(3, 1920000, 0.8), history).reason));
    History high;
    evaluate(config(), sample(1, 640000, 2.2), high);
    evaluate(config(), sample(2, 1280000, 1.5), high);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::RmsHigh), static_cast<int>(evaluate(config(), sample(3, 1920000, 2.2), high).reason));
}
void testInvalidAndBaselineMissingArePriorityAndReset() {
    History history;
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::Quality), static_cast<int>(evaluate(config(), sample(1, 640000, 1.5, false), history).reason));
    auto missing = config(); missing.baselineId = "unconfigured";
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::BaselineMissing), static_cast<int>(evaluate(missing, sample(2, 1280000, 1.5), history).reason));
}
void testBaselineMissingUsesCooldownInsteadOfPriorityFlood() {
    auto missing = config(); missing.baselineId = "unconfigured";
    History history;
    auto first = evaluate(missing, sample(1, 640000, 1.5), history);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Mode::Priority), static_cast<int>(first.mode));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::BaselineMissing), static_cast<int>(first.reason));

    unsigned priorityCount = 1;
    for (std::uint32_t index = 2; index <= 94; ++index) {
        const auto decision = evaluate(missing, sample(index, 640000ULL * index, 1.5), history);
        if (decision.mode == Mode::Priority) ++priorityCount;
        TEST_ASSERT_EQUAL_INT(static_cast<int>(Mode::Periodic), static_cast<int>(decision.mode));
        TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::BaselineMissing), static_cast<int>(decision.reason));
    }
    TEST_ASSERT_EQUAL_UINT(1, priorityCount);

    const auto afterCooldown = evaluate(missing, sample(95, 640000ULL * 95, 1.5), history);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Mode::Priority), static_cast<int>(afterCooldown.mode));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::BaselineMissing), static_cast<int>(afterCooldown.reason));
}
void testQualityInvalidIgnoresBaselineCooldown() {
    auto missing = config(); missing.baselineId = "unconfigured";
    History history;
    evaluate(missing, sample(1, 640000, 1.5), history);
    const auto invalid = evaluate(missing, sample(2, 1280000, 1.5, false), history);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Mode::Priority), static_cast<int>(invalid.mode));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::Quality), static_cast<int>(invalid.reason));
}
void testDiscontinuityResetsTwoOfThree() {
    History history;
    evaluate(config(), sample(1, 640000, 0.8), history);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::None), static_cast<int>(evaluate(config(), sample(3, 1920000, 0.8), history).reason));
}
void testRecoveryHoldThenPeriodicAfterThirtySeconds() {
    History history;
    evaluate(config(), sample(1, 640000, 1.0), history);
    auto severe = sample(2, 1280000, 3.5);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::Severe), static_cast<int>(evaluate(config(), severe, history).reason));
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::RecoveryHold), static_cast<int>(evaluate(config(), sample(3, 1920000, 1.5), history).reason));
    for (std::uint32_t index = 4; index < 50; ++index)
        evaluate(config(), sample(index, 1920000ULL + (index - 3) * 640000ULL, 1.5), history);
    auto recovered = sample(50, 1920000ULL + (50 - 3) * 640000ULL, 1.5);
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::None), static_cast<int>(evaluate(config(), recovered, history).reason));
}
void testStoragePressureAndRebootReset() {
    History history;
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::StoragePressure), static_cast<int>(evaluate(config(), sample(1, 640000, 1.5, true, true), history).reason));
    auto rebooted = sample(1, 640000, 0.8);
    rebooted.bootId = "new-boot";
    TEST_ASSERT_EQUAL_INT(static_cast<int>(Reason::None), static_cast<int>(evaluate(config(), rebooted, history).reason));
}
void setUp() {}
void tearDown() {}
int main() {
    UNITY_BEGIN();
    RUN_TEST(testRejectsUnconfiguredBaseline);
    RUN_TEST(testAcceptsOrderedMeasuredConfig);
    RUN_TEST(testRejectsInvalidOrdering);
    RUN_TEST(testRejectsNonFiniteAndZeroRecovery);
    RUN_TEST(testCompositeRmsAndPeakDriveSevere);
    RUN_TEST(testTwoOfThreeLowAndHigh);
    RUN_TEST(testInvalidAndBaselineMissingArePriorityAndReset);
    RUN_TEST(testBaselineMissingUsesCooldownInsteadOfPriorityFlood);
    RUN_TEST(testQualityInvalidIgnoresBaselineCooldown);
    RUN_TEST(testDiscontinuityResetsTwoOfThree);
    RUN_TEST(testRecoveryHoldThenPeriodicAfterThirtySeconds);
    RUN_TEST(testStoragePressureAndRebootReset);
    return UNITY_END();
}
