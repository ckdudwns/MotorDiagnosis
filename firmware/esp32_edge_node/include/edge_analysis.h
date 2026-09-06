#pragma once
#include <cstddef>
#include <cstdint>
#include <string>

namespace EdgeAnalysis {
constexpr std::size_t MAX_FRAME_BYTES = 96 * 1024;
struct Features {
    double rms = 0, peak = 0, kurtosis = 0;
    bool hasKurtosis = false, valid = false;
    double bandEnergy[3] = {};
};
// Unwindowed, mean-removed, one-sided FFT energy; bands [0,fs/16),
// [fs/16,fs/8), [fs/8,fs/2]. DC excluded, Nyquist included.
Features summarize(const float* samples, std::size_t count, std::size_t block);
std::string frame(const char* telemetry, const double* x, const double* y,
                  const double* z, const std::int32_t* audio, const char* requestId);
bool accepted(int status, const char* response, std::uint32_t sequence);
std::uint32_t checksum(const char* bytes, std::size_t count);
}
