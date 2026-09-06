#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace CommunicationQuality {
constexpr std::uint64_t INTERVAL_MS = 60000;
constexpr std::uint64_t MAX_INTEGER = 9007199254740991ULL;
enum Counter : std::size_t {
    ATTEMPTS, RETRIES, REPLAY_ATTEMPTS, ACKNOWLEDGED, TRANSPORT_FAILURES,
    RETRYABLE_RESPONSES, CONFIGURATION_FAILURES, REJECTED_PACKETS,
    ACK_LATENCY_TOTAL_MS, ACK_LATENCY_MAX_MS, BUFFER_SAMPLES, BUFFER_DEPTH_SUM,
    BUFFER_DEPTH_MAX, BUFFER_DEPTH_LAST, BUFFER_CAPACITY, BUFFER_DROPPED,
    WIFI_SAMPLES, OFFLINE_SAMPLES, COUNTER_COUNT
};
enum class Outcome { ACK, TRANSPORT_FAILURE, RETRYABLE_RESPONSE, CONFIGURATION_FAILURE, PACKET_REJECT };
struct Identity { const char* deviceId; const char* siteId; const char* assetId; };

// A completed immutable window is one NVS value. No per-packet flash writes.
struct alignas(8) Window {
    std::uint32_t magic = 0;
    std::uint32_t schema = 1;
    char bootId[36] = {};
    std::uint32_t windowId = 0;
    char deviceId[64] = {}, siteId[64] = {}, assetId[64] = {};
    std::uint64_t startUptimeMs = 0, endUptimeMs = 0;
    std::int64_t startedAtMs = 0, endedAtMs = 0;
    std::uint64_t metrics[COUNTER_COUNT] = {};
    std::uint32_t reserved = 0; // Explicit padding keeps every uint64 aligned.
    std::uint32_t crc = 0;
};
static_assert(sizeof(Window) == 424 && offsetof(Window, metrics) == 272 && offsetof(Window, crc) == 420,
              "Quality NVS format changed");

bool validWindow(const Window& window, const Identity& identity);
std::string payload(const Window& window);
bool accepted(int status, const char* json, const Window& window);

class Collector {
public:
    bool begin(const Identity& identity, const char* bootId, std::uint64_t uptimeMs,
               std::int64_t epochMs, std::size_t capacity, std::uint64_t dropped);
    bool started() const { return started_; }
    bool overflowed() const { return overflowed_; }
    void attempt(std::uint32_t sequence, bool replay, Outcome outcome, std::uint64_t elapsedMs);
    void sampleBuffer(std::size_t depth, std::uint64_t dropped);
    void sampleWifi(bool connected);
    bool ready(std::uint64_t uptimeMs) const;
    // Caller owns/persists the frozen window. While one waits for ACK, keep
    // accumulating another interval in RAM; do not overwrite the frozen one.
    bool freeze(std::uint64_t uptimeMs, Window& output);
private:
    void add(Counter counter, std::uint64_t value = 1);
    Window active_;
    bool started_ = false, overflowed_ = false, attempted_ = false;
    std::uint32_t lastSequence_ = 0;
    std::uint64_t lastDropped_ = 0;
};

class Schedule {
public:
    bool due(std::uint32_t now) const { return !attempted_ || now - last_ >= wait_; }
    void completed(std::uint32_t now, bool success);
private:
    bool attempted_ = false;
    std::uint32_t last_ = 0, wait_ = 0, retry_ = 5000;
};

using Persist = bool (*)(const Window&, void*);
using Erase = bool (*)(void*);
class Outbox {
public:
    bool restore(const Window& window, const Identity& identity);
    bool capture(Collector& collector, std::uint64_t now);
    bool checkpoint(Persist persist, void* context = nullptr);
    bool acknowledge(int status, const char* response, Erase erase, void* context = nullptr);
    bool pending() const { return pending_; }
    bool sendable() const { return pending_ && durable_; }
    const Window& window() const { return window_; }
private:
    Window window_;
    bool pending_ = false, durable_ = false;
};
} // namespace CommunicationQuality
