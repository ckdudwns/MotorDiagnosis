#include "adaptive_transmission.h"
#include <cmath>
#include <cstring>
#include <algorithm>

namespace AdaptiveTransmission {
bool valid(const Config& config) {
    if (!config.baselineId || !*config.baselineId ||
        std::strcmp(config.baselineId, "unconfigured") == 0 || config.recoveryMs == 0)
        return false;
    if (!std::isfinite(config.rmsLowG) || !std::isfinite(config.rmsHighG) ||
        !std::isfinite(config.severeLowG) || !std::isfinite(config.severeHighG) ||
        !std::isfinite(config.severePeakG))
        return false;
    return config.severeLowG >= 0.0 && config.severeLowG < config.rmsLowG &&
           config.rmsLowG < config.rmsHighG && config.rmsHighG < config.severeHighG &&
           config.severePeakG > 0.0;
}

namespace {
constexpr std::uint64_t WindowUs = 640000;
constexpr std::uint64_t BaselineMissingIntervalUs =
    static_cast<std::uint64_t>(ADAPTIVE_BASELINE_MISSING_INTERVAL_MS) * 1000ULL;

void reset(History& history) {
    history = History{};
}

void resetTrend(History& history) {
    const bool baselineMissingSent = history.baselineMissingSent;
    const std::uint64_t baselineMissingLastUs = history.baselineMissingLastUs;
    history = History{};
    history.baselineMissingSent = baselineMissingSent;
    history.baselineMissingLastUs = baselineMissingLastUs;
}

void rememberSample(History& history, const Sample& sample) {
    history.initialized = true;
    std::strncpy(history.bootId, sample.bootId ? sample.bootId : "", sizeof(history.bootId) - 1);
    history.bootId[sizeof(history.bootId) - 1] = '\0';
    history.lastIndex = sample.index;
    history.lastTimestampUs = sample.timestampUs;
}

void push(History& history, bool low, bool high) {
    if (history.recentCount < 3) {
        history.recentLow[history.recentCount] = low;
        history.recentHigh[history.recentCount] = high;
        ++history.recentCount;
        return;
    }
    for (unsigned i = 1; i < 3; ++i) {
        history.recentLow[i - 1] = history.recentLow[i];
        history.recentHigh[i - 1] = history.recentHigh[i];
    }
    history.recentLow[2] = low;
    history.recentHigh[2] = high;
}

bool twice(const bool values[3], unsigned count) {
    unsigned hits = 0;
    for (unsigned i = 0; i < count; ++i) hits += values[i] ? 1U : 0U;
    return hits >= 2;
}
}

Decision evaluate(const Config& config, const Sample& sample, History& history) {
    const char* sampleBootId = sample.bootId ? sample.bootId : "";
    const bool identityChanged = history.initialized &&
        (std::strncmp(history.bootId, sampleBootId, sizeof(history.bootId)) != 0);
    const bool discontinuity = history.initialized &&
        (sample.index != history.lastIndex + 1 ||
         sample.timestampUs != history.lastTimestampUs + WindowUs);
    if (identityChanged || discontinuity) reset(history);

    if (!sample.qualityValid) {
        resetTrend(history);
        return {Mode::Priority, Reason::Quality};
    }

    if (!valid(config)) {
        resetTrend(history);
        const bool eligible = !history.baselineMissingSent ||
            sample.timestampUs - history.baselineMissingLastUs >= BaselineMissingIntervalUs;
        rememberSample(history, sample);
        if (eligible) {
            history.baselineMissingSent = true;
            history.baselineMissingLastUs = sample.timestampUs;
            return {Mode::Priority, Reason::BaselineMissing};
        }
        return {Mode::Periodic, Reason::BaselineMissing};
    }

    // A newly valid baseline starts a fresh missing-baseline episode later.
    history.baselineMissingSent = false;
    history.baselineMissingLastUs = 0;

    const double rmsX = sample.values[0];
    const double rmsY = sample.values[7];
    const double rmsZ = sample.values[14];
    const double peakX = sample.values[1];
    const double peakY = sample.values[8];
    const double peakZ = sample.values[15];
    const double compositeRms = std::sqrt(rmsX * rmsX + rmsY * rmsY + rmsZ * rmsZ);
    const double maxPeak = std::max(peakX, std::max(peakY, peakZ));
    const bool severe = compositeRms <= config.severeLowG ||
                        compositeRms >= config.severeHighG || maxPeak >= config.severePeakG;
    const bool low = compositeRms <= config.rmsLowG;
    const bool high = compositeRms >= config.rmsHighG;

    if (!history.initialized) {
        history.initialized = true;
        std::strncpy(history.bootId, sample.bootId ? sample.bootId : "", sizeof(history.bootId) - 1);
        history.bootId[sizeof(history.bootId) - 1] = '\0';
    }
    history.lastIndex = sample.index;
    history.lastTimestampUs = sample.timestampUs;

    if (severe) {
        reset(history);
        history.recovery = true;
        history.recoveryStartUs = sample.timestampUs;
        rememberSample(history, sample);
        return {Mode::Priority, Reason::Severe};
    }

    push(history, low, high);
    if (twice(history.recentLow, history.recentCount)) {
        history.recovery = true;
        history.recoveryStartUs = sample.timestampUs;
        return {Mode::Priority, Reason::RmsLow};
    }
    if (twice(history.recentHigh, history.recentCount)) {
        history.recovery = true;
        history.recoveryStartUs = sample.timestampUs;
        return {Mode::Priority, Reason::RmsHigh};
    }
    if (history.recovery) {
        if (sample.timestampUs - history.recoveryStartUs < config.recoveryMs * 1000ULL)
            return {Mode::Priority, Reason::RecoveryHold};
        history.recovery = false;
    }
    if (sample.storagePressure) return {Mode::Periodic, Reason::StoragePressure};
    return {Mode::Periodic, Reason::None};
}

const char* reasonName(Reason reason) {
    switch (reason) {
        case Reason::None: return "none";
        case Reason::RmsLow: return "rms_low";
        case Reason::RmsHigh: return "rms_high";
        case Reason::Severe: return "severe";
        case Reason::Quality: return "quality";
        case Reason::BaselineMissing: return "baseline_missing";
        case Reason::StoragePressure: return "storage_pressure";
        case Reason::RecoveryHold: return "recovery_hold";
    }
    return "unknown";
}
} // namespace AdaptiveTransmission
