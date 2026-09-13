#pragma once

#include <cstdint>
#include <ctime>

class WifiTimeSync {
public:
    void begin();
    void service();

    bool connected() const;
    bool timeSynced() const { return timeSynced_; }
    bool utcNow(time_t& value) const;
    bool utcNowUs(uint64_t& value) const;
    bool utcAtUptimeUs(uint64_t uptimeUs, uint64_t& value) const;

private:
    bool wasConnected_ = false;
    bool timeSyncRequested_ = false;
    bool timeSynced_ = false;
    uint32_t nextRetryAt_ = 0;
    uint32_t nextTimeCheckAt_ = 0;
    uint64_t syncEpochUs_ = 0;
    int64_t syncUptimeUs_ = 0;
};
