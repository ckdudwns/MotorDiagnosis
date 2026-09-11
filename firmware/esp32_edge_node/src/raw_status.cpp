#include "raw_status.h"

#include <cstring>

namespace RawStatus {

Kind classify(int status, const char* responseBody, bool strictAck) {
    if (status <= 0 || status == 408 || status == 429 || (status >= 500 && status <= 599))
        return Kind::Retryable;
    if (status == 200 || status == 202)
        return strictAck ? Kind::Accepted : Kind::ProtocolHold;
    if (status == 400 || status == 401 || status == 403)
        return Kind::ConfigurationHold;
    if (status == 409) {
        if (responseBody && std::strstr(responseBody, "WINDOW_SEQUENCE_CONFLICT"))
            return Kind::OrderingQuarantine;
        if (responseBody && std::strstr(responseBody, "WINDOW_CONFLICT"))
            return Kind::PermanentQuarantine;
        return Kind::UnknownHold;
    }
    return Kind::UnknownHold;
}

const char* name(Kind kind) {
    switch (kind) {
        case Kind::Accepted: return "accepted";
        case Kind::Retryable: return "retryable";
        case Kind::ProtocolHold: return "protocol_hold";
        case Kind::ConfigurationHold: return "configuration_hold";
        case Kind::PermanentQuarantine: return "permanent_quarantine";
        case Kind::OrderingQuarantine: return "ordering_quarantine";
        case Kind::UnknownHold: return "unknown_hold";
    }
    return "unknown";
}

bool retryable(Kind kind) {
    return kind == Kind::Retryable || kind == Kind::ProtocolHold ||
        kind == Kind::ConfigurationHold || kind == Kind::UnknownHold;
}

} // namespace RawStatus
