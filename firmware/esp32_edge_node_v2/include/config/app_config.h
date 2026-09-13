#pragma once

#include <cstdint>

namespace app {

constexpr uint8_t kAdxl345CsPin = 10;
constexpr uint8_t kAdxl345MosiPin = 11;
constexpr uint8_t kAdxl345SckPin = 12;
constexpr uint8_t kAdxl345MisoPin = 13;

constexpr uint16_t kVibrationSampleRateHz = 800;
constexpr uint16_t kVibrationSamplesPerWindow = 512;
constexpr float kAdxl345GPerCount = 0.0039F;
constexpr char kDeviceId[] = "DEV-01-MOT-02";
constexpr char kSiteId[] = "SITE-01";
constexpr char kAssetId[] = "SITE-01-MOT-02";
constexpr char kSensorId[] = "SENSOR-02";
constexpr char kFeatureProfileId[] = "adxl345-ac-cf-sk-ku-25s-v1";
constexpr char kPolicyId[] = "edge-feature-history-v1";

constexpr uint32_t kWindowCaptureTimeoutMs = 5000;
// Test-only policy knobs. Replace with calibrated motor thresholds later.
constexpr bool kAnomalyDetectionEnabled = false;
constexpr float kAnomalyResultantRmsThresholdG = 0.0F;
constexpr float kAnomalyStrongestAcPeakThresholdG = 0.0F;
constexpr bool kAnomalyUseOr = true;
constexpr uint8_t kAnomalyConsecutiveWindows = 3;
constexpr uint8_t kRecoveryConsecutiveWindows = 5;
constexpr uint32_t kSerialBaudRate = 115200;

}  // namespace app
