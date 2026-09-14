#include "diagnostics/serial_report.h"

#include <Arduino.h>

namespace {

const char* qualityName(CaptureQuality quality) {
    switch (quality) {
        case CaptureQuality::valid:
            return "valid";
        case CaptureQuality::fifo_overrun:
            return "fifo_overrun";
        case CaptureQuality::sensor_unavailable:
            return "sensor_unavailable";
        case CaptureQuality::timeout:
            return "timeout";
    }
    return "unknown";
}

}  // namespace

void printCaptureResult(const CaptureResult& result,
                        const VibrationWindow& window) {
    Serial.print("[WINDOW] quality=");
    Serial.print(qualityName(result.quality));
    Serial.print(" samples=");
    Serial.print(window.size());
    Serial.print(" elapsed_ms=");
    Serial.println(result.elapsedMs);

    if (!result.ok()) {
        return;
    }

    const WindowStats stats = window.summarize();
    Serial.print("[STATS] resultant_rms_g=");
    Serial.print(stats.resultantRmsG, 6);
    Serial.print(" rms_xyz_g=");
    Serial.print(stats.rmsXG, 6);
    Serial.print(",");
    Serial.print(stats.rmsYG, 6);
    Serial.print(",");
    Serial.print(stats.rmsZG, 6);
    Serial.print(" strongest_ac_peak_g=");
    Serial.println(stats.strongestAcPeakG, 6);
    Serial.print("[FEATURES] cf=");
    Serial.print(stats.features.cfA1, 6);
    Serial.print(",");
    Serial.print(stats.features.cfA2, 6);
    Serial.print(",");
    Serial.print(stats.features.cfA3, 6);
    Serial.print(" sk=");
    Serial.print(stats.features.skewnessA1, 6);
    Serial.print(",");
    Serial.print(stats.features.skewnessA2, 6);
    Serial.print(",");
    Serial.print(stats.features.skewnessA3, 6);
    Serial.print(" ku=");
    Serial.print(stats.features.kurtosisA1, 6);
    Serial.print(",");
    Serial.print(stats.features.kurtosisA2, 6);
    Serial.print(",");
    Serial.println(stats.features.kurtosisA3, 6);
}
