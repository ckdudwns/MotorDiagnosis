#pragma once

#include <cstdint>

#include "hardware/adxl345_sensor.h"
#include "sampling/vibration_window.h"

enum class CaptureQuality : uint8_t {
    valid,
    fifo_overrun,
    sensor_unavailable,
    timeout,
};

struct CaptureResult {
    CaptureResult() = default;
    CaptureResult(CaptureQuality captureQuality, uint32_t captureElapsedMs)
        : quality(captureQuality), elapsedMs(captureElapsedMs) {}

    CaptureQuality quality = CaptureQuality::timeout;
    uint32_t elapsedMs = 0;

    bool ok() const { return quality == CaptureQuality::valid; }
};

class VibrationSampler {
public:
    explicit VibrationSampler(Adxl345Sensor& sensor) : sensor_(sensor) {}

    CaptureResult capture(VibrationWindow& window,
                          uint32_t timeoutMs);

private:
    Adxl345Sensor& sensor_;
};
