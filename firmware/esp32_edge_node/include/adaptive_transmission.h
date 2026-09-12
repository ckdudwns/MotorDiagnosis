#pragma once
#include <cmath>
#include <cstdint>
#include "vibration_window.h"

namespace AdaptiveTransmission {
constexpr std::uint32_t NormalIntervalMs = 300000;
constexpr std::uint32_t ActiveIntervalMs = 10000;
constexpr unsigned BatchSize = 4;
constexpr unsigned PreContext = 3;
enum class Reason : std::uint8_t { None, Low, High, Severe, Quality, BaselineMissing, StoragePressure, RecoveryHold, Recovery };
inline const char* reasonName(Reason r) {
    const char* names[] = {"none", "rms_low", "rms_high", "severe", "quality",
                          "baseline_missing", "storage_pressure", "recovery_hold", "recovery"};
    return names[static_cast<unsigned>(r)];
}
struct Config {
    // Fixed commissioned baseline in AC g, not learned automatically at boot.
    double low=0, high=0, severeLow=0, severeHigh=0, severePeak=0;
    std::uint32_t recoveryMs=30000;
    bool valid() const {
        return std::isfinite(low) && std::isfinite(high) && std::isfinite(severeLow) &&
            std::isfinite(severeHigh) && std::isfinite(severePeak) &&
            0 <= severeLow && severeLow < low && low < high && high < severeHigh &&
            severeHigh > 0 && severePeak > 0 && recoveryMs >= 1920;
    }
};
class Detector {
    Config config_;
    unsigned abnormalCount_=0, normalCount_=0;
    std::uint32_t lastIndex_=0;
    std::uint64_t lastStart_=0, lastSentStart_=0;
    bool seen_=false, active_=false, sent_=false, shouldTransmit_=false;
    Reason reason_=Reason::BaselineMissing;
public:
    explicit Detector(Config config={}) : config_(config) {}
    bool configured() const { return config_.valid(); }
    bool active() const { return active_; }
    bool shouldTransmit() const { return shouldTransmit_; }
    Reason reason() const { return reason_; }
    Reason update(const VibrationWindow::Features& f) {
        const bool continuous=seen_ && f.index==lastIndex_+1 && f.startUs>=lastStart_ &&
            f.startUs-lastStart_>=630000 && f.startUs-lastStart_<=670000;
        const bool broken=seen_ && !continuous;
        if (!continuous) {abnormalCount_=normalCount_=0;}
        seen_=true; lastIndex_=f.index; lastStart_=f.startUs;
        shouldTransmit_=false;
        double sum=0, peak=0;
        bool finite=true;
        for (unsigned a=0;a<3;++a) {
            const double rms=f.values[a*7], p=f.values[a*7+1];
            finite=finite && std::isfinite(rms) && std::isfinite(p) && rms>=0 && p>=0;
            sum+=rms*rms; if (p>peak) peak=p;
        }
        const double rms=std::sqrt(sum);
        Reason trigger=Reason::None;
        if (broken || f.quality!=VibrationWindow::Quality::Valid || f.count!=512 || !finite || !std::isfinite(rms)) {
            abnormalCount_=normalCount_=0; trigger=Reason::Quality;
        } else if (!configured()) trigger=Reason::BaselineMissing;
        else {
            const bool abnormal=rms<config_.low || rms>config_.high;
            if (rms<config_.severeLow || rms>config_.severeHigh || peak>config_.severePeak) trigger=Reason::Severe;
            else if (abnormal) {
                normalCount_=0;
                if (!active_ && ++abnormalCount_>=3) trigger=rms<config_.low?Reason::Low:Reason::High;
            } else {
                abnormalCount_=0;
            }
        }
        if (trigger!=Reason::None) {
            const bool wasActive=active_;
            active_=true; normalCount_=0; reason_=trigger;
            if (!wasActive) {shouldTransmit_=true; lastSentStart_=f.startUs; sent_=true;}
            else if (f.startUs-lastSentStart_>=std::uint64_t(ActiveIntervalMs)*1000) {
                shouldTransmit_=true; lastSentStart_=f.startUs; sent_=true;
            }
        } else if (active_) {
            const bool normal=rms>=config_.low && rms<=config_.high;
            if (normal) ++normalCount_; else normalCount_=0;
            if (normalCount_>=5) {
                active_=false; reason_=Reason::Recovery;
                shouldTransmit_=true; lastSentStart_=f.startUs; sent_=true;
            } else {
                reason_=Reason::RecoveryHold;
                if (f.startUs-lastSentStart_>=std::uint64_t(ActiveIntervalMs)*1000) {
                    shouldTransmit_=true; lastSentStart_=f.startUs; sent_=true;
                }
            }
        } else {
            reason_=Reason::None;
            if (!sent_) {lastSentStart_=f.startUs; sent_=true;}
            else if (f.startUs-lastSentStart_>=std::uint64_t(NormalIntervalMs)*1000) {
                shouldTransmit_=true; lastSentStart_=f.startUs;
            }
        }
        return reason_;
    }
};
inline std::uint32_t crc32(const void* data, unsigned size) {
    const auto* bytes=static_cast<const unsigned char*>(data);
    std::uint32_t crc=0xffffffffU;
    for (unsigned i=0;i<size;++i) {
        crc^=bytes[i];
        for (unsigned bit=0;bit<8;++bit) crc=(crc>>1U)^(0xedb88320U & (0U-(crc&1U)));
    }
    return ~crc;
}
struct Slot {
    std::uint64_t order; std::uint32_t index;
    bool used, current, priority, resolved;
    Slot(std::uint64_t o=0,std::uint32_t i=0,bool u=false,bool c=false,bool p=false,bool r=true)
        : order(o),index(i),used(u),current(c),priority(p),resolved(r) {}
};
inline unsigned selectSlot(const Slot* slots,unsigned capacity,bool priority,
                           std::uint64_t after,std::uint64_t cutoff) {
    unsigned best=capacity;
    for (unsigned i=0;i<capacity;++i) {
        const auto& s=slots[i];
        if (!s.used || (!s.current && !s.resolved) || s.order<=after ||
            (priority?!s.priority:s.order>cutoff)) continue;
        if (best==capacity || s.order<slots[best].order) best=i;
    }
    return best;
}
// Summary replay and fresh summary enqueue run in that order in the main loop.
// Keep this dispatcher portable so tests exercise the same ordering as firmware.
class SummarySchedule {
    std::uint32_t last_;
    bool draining_=false;
    bool due(std::uint32_t now,bool empty,bool priority) {
        if (priority) return true;
        if (now-last_>=NormalIntervalMs) draining_=true;
        if (draining_ && empty) {draining_=false; last_=now;}
        return draining_;
    }
public:
    explicit SummarySchedule(std::uint32_t now=0):last_(now) {}
    template<typename Clock,typename Empty,typename Replay>
    bool replayIfDue(Clock clock,Empty empty,Replay replay,bool priority=false) {
        if (!due(clock(),empty(),priority) || empty()) return false;
        replay();
        // Observe completion before the caller appends its fresh measurement.
        // Failed/partial replay keeps the drain active and the source queued.
        if (empty()) {draining_=false; last_=clock();}
        return true;
    }
};
class FlushSchedule {
    std::uint32_t last_;
    std::uint64_t cutoff_=0;
public:
    explicit FlushSchedule(std::uint32_t now=0):last_(now) {}
    std::uint64_t cutoff() const {return cutoff_;}
    void poll(std::uint32_t now,std::uint64_t newest,bool pressure=false) {
        if (!cutoff_ && (pressure || now-last_>=NormalIntervalMs)) {cutoff_=newest; last_=now;}
    }
    void drained() {cutoff_=0;}
};
} // namespace AdaptiveTransmission
