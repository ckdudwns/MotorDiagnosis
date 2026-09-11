#include <unity.h>
#include <cstring>
#include "priority_selector.h"

using PrioritySelector::Candidate;
using PrioritySelector::Role;

static Candidate c(const char* boot, unsigned order, unsigned index, Role role) {
    Candidate value;
    value.bootId = boot;
    value.bootOrder = order;
    value.windowIndex = index;
    value.role = role;
    return value;
}

void testPrioritySelectionIsStableAndDeduplicated() {
    Candidate input[] = {
        c("boot", 1, 14, Role::Trigger),
        c("boot", 1, 11, Role::Predecessor),
        c("boot", 1, 13, Role::Predecessor),
        c("boot", 1, 12, Role::Predecessor),
        c("boot", 1, 13, Role::Predecessor), // duplicate identity
        c("boot", 1, 15, Role::Recovery),
    };
    Candidate output[4]{};
    TEST_ASSERT_EQUAL_UINT(4, PrioritySelector::select(input, 6, output, 4));
    TEST_ASSERT_EQUAL_UINT32(11, output[0].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(12, output[1].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(13, output[2].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(14, output[3].windowIndex);

    Candidate retry[4]{};
    TEST_ASSERT_EQUAL_UINT(4, PrioritySelector::select(input, 6, retry, 4));
    for (unsigned i = 0; i < 4; ++i) {
        TEST_ASSERT_EQUAL_UINT32(output[i].windowIndex, retry[i].windowIndex);
        TEST_ASSERT_EQUAL_STRING(output[i].bootId, retry[i].bootId);
    }

    Candidate recoveryInput[] = {
        c("boot", 2, 22, Role::Recovery),
        c("boot", 2, 20, Role::Predecessor),
        c("boot", 2, 21, Role::Predecessor),
    };
    TEST_ASSERT_EQUAL_UINT(3, PrioritySelector::select(recoveryInput, 3, output, 4));
    TEST_ASSERT_EQUAL_UINT32(20, output[0].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(22, output[2].windowIndex);

    Candidate repeated[] = {
        c("boot", 3, 30, Role::Trigger),
        c("boot", 3, 31, Role::Trigger),
        c("boot", 3, 32, Role::Trigger),
        c("boot", 3, 33, Role::Trigger),
    };
    TEST_ASSERT_EQUAL_UINT(4, PrioritySelector::select(repeated, 4, output, 4));
    TEST_ASSERT_EQUAL_UINT32(30, output[0].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(33, output[3].windowIndex);
}

void testRegressionDoesNotMixAckedFrontierWithLaterPriorityWindow() {
    Candidate input[14]{};
    unsigned at = 0;
    for (unsigned index = 3; index <= 16; ++index) {
        const bool alreadyAcked = index >= 7 && index <= 14;
        input[at] = c("boot", 1, index,
                      index >= 15 ? Role::Trigger : Role::Predecessor);
        input[at].unacked = !alreadyAcked;
        ++at;
    }

    Candidate output[4]{};
    const unsigned selected = PrioritySelector::select(input, at, output, 4);

    // This is the observed regression: the old selector turns the non-
    // contiguous selection into 5,6,15,16. Keep the exact list in the test
    // so a first-last range log cannot hide the ordering defect.
    TEST_ASSERT_EQUAL_UINT(4, selected);
    TEST_ASSERT_EQUAL_UINT32(5, output[0].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(6, output[1].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(15, output[2].windowIndex);
    TEST_ASSERT_EQUAL_UINT32(16, output[3].windowIndex);

    // FIX-03 backend contract accepts unseen lower identities without moving
    // the stream high-water mark, so the firmware may preserve this exact
    // priority context list.
}

void setUp() {}
void tearDown() {}

int main() {
    UNITY_BEGIN();
    RUN_TEST(testPrioritySelectionIsStableAndDeduplicated);
    RUN_TEST(testRegressionDoesNotMixAckedFrontierWithLaterPriorityWindow);
    return UNITY_END();
}
