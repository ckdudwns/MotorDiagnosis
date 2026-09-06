#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace RemoteConfig {
constexpr std::uint32_t DEFAULT_INTERVAL_MS = 3000;
constexpr std::uint32_t DEFAULT_REPLAY_BATCH = 4;
constexpr std::size_t MAX_RESPONSE_BYTES = 2048;

struct Identity {
    const char* deviceId;
    const char* siteId;
    const char* assetId;
};

// One atomic NVS value contains BOTH the version and every live setting.
// Fixed-width fields and zero-filled strings keep CRC/readback deterministic.
struct Blob {
    std::uint32_t magic = 0;
    std::uint32_t schema = 0;
    std::uint32_t version = 0;
    std::uint32_t measurementIntervalMs = DEFAULT_INTERVAL_MS;
    std::uint32_t replayBatchSize = DEFAULT_REPLAY_BATCH;
    char commandId[36] = {};
    char deviceId[64] = {};
    char siteId[64] = {};
    char assetId[64] = {};
    std::uint32_t crc = 0;
};
static_assert(sizeof(Blob) == 252, "Remote configuration storage layout changed.");

enum class Status { NONE, APPLIED, FAILED, REJECTED };
struct Result {
    Status status = Status::NONE;
    std::uint32_t version = 0;
    std::string commandId;
    std::uint32_t measurementIntervalMs = 0;
    std::uint32_t replayBatchSize = 0;
    std::string errorCode;
};

bool validBlob(const Blob& blob, const Identity& identity);
std::string resultPayload(const Result& result);
bool resultAccepted(int httpStatus, const char* response, const char* deviceId,
                    const Result& result);

using Persist = bool (*)(const Blob&, void*);
class Controller {
public:
    const Blob& active() const { return active_; }
    bool restore(const Blob& blob, const Identity& identity);
    Result appliedResult() const;
    // Malformed/unscoped envelopes produce NONE; never apply guessed values.
    Result receive(const char* json, const Identity& identity, Persist persist,
                   void* context = nullptr);
private:
    Blob active_;
};

class Schedule {
public:
    bool due(std::uint32_t now) const;
    void completed(std::uint32_t now, bool success, bool reportPending);
private:
    bool attempted_ = false;
    std::uint32_t lastAttempt_ = 0;
    std::uint32_t waitMs_ = 0;
    std::uint32_t retryMs_ = 5000;
};
} // namespace RemoteConfig
