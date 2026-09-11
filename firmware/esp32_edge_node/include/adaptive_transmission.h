#pragma once

#include <cstdint>
#include <cstddef>

#ifndef ADAPTIVE_BASELINE_MISSING_INTERVAL_MS
#define ADAPTIVE_BASELINE_MISSING_INTERVAL_MS 60000UL
#endif

namespace AdaptiveTransmission {
struct Config {
    const char* baselineId;
    double rmsLowG;
    double rmsHighG;
    double severeLowG;
    double severeHighG;
    double severePeakG;
    std::uint32_t recoveryMs;
};
bool valid(const Config& config);

enum class Mode : std::uint8_t { Periodic, Priority };
enum class Reason : std::uint8_t {
    None, RmsLow, RmsHigh, Severe, Quality, BaselineMissing,
    StoragePressure, RecoveryHold
};
struct Sample {
    const char* bootId;
    std::uint32_t index;
    std::uint64_t timestampUs;
    double values[21];
    bool qualityValid;
    bool storagePressure;
};
struct History {
    bool initialized = false;
    bool recovery = false;
    char bootId[33]{};
    std::uint32_t lastIndex = 0;
    std::uint64_t lastTimestampUs = 0;
    std::uint64_t recoveryStartUs = 0;
    bool recentLow[3]{};
    bool recentHigh[3]{};
    unsigned recentCount = 0;
    // baseline_missing is a diagnostic trigger, not a per-window priority
    // signal. Keep its cooldown in the same monotonic sample-time domain.
    bool baselineMissingSent = false;
    std::uint64_t baselineMissingLastUs = 0;
};
struct Decision {
    Mode mode;
    Reason reason;
};
Decision evaluate(const Config& config, const Sample& sample, History& history);
const char* reasonName(Reason reason);
} // namespace AdaptiveTransmission
