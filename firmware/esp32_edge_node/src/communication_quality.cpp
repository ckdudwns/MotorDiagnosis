#include "communication_quality.h"
#include "device_health.h"
#include <ArduinoJson.h>
#include <algorithm>
#include <cstring>
#include <limits>

namespace CommunicationQuality {
namespace {
constexpr std::uint32_t MAGIC = 0x43515731; // CQW1
const char* const NAMES[] = {
    "attempts", "retries", "replayAttempts", "acknowledged", "transportFailures",
    "retryableResponses", "configurationFailures", "rejectedPackets",
    "ackLatencyTotalMs", "ackLatencyMaxMs", "bufferSamples", "bufferDepthSum",
    "bufferDepthMax", "bufferDepthLast", "bufferCapacity", "bufferDropped", "wifiSamples", "offlineSamples"
};
bool identifier(const char* text) {
    if (!text || !*text || std::strlen(text) > 63 ||
        (!(*text >= 'A' && *text <= 'Z') && !(*text >= '0' && *text <= '9'))) return false;
    for (const char* p = text; *p; ++p)
        if (!(*p >= 'A' && *p <= 'Z') && !(*p >= '0' && *p <= '9') && *p != '.' && *p != '_' && *p != '-') return false;
    return true;
}
bool bootIdValid(const char* text) {
    if (!text || std::strlen(text) != 32) return false;
    for (const char* p = text; *p; ++p) if (!(*p >= '0' && *p <= '9') && !(*p >= 'a' && *p <= 'f')) return false;
    return true;
}
bool storedText(const char* text, std::size_t length) {
    const auto* end = static_cast<const char*>(std::memchr(text, 0, length));
    if (!end) return false;
    for (; end < text + length; ++end) if (*end) return false;
    return true;
}
std::uint32_t checksum(const Window& window) {
    const auto* bytes = reinterpret_cast<const unsigned char*>(&window);
    std::uint32_t crc = 0xFFFFFFFF;
    for (std::size_t i = 0; i < offsetof(Window, crc); ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit) crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320U : 0);
    }
    return ~crc;
}
bool equalText(JsonVariantConst value, const char* text) {
    return value.is<const char*>() && value.as<JsonString>().size() == std::strlen(text) && std::strcmp(value.as<const char*>(), text) == 0;
}
bool sumValid(std::uint64_t total, std::uint64_t maximum, std::uint64_t samples) {
    // ceil(total / maximum), avoiding multiplication overflow and truncation.
    return total >= maximum && (maximum ? total / maximum + (total % maximum != 0) <= samples : total == 0);
}
} // namespace

bool validWindow(const Window& w, const Identity& identity) {
    if (w.magic != MAGIC || w.schema != 1 || w.reserved || !w.windowId || w.crc != checksum(w) ||
        !storedText(w.bootId, sizeof(w.bootId)) || !bootIdValid(w.bootId) ||
        !storedText(w.deviceId, sizeof(w.deviceId)) || !storedText(w.siteId, sizeof(w.siteId)) || !storedText(w.assetId, sizeof(w.assetId)) ||
        !identifier(identity.deviceId) || !identifier(identity.siteId) || !identifier(identity.assetId) ||
        std::strcmp(w.deviceId, identity.deviceId) || std::strcmp(w.siteId, identity.siteId) || std::strcmp(w.assetId, identity.assetId) ||
        w.endUptimeMs > MAX_INTEGER || w.endUptimeMs < w.startUptimeMs || w.endUptimeMs - w.startUptimeMs < INTERVAL_MS ||
        w.startedAtMs < 0 || w.endedAtMs <= w.startedAtMs ||
        static_cast<std::uint64_t>(w.endedAtMs - w.startedAtMs) != w.endUptimeMs - w.startUptimeMs) return false;
    for (const auto count : w.metrics) if (count > MAX_INTEGER) return false;
    const auto* m = w.metrics;
    return m[BUFFER_CAPACITY] >= 1 && m[BUFFER_CAPACITY] <= 1000000 && m[BUFFER_SAMPLES] &&
        m[BUFFER_DEPTH_LAST] <= m[BUFFER_DEPTH_MAX] && m[BUFFER_DEPTH_MAX] <= m[BUFFER_CAPACITY] &&
        sumValid(m[BUFFER_DEPTH_SUM], m[BUFFER_DEPTH_MAX], m[BUFFER_SAMPLES]) &&
        m[RETRIES] <= m[ATTEMPTS] && m[REPLAY_ATTEMPTS] <= m[ATTEMPTS] && m[OFFLINE_SAMPLES] <= m[WIFI_SAMPLES] &&
        m[ATTEMPTS] == m[ACKNOWLEDGED] + m[TRANSPORT_FAILURES] + m[RETRYABLE_RESPONSES] + m[CONFIGURATION_FAILURES] + m[REJECTED_PACKETS] &&
        (m[ACKNOWLEDGED] ? sumValid(m[ACK_LATENCY_TOTAL_MS], m[ACK_LATENCY_MAX_MS], m[ACKNOWLEDGED]) : !m[ACK_LATENCY_TOTAL_MS] && !m[ACK_LATENCY_MAX_MS]);
}

bool Collector::begin(const Identity& id, const char* bootId, std::uint64_t now, std::int64_t epoch,
                      std::size_t capacity, std::uint64_t dropped) {
    if (started_ || !identifier(id.deviceId) || !identifier(id.siteId) || !identifier(id.assetId) ||
        !bootIdValid(bootId) || epoch <= 0 || now > MAX_INTEGER || !capacity || capacity > 1000000) return false;
    active_ = Window{};
    active_.magic = MAGIC;
    std::strcpy(active_.deviceId, id.deviceId); std::strcpy(active_.siteId, id.siteId); std::strcpy(active_.assetId, id.assetId);
    std::strcpy(active_.bootId, bootId);
    active_.windowId = 1;
    active_.startUptimeMs = now;
    active_.startedAtMs = epoch;
    active_.metrics[BUFFER_CAPACITY] = capacity;
    lastDropped_ = dropped; // Persisted lifetime total is not a new boot's loss.
    started_ = true;
    return true;
}
void Collector::add(Counter counter, std::uint64_t value) {
    if (value > MAX_INTEGER - active_.metrics[counter]) overflowed_ = true;
    else active_.metrics[counter] += value;
}
void Collector::attempt(std::uint32_t sequence, bool replay, Outcome outcome, std::uint64_t elapsed) {
    if (!started_ || overflowed_) return;
    add(ATTEMPTS);
    if (attempted_ && lastSequence_ == sequence) add(RETRIES);
    if (replay) add(REPLAY_ATTEMPTS);
    attempted_ = true; lastSequence_ = sequence;
    switch (outcome) {
        case Outcome::ACK:
            add(ACKNOWLEDGED); add(ACK_LATENCY_TOTAL_MS, elapsed);
            active_.metrics[ACK_LATENCY_MAX_MS] = std::max(active_.metrics[ACK_LATENCY_MAX_MS], elapsed); break;
        case Outcome::TRANSPORT_FAILURE: add(TRANSPORT_FAILURES); break;
        case Outcome::RETRYABLE_RESPONSE: add(RETRYABLE_RESPONSES); break;
        case Outcome::CONFIGURATION_FAILURE: add(CONFIGURATION_FAILURES); break;
        case Outcome::PACKET_REJECT: add(REJECTED_PACKETS); break;
    }
}
void Collector::sampleBuffer(std::size_t depth, std::uint64_t dropped) {
    if (!started_ || overflowed_ || depth > active_.metrics[BUFFER_CAPACITY]) return;
    add(BUFFER_SAMPLES); add(BUFFER_DEPTH_SUM, depth);
    active_.metrics[BUFFER_DEPTH_LAST] = depth;
    active_.metrics[BUFFER_DEPTH_MAX] = std::max<std::uint64_t>(active_.metrics[BUFFER_DEPTH_MAX], depth);
    if (dropped >= lastDropped_) add(BUFFER_DROPPED, dropped - lastDropped_);
    lastDropped_ = dropped;
}
void Collector::sampleWifi(bool connected) {
    if (!started_ || overflowed_) return;
    add(WIFI_SAMPLES);
    if (!connected) add(OFFLINE_SAMPLES);
}
bool Collector::ready(std::uint64_t now) const {
    return started_ && !overflowed_ && active_.windowId && now <= MAX_INTEGER &&
        now >= active_.startUptimeMs && now - active_.startUptimeMs >= INTERVAL_MS;
}
bool Collector::freeze(std::uint64_t now, Window& output) {
    if (!ready(now) || !active_.metrics[BUFFER_SAMPLES]) return false;
    Window candidate = active_;
    candidate.endUptimeMs = now;
    if (now - active_.startUptimeMs > static_cast<std::uint64_t>(std::numeric_limits<std::int64_t>::max() - active_.startedAtMs)) return false;
    candidate.endedAtMs = active_.startedAtMs + (now - active_.startUptimeMs);
    candidate.crc = checksum(candidate);
    if (!validWindow(candidate, {active_.deviceId, active_.siteId, active_.assetId})) return false;
    output = candidate;
    const auto capacity = active_.metrics[BUFFER_CAPACITY];
    std::memset(active_.metrics, 0, sizeof(active_.metrics));
    active_.metrics[BUFFER_CAPACITY] = capacity;
    active_.startUptimeMs = now; active_.startedAtMs = candidate.endedAtMs;
    active_.windowId++; // Exhaustion disables freezing, never reuses a window ID.
    return true;
}

std::string payload(const Window& w) {
    if (!validWindow(w, {w.deviceId, w.siteId, w.assetId})) return {};
    JsonDocument doc;
    doc["schemaVersion"] = 1; doc["transport"] = "http";
    doc["deviceId"] = w.deviceId; doc["siteId"] = w.siteId; doc["assetId"] = w.assetId;
    doc["bootId"] = w.bootId; doc["windowId"] = w.windowId;
    doc["startUptimeMs"] = w.startUptimeMs; doc["endUptimeMs"] = w.endUptimeMs;
    doc["startedAt"] = DeviceHealth::utcTimestamp(w.startedAtMs);
    doc["endedAt"] = DeviceHealth::utcTimestamp(w.endedAtMs);
    for (std::size_t i = 0; i < COUNTER_COUNT; ++i) doc["metrics"][NAMES[i]] = w.metrics[i];
    std::string result; serializeJson(doc, result); return result;
}
bool accepted(int status, const char* json, const Window& w) {
    if ((status != 200 && status != 201) || !json || std::strlen(json) > 1024) return false;
    JsonDocument doc;
    if (deserializeJson(doc, json, DeserializationOption::NestingLimit(3)) || !doc.is<JsonObject>()) return false;
    const bool duplicate = doc["duplicate"].is<bool>() && doc["duplicate"].as<bool>();
    const bool stored = equalText(doc["disposition"], "stored");
    const bool expired = equalText(doc["disposition"], "expired");
    return doc["accepted"].is<bool>() && doc["accepted"].as<bool>() && doc["duplicate"].is<bool>() &&
        equalText(doc["deviceId"], w.deviceId) && equalText(doc["bootId"], w.bootId) &&
        !doc["windowId"].is<bool>() && doc["windowId"].is<std::uint32_t>() && doc["windowId"].as<std::uint32_t>() == w.windowId &&
        ((status == 201 && stored && !duplicate) || (status == 200 && ((stored && duplicate) || (expired && !duplicate))));
}
void Schedule::completed(std::uint32_t now, bool success) {
    attempted_ = true; last_ = now;
    if (success) { wait_ = 60000; retry_ = 5000; }
    else { wait_ = retry_; retry_ = std::min<std::uint32_t>(60000, retry_ * 2); }
}
bool Outbox::restore(const Window& window, const Identity& identity) {
    if (pending_ || !validWindow(window, identity)) return false;
    window_ = window; pending_ = durable_ = true; return true;
}
bool Outbox::capture(Collector& collector, std::uint64_t now) {
    if (pending_ || !collector.freeze(now, window_)) return false;
    pending_ = true; durable_ = false; return true;
}
bool Outbox::checkpoint(Persist persist, void* context) {
    if (!pending_) return false;
    if (!durable_ && persist) durable_ = persist(window_, context);
    return durable_;
}
bool Outbox::acknowledge(int status, const char* response, Erase erase, void* context) {
    if (!sendable() || !accepted(status, response, window_) || !erase || !erase(context)) return false;
    pending_ = durable_ = false;
    return true;
}
} // namespace CommunicationQuality
