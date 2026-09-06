#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace DeviceHealth {

enum class Fault : std::uint8_t {
    ADXL_INIT, I2S_INIT, ADXL_CHANNEL, I2S_CHANNEL,
    VIBRATION_TIMEOUT, AUDIO_WINDOW, INVALID_FEATURES, COUNT
};
constexpr std::size_t FAULT_COUNT = static_cast<std::size_t>(Fault::COUNT);
constexpr std::size_t JOURNAL_CAPACITY = 32;

#pragma pack(push, 1)
struct Transition {
    std::uint8_t code = 0;
    std::uint8_t active = 0;
    std::uint16_t reserved = 0;
    std::uint32_t session = 0;
    std::uint64_t monotonicMs = 0;
    std::int64_t epochMs = 0; // 0 means unresolved, never a fabricated UTC time.
};
struct Journal {
    std::uint32_t magic = 0;
    std::uint16_t version = 0;
    std::uint16_t count = 0;
    std::uint32_t known = 0;
    std::int64_t acknowledgedTime[FAULT_COUNT] = {};
    Transition latest[FAULT_COUNT] = {};
    Transition pending[JOURNAL_CAPACITY] = {};
    std::uint32_t crc = 0;
};
#pragma pack(pop)

Journal emptyJournal();
bool validJournal(const Journal& journal);
// False leaves the journal untouched (invalid observation or queue full).
bool observe(Journal& journal, Fault fault, bool active, std::uint32_t session,
             std::uint64_t monotonicMs, std::int64_t epochMs);
bool acknowledgeHead(Journal& journal, std::uint32_t session = 0,
                     std::uint64_t monotonicMs = 0, std::int64_t reportedAtMs = 0);
const char* faultCode(Fault fault);
bool hasPcmVariation(const std::int32_t* samples, std::size_t count);

struct Metrics {
    int rssiDbm = -120;
    std::uint32_t rebootCount = 0;
    std::size_t queuedRecords = 0;
    std::size_t queueCapacity = 0;
    const char* firmwareVersion = nullptr;
};

std::string utcTimestamp(std::int64_t epochMs);
// One pending transition per request: retrying active+recovered in one batch
// would reopen an already closed backend event after ACK loss.
std::string payload(const Journal& journal, const Metrics& metrics,
                    std::uint32_t session, std::uint64_t monotonicMs,
                    std::int64_t epochMs);
bool accepted(int httpStatus, const char* response, const char* deviceId,
              std::int64_t reportedAtMs);
bool metricsChanged(const Metrics& previous, const Metrics& current);

class ReportSchedule {
public:
    bool due(std::uint32_t now, bool changed, bool pending) const;
    void completed(std::uint32_t now, bool success);
private:
    bool attempted_ = false;
    bool succeeded_ = false;
    std::uint32_t lastAttempt_ = 0;
    std::uint32_t lastSuccess_ = 0;
    std::uint32_t retryMs_ = 5000;
};

// At most three initialization attempts per boot, including runtime recovery.
// Exhaustion is latched; network/health servicing must continue.
class SensorRetry {
public:
    bool due(std::uint32_t now) const;
    void attempted(std::uint32_t now, bool success);
    void failed(std::uint32_t now);
    bool ready() const { return ready_; }
    unsigned attempts() const { return attempts_; }
private:
    unsigned attempts_ = 0;
    bool ready_ = false;
    std::uint32_t lastAttempt_ = 0;
};
} // namespace DeviceHealth
