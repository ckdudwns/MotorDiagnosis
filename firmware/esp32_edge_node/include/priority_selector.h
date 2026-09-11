#pragma once

#include "adaptive_transmission.h"
#include <cstddef>
#include <cstdint>
#include <cstring>

namespace PrioritySelector {

enum class Role : std::uint8_t { Predecessor, Trigger, Recovery };

struct Candidate {
    const char* bootId = nullptr;
    std::uint32_t bootOrder = 0;
    std::uint32_t windowIndex = 0;
    Role role = Role::Predecessor;
    bool unacked = true;
};

inline bool sameIdentity(const Candidate& left, const Candidate& right) {
    return left.bootOrder == right.bootOrder &&
        left.windowIndex == right.windowIndex &&
        std::strncmp(left.bootId ? left.bootId : "", right.bootId ? right.bootId : "", 32) == 0;
}

inline bool before(const Candidate& left, const Candidate& right) {
    if (left.bootOrder != right.bootOrder) return left.bootOrder < right.bootOrder;
    if (left.windowIndex != right.windowIndex) return left.windowIndex < right.windowIndex;
    return std::strcmp(left.bootId ? left.bootId : "", right.bootId ? right.bootId : "") < 0;
}

// Select the trigger (or the oldest recovery follow-up) and its three
// unACKed predecessors.  The input is copied into a deterministic order; no
// storage, sensor, or network state is touched, so retrying the same snapshot
// produces the same identities.
inline unsigned select(const Candidate* input, unsigned count,
                       Candidate* output, unsigned capacity) {
    if (!input || !output || !capacity) return 0;
    Candidate ordered[32]{};
    if (count > 32) count = 32;
    unsigned orderedCount = 0;
    for (unsigned i = 0; i < count; ++i) {
        if (!input[i].unacked) continue;
        bool duplicate = false;
        for (unsigned j = 0; j < orderedCount; ++j)
            if (sameIdentity(ordered[j], input[i])) { duplicate = true; break; }
        if (duplicate) continue;
        unsigned at = orderedCount;
        while (at && before(input[i], ordered[at - 1])) --at;
        for (unsigned j = orderedCount; j > at; --j) ordered[j] = ordered[j - 1];
        ordered[at] = input[i];
        ++orderedCount;
    }
    if (!orderedCount) return 0;
    unsigned trigger = orderedCount;
    // Pick the newest trigger in the snapshot; its preceding entries provide
    // the context window while repeated trigger decisions remain deduplicated.
    for (unsigned i = 0; i < orderedCount; ++i)
        if (ordered[i].role == Role::Trigger) trigger = i;
    if (trigger == orderedCount)
        for (unsigned i = 0; i < orderedCount; ++i)
            if (ordered[i].role == Role::Recovery) { trigger = i; break; }
    if (trigger == orderedCount) return 0;

    const unsigned first = trigger > 3 ? trigger - 3 : 0;
    unsigned selected = 0;
    for (unsigned i = first; i <= trigger && selected < capacity; ++i)
        output[selected++] = ordered[i];
    return selected;
}

} // namespace PrioritySelector
