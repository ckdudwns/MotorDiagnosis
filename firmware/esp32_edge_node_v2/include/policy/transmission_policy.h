#pragma once

#include <cstdint>

#include "sampling/vibration_window.h"

enum class TransmissionPolicyState : uint8_t {
    normal,
    anomalyActive,
};

enum class TransmissionPolicyAction : uint8_t {
    discard,
    anomalyStart,
    anomalyActive,
    recovery,
};

struct TransmissionPolicyConfig {
    TransmissionPolicyConfig(bool enabled, float rmsThreshold,
                             float peakThreshold, bool anomalyUseOr,
                             uint8_t anomalyCount, uint8_t recoveryCount,
                             uint64_t anomalyUs)
        : anomalyDetectionEnabled(enabled),
          resultantRmsThresholdG(rmsThreshold),
          strongestAcPeakThresholdG(peakThreshold),
          useOr(anomalyUseOr),
          anomalyConsecutiveWindows(anomalyCount),
          recoveryConsecutiveWindows(recoveryCount),
          anomalyIntervalUs(anomalyUs) {}

    bool anomalyDetectionEnabled = false;
    float resultantRmsThresholdG = 0.0F;
    float strongestAcPeakThresholdG = 0.0F;
    bool useOr = true;
    uint8_t anomalyConsecutiveWindows = 3;
    uint8_t recoveryConsecutiveWindows = 5;
    uint64_t anomalyIntervalUs = 10000000ULL;
};

struct TransmissionPolicyInput {
    bool valid = false;
    WindowStats stats;
    bool utcValid = false;
    uint64_t utcUs = 0;
    uint64_t uptimeUs = 0;
};

struct TransmissionPolicyDecision {
    TransmissionPolicyState state = TransmissionPolicyState::normal;
    TransmissionPolicyAction action = TransmissionPolicyAction::discard;
    uint8_t anomalyCount = 0;
    uint8_t normalCount = 0;
};

class TransmissionPolicy {
public:
    explicit TransmissionPolicy(const TransmissionPolicyConfig& config)
        : config_(config) {}

    TransmissionPolicyDecision evaluate(const TransmissionPolicyInput& input);

private:
    bool isAnomaly(const WindowStats& stats) const;
    TransmissionPolicyDecision decision(TransmissionPolicyAction action) const;

    TransmissionPolicyConfig config_;
    TransmissionPolicyState state_ = TransmissionPolicyState::normal;
    uint8_t anomalyCount_ = 0;
    uint8_t normalCount_ = 0;
    uint64_t nextAnomalyUptimeUs_ = 0;
};

const char* transmissionPolicyStateName(TransmissionPolicyState state);
const char* transmissionPolicyActionName(TransmissionPolicyAction action);
