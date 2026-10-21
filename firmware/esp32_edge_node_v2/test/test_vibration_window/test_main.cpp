#include <cmath>

#include <unity.h>

#include "policy/transmission_policy.h"
#include "sampling/vibration_window.h"

void test_window_accepts_exactly_512_samples() {
    VibrationWindow window;
    window.clear();

    for (int i = 0; i < 512; ++i) {
        TEST_ASSERT_TRUE(window.add(
            {static_cast<int16_t>(100 + (i % 2 ? 10 : -10)), 0,
             static_cast<int16_t>(i % 2 ? 20 : -20)}));
    }

    TEST_ASSERT_TRUE(window.full());
    TEST_ASSERT_FALSE(window.add({0, 0, 0}));
    TEST_ASSERT_EQUAL_UINT16(512, window.size());
}

void test_window_removes_axis_mean_before_rms() {
    VibrationWindow window;
    window.clear();

    for (int i = 0; i < 512; ++i) {
        TEST_ASSERT_TRUE(window.add(
            {static_cast<int16_t>(100 + (i % 2 ? 10 : -10)), 0,
             static_cast<int16_t>(i % 2 ? 20 : -20)}));
    }

    const WindowStats stats = window.summarize();
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.039F, stats.rmsXG);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.078F, stats.rmsZG);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.0F, stats.rmsYG);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.08720665F, stats.resultantRmsG);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.078F,
                             stats.strongestAcPeakG);
    TEST_ASSERT_TRUE(stats.featuresValid);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 1.0F, stats.features.cfA1);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 1.0F, stats.features.cfA3);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.0F,
                             stats.features.skewnessA1);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 1.0F,
                             stats.features.kurtosisA1);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.0F, stats.features.cfA2);
}

void test_empty_window_has_zero_stats() {
    VibrationWindow window;
    window.clear();

    const WindowStats stats = window.summarize();
    TEST_ASSERT_EQUAL_UINT16(0, stats.sampleCount);
    TEST_ASSERT_FLOAT_WITHIN(0.00001F, 0.0F, stats.resultantRmsG);
}

void test_invalid_window_preserves_policy_counters() {
    TransmissionPolicy policy({true, 1.0F, 1.0F, true, 3, 5, 10ULL});
    WindowStats abnormal;
    abnormal.resultantRmsG = 2.0F;
    abnormal.strongestAcPeakG = 2.0F;

    TransmissionPolicyInput input;
    input.valid = true;
    input.stats = abnormal;
    input.uptimeUs = 1;
    TEST_ASSERT_EQUAL_UINT8(1, policy.evaluate(input).anomalyCount);

    input.valid = false;
    TEST_ASSERT_EQUAL_UINT8(1, policy.evaluate(input).anomalyCount);

    input.valid = true;
    input.uptimeUs = 2;
    TEST_ASSERT_EQUAL_UINT8(2, policy.evaluate(input).anomalyCount);
}

void runTests() {
    UNITY_BEGIN();
    RUN_TEST(test_window_accepts_exactly_512_samples);
    RUN_TEST(test_window_removes_axis_mean_before_rms);
    RUN_TEST(test_empty_window_has_zero_stats);
    RUN_TEST(test_invalid_window_preserves_policy_counters);
    UNITY_END();
}

#ifdef ARDUINO
void setup() { runTests(); }

void loop() {}
#else
int main() {
    runTests();
    return 0;
}
#endif
