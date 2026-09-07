#pragma once
#include <cstddef>
#include <cstdint>

namespace VibrationWindow {
constexpr unsigned Samples = 512;
constexpr unsigned Rate = 800;
constexpr unsigned FeatureCount = 21;
constexpr std::uint64_t DurationUs = 640000;
constexpr const char* Profile = "mcc5-vibration-800hz-xyz-v1";
enum class Quality : std::uint8_t {
    Valid, FifoOverrun, SensorUnavailable, SampleGap, Clipped,
    ConstantAxis, ProcessingOverflow
};
const char* qualityName(Quality quality);
struct Raw {
    std::uint32_t index = 0;
    std::uint64_t startUs = 0;
    std::uint64_t audioStart = 0;
    std::uint32_t audioGeneration = 0;
    std::uint16_t count = 0;
    Quality quality = Quality::Valid;
    std::int16_t xyz[Samples][3]{};
};
struct Features {
    std::uint32_t index = 0;
    std::uint64_t startUs = 0;
    std::uint16_t count = 0;
    Quality quality = Quality::Valid;
    double values[FeatureCount]{};
    double peakHz[3]{};
};
// Owned by one processing task, not allocated on its stack or in PSRAM.
struct Workspace { double real[Samples]; double imag[Samples]; };
Features extract(const Raw& raw, Workspace& workspace);

inline bool serializeCountsLE(const Raw& raw, std::uint8_t* bytes, std::size_t capacity) {
    if (!bytes || raw.count > Samples || capacity < raw.count*6U) return false;
    for (unsigned i=0; i<raw.count; ++i) for (unsigned axis=0; axis<3; ++axis) {
        const auto value = static_cast<std::uint16_t>(raw.xyz[i][axis]);
        const unsigned offset = (i*3+axis)*2;
        bytes[offset] = value & 0xff;
        bytes[offset+1] = value >> 8;
    }
    return true;
}

// External synchronization is required. Overflow rejects newest: an in-flight
// head is never overwritten and can be retried with exactly the same identity.
template <typename T, unsigned Capacity> class Queue {
    T entries_[Capacity]{};
    unsigned head_ = 0, size_ = 0;
public:
    std::uint64_t dropped = 0;
    bool push(const T& value) {
        if (size_ == Capacity) { ++dropped; return false; }
        entries_[(head_ + size_) % Capacity] = value; ++size_; return true;
    }
    unsigned size() const { return size_; }
    const T& at(unsigned offset) const { return entries_[(head_ + offset) % Capacity]; }
    bool acknowledge(unsigned count) {
        if (!count || count > size_) return false;
        head_ = (head_ + count) % Capacity; size_ -= count; return true;
    }
};
}
