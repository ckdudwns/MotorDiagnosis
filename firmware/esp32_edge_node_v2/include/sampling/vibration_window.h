#pragma once

#include <array>
#include <cstdint>

#include "config/app_config.h"

struct AxisSample {
    int16_t x;
    int16_t y;
    int16_t z;
};

struct VibrationFeatures {
    float cfA1 = 0.0F;
    float cfA2 = 0.0F;
    float cfA3 = 0.0F;
    float skewnessA1 = 0.0F;
    float skewnessA2 = 0.0F;
    float skewnessA3 = 0.0F;
    float kurtosisA1 = 0.0F;
    float kurtosisA2 = 0.0F;
    float kurtosisA3 = 0.0F;
};

struct WindowStats {
    uint16_t sampleCount = 0;
    bool featuresValid = false;
    float rmsXG = 0.0F;
    float rmsYG = 0.0F;
    float rmsZG = 0.0F;
    float resultantRmsG = 0.0F;
    float acPeakXG = 0.0F;
    float acPeakYG = 0.0F;
    float acPeakZG = 0.0F;
    float strongestAcPeakG = 0.0F;
    VibrationFeatures features;
};

class VibrationWindow {
public:
    void clear();
    bool add(const AxisSample& sample);
    bool full() const { return count_ == samples_.size(); }
    uint16_t size() const { return count_; }
    const AxisSample* data() const { return samples_.data(); }

    WindowStats summarize() const;

private:
    std::array<AxisSample, app::kVibrationSamplesPerWindow> samples_{};
    uint16_t count_ = 0;
};
