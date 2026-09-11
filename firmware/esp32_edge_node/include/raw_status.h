#pragma once

namespace RawStatus {

enum class Kind {
    Accepted,
    Retryable,
    ProtocolHold,
    ConfigurationHold,
    PermanentQuarantine,
    OrderingQuarantine,
    UnknownHold
};

Kind classify(int status, const char* responseBody, bool strictAck);
const char* name(Kind kind);
bool retryable(Kind kind);

} // namespace RawStatus
