#pragma once
// Local commissioning values only; no guessed field thresholds in source.
#if __has_include("adaptive_transmission.local.h")
#include "adaptive_transmission.local.h"
#endif
#ifndef ADAPTIVE_BASELINE_ID
#define ADAPTIVE_BASELINE_ID "unconfigured"
#endif
#ifndef ADAPTIVE_RMS_LOW_G
#define ADAPTIVE_RMS_LOW_G 0.0
#define ADAPTIVE_RMS_HIGH_G 0.0
#define ADAPTIVE_SEVERE_LOW_G 0.0
#define ADAPTIVE_SEVERE_HIGH_G 0.0
#define ADAPTIVE_SEVERE_PEAK_G 0.0
#endif
#ifndef ADAPTIVE_RECOVERY_MS
#define ADAPTIVE_RECOVERY_MS 30000
#endif
inline AdaptiveTransmission::Config adaptiveConfig() {
    AdaptiveTransmission::Config c;
    c.low=ADAPTIVE_RMS_LOW_G; c.high=ADAPTIVE_RMS_HIGH_G;
    c.severeLow=ADAPTIVE_SEVERE_LOW_G; c.severeHigh=ADAPTIVE_SEVERE_HIGH_G;
    c.severePeak=ADAPTIVE_SEVERE_PEAK_G; c.recoveryMs=ADAPTIVE_RECOVERY_MS;
    if (!strcmp(ADAPTIVE_BASELINE_ID,"unconfigured") || !strlen(ADAPTIVE_BASELINE_ID) || strlen(ADAPTIVE_BASELINE_ID)>64)
        return AdaptiveTransmission::Config{};
    return c;
}
