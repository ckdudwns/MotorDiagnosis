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
// Authorization for NEW raw captures only; never applied to durable outbox replay.
class PendingRequest {
public:
    bool load(const char* response, const char* device, const char* site, const char* asset);
    const char* forCapture(std::uint64_t capturedEpoch, double nowEpoch);
    void clear();
private:
    std::string id_;
    double expiresAtEpoch_ = 0;
};
// Unwindowed, mean-removed, one-sided FFT energy; bands [0,fs/16),
// [fs/16,fs/8), [fs/8,fs/2]. DC excluded, Nyquist included.
Features summarize(const float* samples, std::size_t count, std::size_t block);
std::string frame(const char* telemetry, const double* x, const double* y,
                  const double* z, const std::int32_t* audio, const char* requestId);
bool accepted(int status, const char* response, std::uint32_t sequence);
std::uint32_t checksum(const char* bytes, std::size_t count);
}
