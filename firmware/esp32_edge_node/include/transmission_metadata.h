#pragma once

#include <ArduinoJson.h>
#include "adaptive_transmission.h"

namespace TransmissionMetadata {

constexpr const char* PolicyId = "edge-trigger-batch-v1";

inline void write(JsonObject transmission,
                  const char* mode,
                  AdaptiveTransmission::Reason reason,
                  const char* baselineId,
                  std::uint64_t droppedWindows) {
    transmission["policyId"] = PolicyId;
    transmission["mode"] = mode && *mode ? mode : "periodic";
    transmission["reason"] = AdaptiveTransmission::reasonName(reason);
    transmission["baselineId"] = baselineId && *baselineId ? baselineId : "unconfigured";
    transmission["droppedWindows"] = droppedWindows;
}

} // namespace TransmissionMetadata
