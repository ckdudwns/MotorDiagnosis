#pragma once

#include <cstddef>
#include <cstdint>

namespace RawAck {

struct Expected {
    const char* bootId = nullptr;
    std::uint32_t windowIndex = 0;
};

bool matches(
    int status,
    const char* responseJson,
    const char* expectedDeviceId,
    const Expected* expected,
    std::size_t count,
    std::size_t* matchedCount = nullptr
);

} // namespace RawAck
