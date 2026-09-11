#include <unity.h>
#include "periodic_scheduler.h"

using PeriodicScheduler::Candidate;

void testCutoffIsFixedAndExcludesNewWindows() {
    const std::uint64_t now = 600000000ULL;
    TEST_ASSERT_EQUAL_UINT64(300000000ULL, PeriodicScheduler::cutoff(now));
    TEST_ASSERT_TRUE(PeriodicScheduler::eligible(300000000ULL, now));
    TEST_ASSERT_FALSE(PeriodicScheduler::eligible(300000001ULL, now));
}

void testSelectionIsOldestInputOrderAndBounded() {
    const Candidate input[] = {{1, 100000000ULL}, {2, 300000000ULL},
                               {3, 300000001ULL}, {4, 200000000ULL},
                               {5, 100000000ULL}};
    Candidate output[4]{};
    const unsigned count = PeriodicScheduler::select(input, 5, 600000000ULL, output, 4);
    TEST_ASSERT_EQUAL_UINT(4, count);
    TEST_ASSERT_EQUAL_UINT32(1, output[0].index);
    TEST_ASSERT_EQUAL_UINT32(2, output[1].index);
    TEST_ASSERT_EQUAL_UINT32(4, output[2].index);
    TEST_ASSERT_EQUAL_UINT32(5, output[3].index);
}

void testBeforeFiveMinutesNothingIsEligible() {
    const Candidate input[] = {{1, 1ULL}, {2, 299999999ULL}};
    Candidate output[4]{};
    TEST_ASSERT_EQUAL_UINT(0, PeriodicScheduler::select(input, 2, 300000000ULL, output, 4));
}

void setUp() {}
void tearDown() {}
int main() {
    UNITY_BEGIN();
    RUN_TEST(testCutoffIsFixedAndExcludesNewWindows);
    RUN_TEST(testSelectionIsOldestInputOrderAndBounded);
    RUN_TEST(testBeforeFiveMinutesNothingIsEligible);
    return UNITY_END();
}
