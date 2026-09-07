#pragma once

#include "device_health.h"

namespace ContinuousVibration {

// Owned exclusively by captureTask. The sink transfers transitions to the
// main task; only that task may mutate/persist the existing health journal.
class CaptureHealth {
public:
    enum class State { InitFailed, ChannelFailed, TimedOut, Healthy };

    static bool owns(DeviceHealth::Fault fault) {
        return fault == DeviceHealth::Fault::ADXL_INIT ||
               fault == DeviceHealth::Fault::ADXL_CHANNEL ||
               fault == DeviceHealth::Fault::VIBRATION_TIMEOUT;
    }

    template<class Sink>
    bool record(State state, std::uint64_t observedMs, Sink sink) {
        const DeviceHealth::Fault faults[] = {
            DeviceHealth::Fault::ADXL_INIT, DeviceHealth::Fault::ADXL_CHANNEL,
            DeviceHealth::Fault::VIBRATION_TIMEOUT
        };
        for (unsigned i = 0; i < 3; ++i) {
            const unsigned bit = 1U << i;
            // A failure does not clear another latched failure. Driver init
            // alone is not recovery: Healthy requires a complete FIFO window.
            if (state != State::Healthy && static_cast<unsigned>(state) != i) continue;
            const bool active = state != State::Healthy;
            if ((known_ & bit) && bool(active_ & bit) == active) continue;
            if (!sink(faults[i], active, observedMs)) return false;
            known_ |= bit;
            if (active) active_ |= bit;
            else active_ &= ~bit;
        }
        return true;
    }

private:
    unsigned known_ = 0;
    unsigned active_ = 0;
};
} // namespace ContinuousVibration
