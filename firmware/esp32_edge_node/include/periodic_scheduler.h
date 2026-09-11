#pragma once

#include <cstdint>

namespace PeriodicScheduler {
constexpr std::uint64_t CutoffUs = 300000000ULL;

struct Candidate {
    std::uint32_t index;
    std::uint64_t startUs;
};

inline std::uint64_t cutoff(std::uint64_t nowUs) {
    return nowUs > CutoffUs ? nowUs - CutoffUs : 0;
}

inline bool eligible(std::uint64_t startUs, std::uint64_t nowUs) {
    return startUs <= cutoff(nowUs);
}

inline unsigned select(const Candidate* input, unsigned count, std::uint64_t nowUs,
                       Candidate* output, unsigned capacity) {
    if (!input || !output || !capacity) return 0;
    unsigned selected = 0;
    for (unsigned i = 0; i < count && selected < capacity; ++i)
        if (eligible(input[i].startUs, nowUs)) output[selected++] = input[i];
    return selected;
}
} // namespace PeriodicScheduler
