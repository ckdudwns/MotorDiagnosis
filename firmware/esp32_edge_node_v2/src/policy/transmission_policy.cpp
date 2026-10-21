#include "policy/transmission_policy.h"

namespace {

uint8_t increment(uint8_t value) {
    return value == 255 ? value : static_cast<uint8_t>(value + 1);
}

}  // namespace

bool TransmissionPolicy::isAnomaly(const WindowStats& stats) const {
    if (!config_.anomalyDetectionEnabled) {
        return false;
    }

    const bool rmsExceeded =
        config_.resultantRmsThresholdG > 0.0F &&
        stats.resultantRmsG >= config_.resultantRmsThresholdG;
    const bool peakExceeded =
        config_.strongestAcPeakThresholdG > 0.0F &&
        stats.strongestAcPeakG >= config_.strongestAcPeakThresholdG;
    return config_.useOr ? (rmsExceeded || peakExceeded)
                         : (rmsExceeded && peakExceeded);
}

TransmissionPolicyDecision TransmissionPolicy::decision(
    TransmissionPolicyAction action) const {
    TransmissionPolicyDecision result;
    result.state = state_;
    result.action = action;
    result.anomalyCount = anomalyCount_;
    result.normalCount = normalCount_;
    return result;
}

TransmissionPolicyDecision TransmissionPolicy::evaluate(
    const TransmissionPolicyInput& input) {
    if (!input.valid) {
        // Invalid samples do not alter state or either consecutive counter.
        return decision(TransmissionPolicyAction::discard);
    }

    if (state_ == TransmissionPolicyState::normal) {
        if (isAnomaly(input.stats)) {
            anomalyCount_ = increment(anomalyCount_);
        } else {
            anomalyCount_ = 0;
        }

        if (anomalyCount_ >= config_.anomalyConsecutiveWindows &&
            config_.anomalyConsecutiveWindows > 0) {
            state_ = TransmissionPolicyState::anomalyActive;
            normalCount_ = 0;
            nextAnomalyUptimeUs_ = input.uptimeUs + config_.anomalyIntervalUs;
            return decision(TransmissionPolicyAction::anomalyStart);
        }
        return decision(TransmissionPolicyAction::discard);
    }

    if (isAnomaly(input.stats)) {
        normalCount_ = 0;
    } else {
        normalCount_ = increment(normalCount_);
    }

    if (normalCount_ >= config_.recoveryConsecutiveWindows &&
        config_.recoveryConsecutiveWindows > 0) {
        state_ = TransmissionPolicyState::normal;
        anomalyCount_ = 0;
        normalCount_ = 0;
        return decision(TransmissionPolicyAction::recovery);
    }

    if (input.uptimeUs >= nextAnomalyUptimeUs_) {
        do {
            nextAnomalyUptimeUs_ += config_.anomalyIntervalUs;
        } while (config_.anomalyIntervalUs > 0 &&
                 input.uptimeUs >= nextAnomalyUptimeUs_);
        return decision(TransmissionPolicyAction::anomalyActive);
    }

    return decision(TransmissionPolicyAction::discard);
}

const char* transmissionPolicyStateName(TransmissionPolicyState state) {
    return state == TransmissionPolicyState::anomalyActive ? "ANOMALY_ACTIVE"
                                                            : "NORMAL";
}

const char* transmissionPolicyActionName(TransmissionPolicyAction action) {
    switch (action) {
        case TransmissionPolicyAction::anomalyStart:
            return "anomaly_start";
        case TransmissionPolicyAction::anomalyActive:
            return "anomaly_active";
        case TransmissionPolicyAction::recovery:
            return "recovery";
        case TransmissionPolicyAction::discard:
        default:
            return "discard";
    }
}
