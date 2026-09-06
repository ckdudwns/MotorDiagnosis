#include "remote_config.h"
#include <ArduinoJson.h>
#include <algorithm>
#include <cstring>

namespace RemoteConfig {
namespace {
constexpr std::uint32_t MAGIC = 0x4D524331U; // MRC1
constexpr std::uint32_t MAX_VERSION = 2147483647U;
bool settingsValid(std::uint32_t interval, std::uint32_t batch) {
    return interval >= 3000 && interval <= 60000 && batch >= 1 && batch <= 4;
}
bool identifier(const char* value) {
    if (!value || !*value || std::strlen(value) > 63) return false;
    if (!(*value >= 'A' && *value <= 'Z') && !(*value >= '0' && *value <= '9')) return false;
    for (const char* p = value; *p; ++p)
        if (!(*p >= 'A' && *p <= 'Z') && !(*p >= '0' && *p <= '9') &&
            *p != '-' && *p != '_' && *p != '.') return false;
    return true;
}
bool commandIdValid(const char* value) {
    if (!value || std::strlen(value) != 32) return false;
    for (const char* p = value; *p; ++p)
        if (!(*p >= '0' && *p <= '9') && !(*p >= 'a' && *p <= 'f')) return false;
    return true;
}
bool storedText(const char* value, std::size_t capacity) {
    const char* end = static_cast<const char*>(std::memchr(value, 0, capacity));
    if (!end) return false;
    for (; end != value + capacity; ++end) if (*end) return false;
    return true;
}
std::uint32_t checksum(const Blob& blob) {
    const auto* bytes = reinterpret_cast<const unsigned char*>(&blob);
    std::uint32_t crc = 0xFFFFFFFFU;
    for (std::size_t i = 0; i < offsetof(Blob, crc); ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit)
            crc = (crc >> 1) ^ ((crc & 1) ? 0xEDB88320U : 0);
    }
    return ~crc;
}
const char* statusText(Status status) {
    switch (status) {
        case Status::APPLIED: return "applied";
        case Status::FAILED: return "failed";
        case Status::REJECTED: return "rejected";
        default: return "";
    }
}
bool equalText(JsonVariantConst value, const char* expected) {
    return value.is<const char*>() && expected &&
        value.as<JsonString>().size() == std::strlen(expected) &&
        std::strcmp(value.as<const char*>(), expected) == 0;
}
bool unsignedInteger(JsonVariantConst value) {
    return !value.is<bool>() && value.is<std::uint32_t>();
}
} // namespace

bool validBlob(const Blob& blob, const Identity& identity) {
    return blob.magic == MAGIC && blob.schema == 1 && blob.version >= 1 &&
        blob.version <= MAX_VERSION && settingsValid(blob.measurementIntervalMs, blob.replayBatchSize) &&
        storedText(blob.commandId, sizeof(blob.commandId)) && commandIdValid(blob.commandId) &&
        storedText(blob.deviceId, sizeof(blob.deviceId)) &&
        storedText(blob.siteId, sizeof(blob.siteId)) &&
        storedText(blob.assetId, sizeof(blob.assetId)) &&
        identifier(identity.deviceId) && identifier(identity.siteId) && identifier(identity.assetId) &&
        std::strcmp(blob.deviceId, identity.deviceId) == 0 &&
        std::strcmp(blob.siteId, identity.siteId) == 0 &&
        std::strcmp(blob.assetId, identity.assetId) == 0 && blob.crc == checksum(blob);
}

bool Controller::restore(const Blob& blob, const Identity& identity) {
    if (!validBlob(blob, identity)) return false;
    active_ = blob;
    return true;
}

Result Controller::appliedResult() const {
    Result result;
    if (!active_.version) return result;
    result.status = Status::APPLIED;
    result.version = active_.version;
    result.commandId = active_.commandId;
    result.measurementIntervalMs = active_.measurementIntervalMs;
    result.replayBatchSize = active_.replayBatchSize;
    return result;
}

Result Controller::receive(const char* json, const Identity& identity, Persist persist, void* context) {
    Result result;
    if (!json || std::strlen(json) > MAX_RESPONSE_BYTES ||
        !identifier(identity.deviceId) || !identifier(identity.siteId) || !identifier(identity.assetId)) return result;
    JsonDocument document;
    if (deserializeJson(document, json, DeserializationOption::NestingLimit(5)) ||
        !document.is<JsonObject>() || document.size() != 5 ||
        !unsignedInteger(document["schemaVersion"]) || document["schemaVersion"].as<std::uint32_t>() != 1 ||
        !equalText(document["deviceId"], identity.deviceId) ||
        !equalText(document["siteId"], identity.siteId) ||
        !equalText(document["assetId"], identity.assetId) || !document["desired"].is<JsonObject>()) return result;
    JsonObjectConst desired = document["desired"].as<JsonObjectConst>();
    if (!unsignedInteger(desired["version"]) || desired["version"].as<std::uint32_t>() < 1 ||
        desired["version"].as<std::uint32_t>() > MAX_VERSION ||
        !desired["commandId"].is<const char*>() || !commandIdValid(desired["commandId"])) return result;
    result.version = desired["version"];
    result.commandId = desired["commandId"].as<const char*>();
    result.status = Status::REJECTED;
    result.errorCode = "invalid_config";
    JsonObjectConst settings = desired["settings"].as<JsonObjectConst>();
    if (desired.size() != 3 || settings.isNull() || settings.size() != 2 ||
        !unsignedInteger(settings["measurementIntervalMs"]) || !unsignedInteger(settings["replayBatchSize"])) return result;
    const std::uint32_t interval = settings["measurementIntervalMs"];
    const std::uint32_t batch = settings["replayBatchSize"];
    if (!settingsValid(interval, batch)) return result;
    if (result.version < active_.version) {
        result.errorCode = "stale_version";
        return result;
    }
    if (result.version == active_.version) {
        if (result.commandId == active_.commandId && interval == active_.measurementIntervalMs && batch == active_.replayBatchSize)
            return appliedResult(); // ACK retry: no flash write, no reapplication.
        result.errorCode = "version_conflict";
        return result;
    }
    Blob candidate;
    candidate.magic = MAGIC;
    candidate.schema = 1;
    candidate.version = result.version;
    candidate.measurementIntervalMs = interval;
    candidate.replayBatchSize = batch;
    std::strcpy(candidate.commandId, result.commandId.c_str());
    std::strcpy(candidate.deviceId, identity.deviceId);
    std::strcpy(candidate.siteId, identity.siteId);
    std::strcpy(candidate.assetId, identity.assetId);
    candidate.crc = checksum(candidate);
    if (!persist || !persist(candidate, context)) {
        result.status = Status::FAILED;
        result.errorCode = "storage_failure";
        return result; // Active runtime values stay unchanged on failure.
    }
    active_ = candidate;
    return appliedResult();
}

std::string resultPayload(const Result& result) {
    if (result.status == Status::NONE || !result.version || !commandIdValid(result.commandId.c_str())) return {};
    JsonDocument document;
    document["version"] = result.version;
    document["commandId"] = result.commandId;
    document["status"] = statusText(result.status);
    if (result.status == Status::APPLIED) {
        document["settings"]["measurementIntervalMs"] = result.measurementIntervalMs;
        document["settings"]["replayBatchSize"] = result.replayBatchSize;
        document["errorCode"] = nullptr;
    } else {
        document["settings"] = nullptr;
        document["errorCode"] = result.errorCode;
    }
    std::string output;
    serializeJson(document, output);
    return output;
}

bool resultAccepted(int httpStatus, const char* response, const char* deviceId, const Result& result) {
    if (httpStatus != 200 || !response || std::strlen(response) > MAX_RESPONSE_BYTES || result.status == Status::NONE) return false;
    JsonDocument document;
    return !deserializeJson(document, response, DeserializationOption::NestingLimit(3)) &&
        document.is<JsonObject>() && document["accepted"].is<bool>() && document["accepted"].as<bool>() &&
        equalText(document["deviceId"], deviceId) && unsignedInteger(document["version"]) &&
        document["version"].as<std::uint32_t>() == result.version &&
        equalText(document["commandId"], result.commandId.c_str()) && equalText(document["status"], statusText(result.status));
}

bool Schedule::due(std::uint32_t now) const {
    return !attempted_ || now - lastAttempt_ >= waitMs_;
}
void Schedule::completed(std::uint32_t now, bool success, bool reportPending) {
    attempted_ = true;
    lastAttempt_ = now;
    if (success) {
        retryMs_ = 5000;
        waitMs_ = reportPending ? 1000 : 30000;
    } else {
        waitMs_ = retryMs_;
        retryMs_ = std::min<std::uint32_t>(60000, retryMs_ * 2);
    }
}
} // namespace RemoteConfig
