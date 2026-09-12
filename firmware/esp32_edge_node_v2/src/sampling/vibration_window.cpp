#include "sampling/vibration_window.h"

#include <cmath>

void VibrationWindow::clear() {
    count_ = 0;
}

bool VibrationWindow::add(const AxisSample& sample) {
    if (full()) {
        return false;
    }
    samples_[count_++] = sample;
    return true;
}

WindowStats VibrationWindow::summarize() const {
    WindowStats stats;
    stats.sampleCount = count_;
    if (count_ == 0) {
        return stats;
    }

    double sumX = 0.0;
    double sumY = 0.0;
    double sumZ = 0.0;
    for (uint16_t i = 0; i < count_; ++i) {
        sumX += samples_[i].x * app::kAdxl345GPerCount;
        sumY += samples_[i].y * app::kAdxl345GPerCount;
        sumZ += samples_[i].z * app::kAdxl345GPerCount;
    }

    const double meanX = sumX / count_;
    const double meanY = sumY / count_;
    const double meanZ = sumZ / count_;
    double sumSqX = 0.0;
    double sumSqY = 0.0;
    double sumSqZ = 0.0;
    double sumCubeX = 0.0;
    double sumCubeY = 0.0;
    double sumCubeZ = 0.0;
    double sumFourthX = 0.0;
    double sumFourthY = 0.0;
    double sumFourthZ = 0.0;

    for (uint16_t i = 0; i < count_; ++i) {
        const double x = samples_[i].x * app::kAdxl345GPerCount - meanX;
        const double y = samples_[i].y * app::kAdxl345GPerCount - meanY;
        const double z = samples_[i].z * app::kAdxl345GPerCount - meanZ;
        sumSqX += x * x;
        sumSqY += y * y;
        sumSqZ += z * z;
        sumCubeX += x * x * x;
        sumCubeY += y * y * y;
        sumCubeZ += z * z * z;
        sumFourthX += x * x * x * x;
        sumFourthY += y * y * y * y;
        sumFourthZ += z * z * z * z;
        stats.acPeakXG = fmaxf(stats.acPeakXG, static_cast<float>(fabs(x)));
        stats.acPeakYG = fmaxf(stats.acPeakYG, static_cast<float>(fabs(y)));
        stats.acPeakZG = fmaxf(stats.acPeakZG, static_cast<float>(fabs(z)));
    }

    stats.rmsXG = sqrtf(static_cast<float>(sumSqX / count_));
    stats.rmsYG = sqrtf(static_cast<float>(sumSqY / count_));
    stats.rmsZG = sqrtf(static_cast<float>(sumSqZ / count_));
    stats.resultantRmsG = sqrtf(stats.rmsXG * stats.rmsXG +
                                stats.rmsYG * stats.rmsYG +
                                stats.rmsZG * stats.rmsZG);
    stats.strongestAcPeakG = fmaxf(stats.acPeakXG,
                                   fmaxf(stats.acPeakYG, stats.acPeakZG));
    const double rmsX = sqrt(sumSqX / count_);
    const double rmsY = sqrt(sumSqY / count_);
    const double rmsZ = sqrt(sumSqZ / count_);
    constexpr double kFeatureEpsilon = 1.0e-12;
    const auto calculateFeatures = [kFeatureEpsilon](double rms, double peak,
                                                      double cube,
                                                      double fourth,
                                                      float& cf, float& skew,
                                                      float& kurtosis) {
        if (rms <= kFeatureEpsilon) {
            cf = 0.0F;
            skew = 0.0F;
            kurtosis = 0.0F;
            return;
        }
        cf = static_cast<float>(peak / rms);
        skew = static_cast<float>(cube / (rms * rms * rms));
        kurtosis = static_cast<float>(fourth /
                                      (rms * rms * rms * rms));
    };
    calculateFeatures(rmsX, stats.acPeakXG, sumCubeX / count_,
                      sumFourthX / count_, stats.features.cfA1,
                      stats.features.skewnessA1, stats.features.kurtosisA1);
    calculateFeatures(rmsY, stats.acPeakYG, sumCubeY / count_,
                      sumFourthY / count_, stats.features.cfA2,
                      stats.features.skewnessA2, stats.features.kurtosisA2);
    calculateFeatures(rmsZ, stats.acPeakZG, sumCubeZ / count_,
                      sumFourthZ / count_, stats.features.cfA3,
                      stats.features.skewnessA3, stats.features.kurtosisA3);
    stats.featuresValid = count_ == samples_.size();
    return stats;
}
