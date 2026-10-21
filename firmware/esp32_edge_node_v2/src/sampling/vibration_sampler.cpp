#include "sampling/vibration_sampler.h"

#include <Arduino.h>

CaptureResult VibrationSampler::capture(VibrationWindow& window,
                                        uint32_t timeoutMs) {
    window.clear();
    const uint32_t startedAt = millis();

    while (!window.full()) {
        if (millis() - startedAt > timeoutMs) {
            return {CaptureQuality::timeout, millis() - startedAt};
        }

        AxisSample sample{};
        switch (sensor_.readSample(sample)) {
            case SampleReadStatus::sample:
                window.add(sample);
                break;
            case SampleReadStatus::empty:
                yield();
                break;
            case SampleReadStatus::fifo_overrun:
                window.clear();
                return {CaptureQuality::fifo_overrun, millis() - startedAt};
            case SampleReadStatus::sensor_error:
                window.clear();
                return {CaptureQuality::sensor_unavailable,
                        millis() - startedAt};
        }
    }

    return {CaptureQuality::valid, millis() - startedAt};
}
