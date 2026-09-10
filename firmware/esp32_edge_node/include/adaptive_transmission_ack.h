#pragma once
#include <ArduinoJson.h>
#include <cstring>
#include <cstdint>
namespace AdaptiveTransmission {
template<class Records>
bool storageAck(int status,JsonVariantConst response,const Records& records,unsigned count) {
    if ((status!=200 && status!=202) || !count || !response.is<JsonObjectConst>() ||
        !response["accepted"].is<unsigned>()) return false;
    const unsigned accepted=response["accepted"].as<unsigned>();
    if (accepted>count || (status==200 && accepted!=0) || (status==202 && accepted==0)) return false;
    auto ack=response["acknowledged"].as<JsonArrayConst>();
    if (response["deviceId"]!=records[0].device || ack.size()!=count) return false;
    for (unsigned i=0;i<count;++i) {
        if (ack[i]["bootId"]!=records[i].bootId || !ack[i]["windowIndex"].is<std::uint32_t>() ||
            ack[i]["windowIndex"].as<std::uint32_t>()!=records[i].raw.index) return false;
        const char* digest=ack[i]["digest"].as<const char*>();
        if (!digest || strlen(digest)!=64) return false;
        for (unsigned j=0;j<64;++j) if (!((digest[j]>='0' && digest[j]<='9') || (digest[j]>='a' && digest[j]<='f'))) return false;
    }
    return true;
}
}
