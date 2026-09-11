#include "raw_ack.h"

#include <ArduinoJson.h>
#include <cstring>

namespace RawAck {

bool matches(
    int status,
    const char* responseJson,
    const char* expectedDeviceId,
    const Expected* expected,
    std::size_t count,
    std::size_t* matchedCount
) {
    if (matchedCount) *matchedCount = 0;
    if ((status != 200 && status != 202) || !responseJson || !expected || !count || !expectedDeviceId)
        return false;

    JsonDocument document;
    if (deserializeJson(document, responseJson, DeserializationOption::NestingLimit(5))) return false;

    // Server versions have returned accepted as either a count or a boolean;
    // the ACK identity list, never this summary field, authorizes deletion.
    const JsonVariantConst accepted = document["accepted"];
    if (!accepted.is<bool>() && !accepted.is<int>() && !accepted.is<unsigned>()) return false;
    if (document["deviceId"] != expectedDeviceId) return false;

    const JsonArrayConst acknowledged = document["acknowledged"].as<JsonArrayConst>();
    if (acknowledged.size() != count) {
        if (matchedCount) *matchedCount = acknowledged.size();
        return false;
    }

    bool matched[32] = {};
    if (count > sizeof(matched)) return false;
    for (const JsonVariantConst item : acknowledged) {
        if (!item.is<JsonObjectConst>()) return false;
        const char* bootId = item["bootId"].as<const char*>();
        if (!bootId || !item["windowIndex"].is<std::uint32_t>()) return false;
        const auto index = item["windowIndex"].as<std::uint32_t>();
        std::size_t found = count;
        for (std::size_t i = 0; i < count; ++i) {
            if (!matched[i] && expected[i].bootId && std::strcmp(bootId, expected[i].bootId) == 0 &&
                index == expected[i].windowIndex) {
                found = i;
                break;
            }
        }
        if (found == count) return false;
        matched[found] = true;
        if (matchedCount) ++*matchedCount;
    }
    for (std::size_t i = 0; i < count; ++i) if (!matched[i]) return false;
    return true;
}

} // namespace RawAck
