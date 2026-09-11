#pragma once
// Included by main.cpp after sensor helpers: one SPI owner, separate compute
// and network tasks. No LittleFS, NVS, HTTPS or feature FFT in the FIFO reader.
#include "vibration_window.h"
#include "continuous_vibration_health.h"
#include "raw_ack.h"
#include "raw_status.h"
#include "periodic_scheduler.h"
#include "priority_selector.h"
#include "transmission_metadata.h"
#include <mbedtls/base64.h>
#include <esp_heap_caps.h>
#include <cstring>
#include <new>
#include <iterator>
#include <sys/stat.h>

#ifndef RAW_VIBRATION_ENABLED
#define RAW_VIBRATION_ENABLED 0
#endif

#ifndef CONTINUOUS_VIBRATION_ENABLED
#define CONTINUOUS_VIBRATION_ENABLED 0
#endif
#ifndef RAW_VIBRATION_DEBUG
#define RAW_VIBRATION_DEBUG 0
#endif

#if CONTINUOUS_VIBRATION_ENABLED
bool beginBackendHttp(BackendHttp&, WiFiClientSecure&, const char*);
extern const AdaptiveTransmission::Config adaptiveTransmissionConfig;
extern bool adaptiveBaselineConfigured;
extern TaskHandle_t networkTaskHandle;
void logStorageMemoryDiagnostic(const char* phase);
bool checkAudioRingCanary();
namespace ContinuousVibration {
using namespace VibrationWindow;
extern SemaphoreHandle_t mutex;
// Keep several completed windows available so capture never immediately
// blocks behind feature extraction or a temporarily full pending queue.
constexpr unsigned RawQueueCapacity = 8;
#if RAW_VIBRATION_ENABLED
constexpr unsigned PendingCapacity = 8; // PSRAM-backed processing keeps capture from stalling.
constexpr unsigned RawHoldCapacity = 8;
// Server accepts up to four raw windows per request (64 KiB body limit).
constexpr unsigned BatchCapacity = 4;
constexpr size_t MaxRawRequestBytes = 64 * 1024;
using PendingWindow = Raw;
constexpr size_t RawBytesCapacity = Samples*3*2;
constexpr size_t EncodedRawCapacity = RawBytesCapacity*4/3+1;
    // 512 slots cover the 5-minute backlog target; free-byte checks still
    // cap the actual count for the installed LittleFS partition.
    constexpr unsigned RawSpoolSlots = 512;
    constexpr std::uint32_t RawSpoolMagic = 0x52535031UL; // "RSP1"
    constexpr std::uint16_t RawSpoolVersion = 1;
    constexpr std::uint8_t RawSpoolQuarantined = 1;
struct __attribute__((packed)) RawSpoolHeader {
    std::uint32_t magic = RawSpoolMagic;
    std::uint16_t version = RawSpoolVersion;
    std::uint16_t count = 0;
    std::uint32_t index = 0;
    std::uint64_t startUs = 0;
    std::uint8_t quality = 0;
    // reserved[0..1] carry policy selection state while preserving the
    // existing v1 slot size and backwards-compatible periodic defaults.
    std::uint8_t reserved[3] = {};
    char bootId[33] = {};
    std::uint32_t crc = 0;
};
constexpr size_t RawSpoolSlotBytes = sizeof(RawSpoolHeader) + RawBytesCapacity;
static_assert(sizeof(RawSpoolHeader) == 61, "Raw spool header must remain fixed.");
std::atomic<unsigned> rawSpoolNextSlot{0};
constexpr size_t ObservedUnsafeRawSpoolFreeBytes = 4096U;
std::atomic<bool> rawSpoolWritesSuspended{false};
#else
constexpr unsigned PendingCapacity = 64; // 40.96 s, internal RAM only.
constexpr unsigned BatchCapacity = 8;
using PendingWindow = Features;
constexpr unsigned BatchIntervalMs = 1500;
#endif
Queue<PendingWindow, PendingCapacity> pending;

struct StorageTiming {
    std::uint32_t lockWaitUs = 0;
    std::uint32_t slotLookupUs = 0;
    std::uint32_t openUs = 0;
    std::uint32_t writeUs = 0;
    std::uint32_t flushCloseUs = 0;
    std::uint32_t renameUs = 0;
    std::uint32_t totalUs = 0;
};

struct DropMetrics {
    std::atomic<std::uint64_t> capture_queue_full{0};
    std::atomic<std::uint64_t> processing_pending_full{0};
    std::atomic<std::uint64_t> processing_pending_full_unique{0};
    std::atomic<std::uint64_t> actual_drop_total{0};
    std::atomic<std::uint64_t> raw_hold_full{0};
    std::atomic<std::uint64_t> spool_write_fail{0};
    std::atomic<std::uint64_t> spool_capacity_full{0};
    std::atomic<std::uint64_t> spool_corrupt_or_unreadable{0};
    std::atomic<std::uint32_t> raw_queue_high_water{0};
    std::atomic<std::uint32_t> pending_high_water{0};
    std::atomic<std::uint32_t> raw_hold_high_water{0};
    std::atomic<std::uint32_t> storage_samples{0};
    std::atomic<std::uint32_t> storage_lock_wait_us{0};
    std::atomic<std::uint32_t> storage_slot_lookup_us{0};
    std::atomic<std::uint32_t> storage_open_us{0};
    std::atomic<std::uint32_t> storage_write_us{0};
    std::atomic<std::uint32_t> storage_flush_close_us{0};
    std::atomic<std::uint32_t> storage_rename_us{0};
    std::atomic<std::uint32_t> storage_total_us{0};
    std::atomic<std::uint32_t> storage_lock_wait_max_us{0};
    std::atomic<std::uint32_t> storage_slot_lookup_max_us{0};
    std::atomic<std::uint32_t> storage_open_max_us{0};
    std::atomic<std::uint32_t> storage_write_max_us{0};
    std::atomic<std::uint32_t> storage_flush_close_max_us{0};
    std::atomic<std::uint32_t> storage_rename_max_us{0};
    std::atomic<std::uint32_t> storage_total_max_us{0};
    std::atomic<std::uint64_t> storage_total_le_640ms{0};
    std::atomic<std::uint64_t> storage_total_641_to_1280ms{0};
    std::atomic<std::uint64_t> storage_total_gt_1280ms{0};
};

DropMetrics dropMetrics;

void recordStorageTiming(const StorageTiming& timing) {
    const auto updateMax = [](std::atomic<std::uint32_t>& target, std::uint32_t value) {
        auto observed = target.load(std::memory_order_relaxed);
        while (observed < value &&
               !target.compare_exchange_weak(observed, value,
                                             std::memory_order_relaxed,
                                             std::memory_order_relaxed)) {}
    };
    dropMetrics.storage_lock_wait_us.store(timing.lockWaitUs, std::memory_order_relaxed);
    dropMetrics.storage_slot_lookup_us.store(timing.slotLookupUs, std::memory_order_relaxed);
    dropMetrics.storage_open_us.store(timing.openUs, std::memory_order_relaxed);
    dropMetrics.storage_write_us.store(timing.writeUs, std::memory_order_relaxed);
    dropMetrics.storage_flush_close_us.store(timing.flushCloseUs, std::memory_order_relaxed);
    dropMetrics.storage_rename_us.store(timing.renameUs, std::memory_order_relaxed);
    dropMetrics.storage_total_us.store(timing.totalUs, std::memory_order_relaxed);
    updateMax(dropMetrics.storage_lock_wait_max_us, timing.lockWaitUs);
    updateMax(dropMetrics.storage_slot_lookup_max_us, timing.slotLookupUs);
    updateMax(dropMetrics.storage_open_max_us, timing.openUs);
    updateMax(dropMetrics.storage_write_max_us, timing.writeUs);
    updateMax(dropMetrics.storage_flush_close_max_us, timing.flushCloseUs);
    updateMax(dropMetrics.storage_rename_max_us, timing.renameUs);
    updateMax(dropMetrics.storage_total_max_us, timing.totalUs);
    if (timing.totalUs <= 640000U)
        ++dropMetrics.storage_total_le_640ms;
    else if (timing.totalUs <= 1280000U)
        ++dropMetrics.storage_total_641_to_1280ms;
    else
        ++dropMetrics.storage_total_gt_1280ms;
    dropMetrics.storage_samples.fetch_add(1, std::memory_order_relaxed);
}

#if RAW_VIBRATION_ENABLED
extern char boot[33];

struct RawSelectorDiag {
    std::uint32_t event = 0;
    const char* mode = "periodic";
    std::uint64_t nowUs = 0;
    std::uint64_t cutoffUs = UINT64_MAX;
    std::uint32_t slotsChecked = 0;
    std::uint32_t headersPresent = 0;
    std::uint32_t headerReadFail = 0;
    std::uint32_t eligible = 0;
    std::uint32_t selected = 0;
    std::uint32_t cutoffRejected = 0;
    std::uint32_t currentBootRecords = 0;
    std::uint32_t restoredBootRecords = 0;
    std::uint32_t statTotalUs = 0;
    std::uint32_t statMaxUs = 0;
    std::uint32_t headerTotalUs = 0;
    std::uint32_t headerMaxUs = 0;
    std::uint32_t payloadTotalUs = 0;
    std::uint32_t payloadMaxUs = 0;
    std::uint32_t lockWaitUs = 0;
    std::uint64_t firstRestoredStartUs = 0;
    std::uint64_t startedUs = 0;
};

std::uint32_t rawSelectorEvent = 0;
std::atomic<std::int64_t> rawTimestampEpochOffsetUs{0};

void logRawSelectorDiag(const RawSelectorDiag& diag) {
    const auto elapsedUs = static_cast<std::uint64_t>(esp_timer_get_time()) - diag.startedUs;
    Serial.printf(
        "[RAW-SELECT-DIAG] event=%lu mode=%s now_us=%llu cutoff_us=%llu "
        "slots=%u present=%u read_fail=%u eligible=%u selected=%u rejected=%u "
        "current_boot=%u restored_boot=%u stat_total_us=%lu stat_max_us=%lu "
        "header_total_us=%lu header_max_us=%lu payload_total_us=%lu payload_max_us=%lu "
        "lock_wait_us=%lu elapsed_us=%llu\n",
        static_cast<unsigned long>(diag.event), diag.mode,
        static_cast<unsigned long long>(diag.nowUs),
        static_cast<unsigned long long>(diag.cutoffUs), diag.slotsChecked,
        diag.headersPresent, diag.headerReadFail, diag.eligible, diag.selected,
        diag.cutoffRejected, diag.currentBootRecords, diag.restoredBootRecords,
        static_cast<unsigned long>(diag.statTotalUs),
        static_cast<unsigned long>(diag.statMaxUs),
        static_cast<unsigned long>(diag.headerTotalUs),
        static_cast<unsigned long>(diag.headerMaxUs),
        static_cast<unsigned long>(diag.payloadTotalUs),
        static_cast<unsigned long>(diag.payloadMaxUs),
        static_cast<unsigned long>(diag.lockWaitUs),
        static_cast<unsigned long long>(elapsedUs));
    if (diag.restoredBootRecords) {
        Serial.printf("[RAW-TIME-DIAG] mapping=unproven restored_boot_records=%u "
                      "header_start_us=%llu timestamp_sample_us=%llu "
                      "current_uptime_us=%llu current_epoch_offset_us=%lld\n",
                      diag.restoredBootRecords,
                      static_cast<unsigned long long>(diag.firstRestoredStartUs),
                      static_cast<unsigned long long>(diag.firstRestoredStartUs),
                      static_cast<unsigned long long>(esp_timer_get_time()),
                      static_cast<long long>(rawTimestampEpochOffsetUs.load(
                          std::memory_order_relaxed)));
    }
}

bool sameRawIdentity(const Raw& left, const Raw& right) {
    return left.index == right.index && left.startUs == right.startUs;
}

unsigned selectPriorityPending(Raw* destination, unsigned capacity) {
    RawSelectorDiag diag;
    diag.event = ++rawSelectorEvent;
    diag.mode = "priority";
    diag.nowUs = static_cast<std::uint64_t>(esp_timer_get_time());
    diag.startedUs = diag.nowUs;
    if (!destination || !capacity) {
        logRawSelectorDiag(diag);
        return 0;
    }
    PrioritySelector::Candidate candidates[PendingCapacity]{};
    const unsigned count = pending.size();
    diag.currentBootRecords = count;
    diag.eligible = count;
    for (unsigned i = 0; i < count; ++i) {
        const Raw& raw = pending.at(i);
        candidates[i].bootId = boot;
        candidates[i].windowIndex = raw.index;
        candidates[i].role = raw.transmissionReason == AdaptiveTransmission::Reason::RecoveryHold
            ? PrioritySelector::Role::Recovery
            : (raw.transmissionMode == AdaptiveTransmission::Mode::Priority
                ? PrioritySelector::Role::Trigger : PrioritySelector::Role::Predecessor);
    }
    PrioritySelector::Candidate selected[BatchCapacity]{};
    const unsigned selectedCount = PrioritySelector::select(candidates, count, selected, capacity);
    unsigned loaded = 0;
    for (unsigned i = 0; i < selectedCount; ++i)
        for (unsigned j = 0; j < count; ++j)
            if (selected[i].windowIndex == pending.at(j).index) {
                destination[loaded++] = pending.at(j);
                break;
            }
    diag.selected = loaded;
    logRawSelectorDiag(diag);
    return loaded;
}
#endif
// Static network-task-owned buffers: do not place raw batches on its stack.
PendingWindow* batch = nullptr;
#if RAW_VIBRATION_ENABLED
// SPSC fallback for a full pending queue. Only processingTask writes here;
// captureTask records a bounded rawQueue drop instead of becoming a second
// producer. The processing task merges both sources by window index.
Raw* rawHold = nullptr;
std::atomic<std::uint32_t> rawHoldWrite{0};
std::atomic<std::uint32_t> rawHoldRead{0};
unsigned char* rawBytes = nullptr;
unsigned char* encodedRaw = nullptr;
unsigned char* rawHoldAllocation = nullptr;
unsigned char* rawBytesAllocation = nullptr;
unsigned char* encodedRawAllocation = nullptr;
constexpr size_t DiagnosticCanaryBytes = 16;
constexpr unsigned char DiagnosticCanaryHead = 0xA5;
constexpr unsigned char DiagnosticCanaryTail = 0x5A;
extern char boot[33];

void* allocateGuardedRawBuffer(size_t payloadBytes) {
    const size_t allocationBytes = payloadBytes + DiagnosticCanaryBytes * 2;
    void* allocation = heap_caps_malloc(allocationBytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!allocation) allocation = heap_caps_malloc(allocationBytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (!allocation) return nullptr;
    auto* bytes = static_cast<unsigned char*>(allocation);
    std::memset(bytes, DiagnosticCanaryHead, DiagnosticCanaryBytes);
    std::memset(bytes + DiagnosticCanaryBytes + payloadBytes, DiagnosticCanaryTail, DiagnosticCanaryBytes);
    return bytes;
}

bool guardedRawBufferIntact(const unsigned char* allocation, size_t payloadBytes) {
    if (!allocation) return false;
    for (size_t i = 0; i < DiagnosticCanaryBytes; ++i)
        if (allocation[i] != DiagnosticCanaryHead ||
            allocation[DiagnosticCanaryBytes + payloadBytes + i] != DiagnosticCanaryTail)
            return false;
    return true;
}

void logRawBufferCanaries(const char* phase) {
    const bool rawHoldOk = guardedRawBufferIntact(rawHoldAllocation, sizeof(Raw) * RawHoldCapacity);
    const bool rawBytesOk = guardedRawBufferIntact(rawBytesAllocation, RawBytesCapacity);
    const bool encodedRawOk = guardedRawBufferIntact(encodedRawAllocation, EncodedRawCapacity);
    const bool audioRingOk = checkAudioRingCanary();
    Serial.printf("[BUFFER-CANARY] phase=%s rawHold=%s rawBytes=%s encodedRaw=%s audioRing=%s\n",
                  phase ? phase : "unknown", rawHoldOk ? "ok" : "CORRUPT",
                  rawBytesOk ? "ok" : "CORRUPT", encodedRawOk ? "ok" : "CORRUPT",
                  audioRingOk ? "ok" : "CORRUPT");
}

bool holdRaw(const Raw& raw) {
    if (!rawHold) return false;
    const auto write = rawHoldWrite.load(std::memory_order_relaxed);
    const auto read = rawHoldRead.load(std::memory_order_acquire);
    if (write - read >= RawHoldCapacity) return false;
    rawHold[write % RawHoldCapacity] = raw;
    rawHoldWrite.store(write + 1, std::memory_order_release);
    return true;
}

bool takeHeldRaw(Raw& raw) {
    const auto read = rawHoldRead.load(std::memory_order_relaxed);
    if (read == rawHoldWrite.load(std::memory_order_acquire)) return false;
    raw = rawHold[read % RawHoldCapacity];
    rawHoldRead.store(read + 1, std::memory_order_release);
    return true;
}

bool encodeRaw(const Raw& raw, JsonObject w) {
    if (!rawBytes || !encodedRaw || !serializeCountsLE(raw,rawBytes,RawBytesCapacity)) return false;
    size_t length=0;
    if (mbedtls_base64_encode(encodedRaw,EncodedRawCapacity,&length,rawBytes,raw.count*6)) return false;
    encodedRaw[length]=0;
    // Own each string; subsequent windows reuse encodedRaw.
    w["samples"] = String(reinterpret_cast<const char*>(encodedRaw));
    return true;
}

String rawSpoolPath(unsigned slot) {
    return String("/raw-spool-v2-") + slot + ".bin";
}
String legacyRawSpoolPath(unsigned slot) {
    return String("/raw-spool-") + slot + ".bin";
}

// VFS open/exists logs every expected empty slot on this device. POSIX stat
// gives the same read-only check without turning normal scans into errors.
bool rawSpoolPresent(const String& path) {
    struct stat info {};
    const String vfsPath = String("/littlefs") + path;
    return ::stat(vfsPath.c_str(), &info) == 0;
}

void initializeRawSpoolAdmission() {
    size_t total = 0;
    size_t used = 0;
    const esp_err_t rc = esp_littlefs_info("spiffs", &total, &used);
    const size_t freeBytes = rc == ESP_OK && total >= used ? total - used : 0;
    const bool suspend = rc != ESP_OK || freeBytes <= ObservedUnsafeRawSpoolFreeBytes;
    rawSpoolWritesSuspended.store(suspend, std::memory_order_release);
    Serial.printf("[RAW-SPOOL-ADMISSION] rc=%s total=%u used=%u free=%u suspended=%s reason=%s\n",
                  esp_err_to_name(rc), static_cast<unsigned>(total),
                  static_cast<unsigned>(used), static_cast<unsigned>(freeBytes),
                  suspend ? "yes" : "no",
                  rc != ESP_OK ? "info_failed" :
                  (suspend ? "observed_unsafe_state" : "none"));
}

void initializeRawSpoolCursor() {
    const auto startedUs = static_cast<std::uint64_t>(esp_timer_get_time());
    unsigned next = RawSpoolSlots;
    unsigned scanned = 0;
    {
        LittleFsLock fsLock;
        for (unsigned i = 0; i < RawSpoolSlots; ++i) {
            ++scanned;
            if (!rawSpoolPresent(rawSpoolPath(i))) {
                next = i;
                break;
            }
        }
    }
    rawSpoolNextSlot.store(next == RawSpoolSlots ? 0U : next,
                           std::memory_order_relaxed);
    Serial.printf("[RAW-SPOOL-BOOT] scanned=%u next=%u full=%s elapsed_us=%llu\n",
                  scanned, next, next == RawSpoolSlots ? "yes" : "no",
                  static_cast<unsigned long long>(
                      static_cast<std::uint64_t>(esp_timer_get_time()) - startedUs));
}

bool rawSpoolHasPending() {
    LittleFsLock fsLock;
    unsigned legacy = 0;
    bool current = false;
    for (unsigned i = 0; i < RawSpoolSlots; ++i)
        if (rawSpoolPresent(rawSpoolPath(i))) current = true;
        else if (rawSpoolPresent(legacyRawSpoolPath(i))) ++legacy;
    if (legacy) Serial.printf("[RAW-SPOOL] legacy spool pending=%u; unreadable for current boot and held (header has no bootId)\n", legacy);
    return current || legacy != 0;
}

bool rawSpoolHeaderUnlocked(unsigned slot, RawSpoolHeader& header) {
    File file = LittleFS.open(rawSpoolPath(slot), "r");
    if (!file || file.size() != RawSpoolSlotBytes ||
        file.read(reinterpret_cast<std::uint8_t*>(&header), sizeof(header)) != sizeof(header)) {
        if (file) file.close();
        return false;
    }
    file.close();
    return header.magic == RawSpoolMagic && header.version == RawSpoolVersion &&
        header.count > 0 && header.count <= Samples &&
        header.quality <= static_cast<std::uint8_t>(Quality::ProcessingOverflow) &&
        header.reserved[0] <= static_cast<std::uint8_t>(AdaptiveTransmission::Mode::Priority) &&
        header.reserved[1] <= static_cast<std::uint8_t>(AdaptiveTransmission::Reason::RecoveryHold) &&
        header.reserved[2] <= RawSpoolQuarantined;
}

bool readRawSpoolUnlocked(unsigned slot, Raw& raw) {
    auto* bytes = static_cast<std::uint8_t*>(heap_caps_malloc(RawBytesCapacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!bytes) bytes = static_cast<std::uint8_t*>(heap_caps_malloc(RawBytesCapacity, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (!bytes) return false;
    std::memset(bytes, 0, RawBytesCapacity);
    RawSpoolHeader header;
    {
        File file = LittleFS.open(rawSpoolPath(slot), "r");
        if (!file || file.size() != RawSpoolSlotBytes ||
            file.read(reinterpret_cast<std::uint8_t*>(&header), sizeof(header)) != sizeof(header) ||
            file.read(bytes, RawBytesCapacity) != RawBytesCapacity) {
            if (file) file.close();
            heap_caps_free(bytes);
            return false;
        }
        file.close();
    }
    if (header.magic != RawSpoolMagic || header.version != RawSpoolVersion ||
        header.count == 0 || header.count > Samples ||
        header.quality > static_cast<std::uint8_t>(Quality::ProcessingOverflow) ||
        header.reserved[0] > static_cast<std::uint8_t>(AdaptiveTransmission::Mode::Priority) ||
        header.reserved[1] > static_cast<std::uint8_t>(AdaptiveTransmission::Reason::RecoveryHold) ||
        header.reserved[2] > RawSpoolQuarantined ||
        header.crc != EdgeAnalysis::checksum(reinterpret_cast<const char*>(bytes), RawBytesCapacity)) {
        heap_caps_free(bytes);
        return false;
    }
    raw = Raw{};
    raw.index = header.index;
    raw.startUs = header.startUs;
    raw.count = header.count;
    raw.quality = static_cast<Quality>(header.quality);
    raw.transmissionMode = static_cast<AdaptiveTransmission::Mode>(header.reserved[0]);
    raw.transmissionReason = static_cast<AdaptiveTransmission::Reason>(header.reserved[1]);
    const bool valid = deserializeCountsLE(raw, bytes, RawBytesCapacity);
    heap_caps_free(bytes);
    return valid;
}

bool readRawSpool(unsigned slot, Raw& raw) {
    LittleFsLock fsLock;
    const bool readable = readRawSpoolUnlocked(slot, raw);
    if (!readable) ++dropMetrics.spool_corrupt_or_unreadable;
    return readable;
}

bool sameRaw(const Raw& left, const Raw& right) {
    return left.index == right.index && left.startUs == right.startUs &&
        left.count == right.count && left.quality == right.quality &&
        left.transmissionMode == right.transmissionMode &&
        left.transmissionReason == right.transmissionReason &&
        std::memcmp(left.xyz, right.xyz, sizeof(left.xyz)) == 0;
}

bool writeRawSpool(const Raw& raw, unsigned* slotOut = nullptr) {
    if (rawSpoolWritesSuspended.load(std::memory_order_acquire)) {
        ++dropMetrics.spool_capacity_full;
        static std::uint32_t lastSuspendedLogMs = 0;
        static bool suspendedLogInitialized = false;
        const auto nowMs = millis();
        if (!suspendedLogInitialized || nowMs - lastSuspendedLogMs >= 1000U) {
            Serial.printf("[RAW-SPOOL-FULL] writes_suspended=1 windowIndex=%lu retained=1\n",
                          static_cast<unsigned long>(raw.index));
            lastSuspendedLogMs = nowMs;
            suspendedLogInitialized = true;
        }
        return false;
    }
    const auto storageStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
    StorageTiming timing;
    auto* bytes = static_cast<std::uint8_t*>(heap_caps_malloc(RawBytesCapacity, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
    if (!bytes) bytes = static_cast<std::uint8_t*>(heap_caps_malloc(RawBytesCapacity, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
    if (!bytes) {
        ++dropMetrics.spool_write_fail;
        timing.totalUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - storageStartedUs);
        recordStorageTiming(timing);
        return false;
    }
    std::memset(bytes, 0, RawBytesCapacity);
    if (!serializeCountsLE(raw, bytes, RawBytesCapacity)) {
        heap_caps_free(bytes);
        ++dropMetrics.spool_write_fail;
        timing.totalUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - storageStartedUs);
        recordStorageTiming(timing);
        return false;
    }
    unsigned slot = RawSpoolSlots;
    {
        const auto lockStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        LittleFsLock fsLock;
        timing.lockWaitUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - lockStartedUs);
        const auto slotLookupStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        const unsigned nextSlot = rawSpoolNextSlot.load(std::memory_order_relaxed) % RawSpoolSlots;
        if (!rawSpoolPresent(rawSpoolPath(nextSlot))) slot = nextSlot;
        timing.slotLookupUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - slotLookupStartedUs);
        const size_t totalBytes = LittleFS.totalBytes();
        const size_t usedBytes = LittleFS.usedBytes();
        const size_t freeBytes = totalBytes > usedBytes ? totalBytes - usedBytes : 0;
        if (slot == RawSpoolSlots || freeBytes < RawSpoolSlotBytes) {
            ++dropMetrics.spool_capacity_full;
            timing.totalUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - storageStartedUs);
            recordStorageTiming(timing);
            Serial.printf("[RAW-SPOOL-FULL] slot=%u total=%u used=%u free=%u need=%u windowIndex=%lu.\n",
                          slot, static_cast<unsigned>(totalBytes), static_cast<unsigned>(usedBytes),
                          static_cast<unsigned>(freeBytes), static_cast<unsigned>(RawSpoolSlotBytes),
                          static_cast<unsigned long>(raw.index));
            heap_caps_free(bytes);
            return false;
        }
        RawSpoolHeader header;
        header.index = raw.index;
        header.startUs = raw.startUs;
        header.count = raw.count;
        header.quality = static_cast<std::uint8_t>(raw.quality);
        header.reserved[0] = static_cast<std::uint8_t>(raw.transmissionMode);
        header.reserved[1] = static_cast<std::uint8_t>(raw.transmissionReason);
        std::strncpy(header.bootId, boot, sizeof(header.bootId) - 1);
        header.crc = EdgeAnalysis::checksum(reinterpret_cast<const char*>(bytes), RawBytesCapacity);
        const String temp = rawSpoolPath(slot) + ".tmp";
        const auto openStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        File file = LittleFS.open(temp, "w");
        timing.openUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - openStartedUs);
        const auto writeStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        const bool opened = static_cast<bool>(file);
        const bool headerWritten = opened &&
            file.write(reinterpret_cast<const std::uint8_t*>(&header), sizeof(header)) == sizeof(header);
        const bool payloadWritten = headerWritten &&
            file.write(bytes, RawBytesCapacity) == RawBytesCapacity;
        const bool written = opened && headerWritten && payloadWritten;
        timing.writeUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - writeStartedUs);
        const auto flushCloseStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        if (file) { file.flush(); file.close(); }
        timing.flushCloseUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - flushCloseStartedUs);
        const auto renameStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        const bool renamed = written && LittleFS.rename(temp, rawSpoolPath(slot));
        timing.renameUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - renameStartedUs);
        if (!renamed) {
            ++dropMetrics.spool_write_fail;
            timing.totalUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - storageStartedUs);
            recordStorageTiming(timing);
            if (rawSpoolPresent(temp)) LittleFS.remove(temp);
            Serial.printf("[SPOOL-WRITE-FAIL] stage=%s slot=%u windowIndex=%lu retained=1\n",
                          written ? "rename" : "write", slot,
                          static_cast<unsigned long>(raw.index));
            heap_caps_free(bytes);
            return false;
        }
        if (slotOut) *slotOut = slot;
        rawSpoolNextSlot.store((slot + 1U) % RawSpoolSlots, std::memory_order_relaxed);
    }
    heap_caps_free(bytes);
    timing.totalUs = static_cast<std::uint32_t>(static_cast<std::uint64_t>(esp_timer_get_time()) - storageStartedUs);
    recordStorageTiming(timing);
    // The atomic rename plus CRC is the durable write boundary. The network
    // selector performs the full read/CRC validation before transmission.
    Serial.printf("[RAW-SPOOL] stored slot=%u windowIndex=%lu.\n", slot,
                  static_cast<unsigned long>(raw.index));
    return true;
}

unsigned loadRawSpoolBatch(Raw* destination, unsigned* slots, unsigned capacity,
                            std::uint64_t cutoffUs = UINT64_MAX,
                            char (*bootIds)[33] = nullptr, bool priorityOnly = false) {
    RawSelectorDiag diag;
    diag.event = ++rawSelectorEvent;
    diag.mode = priorityOnly ? "priority" : "periodic";
    diag.nowUs = static_cast<std::uint64_t>(esp_timer_get_time());
    diag.cutoffUs = cutoffUs;
    diag.startedUs = diag.nowUs;
    if (!destination || !slots || !capacity) {
        logRawSelectorDiag(diag);
        return 0;
    }
    bool used[RawSpoolSlots] = {};
    unsigned loaded = 0;
    while (loaded < capacity) {
        unsigned selected = RawSpoolSlots;
        std::uint32_t selectedIndex = UINT32_MAX;
        RawSpoolHeader selectedHeader;
        for (unsigned i = 0; i < RawSpoolSlots; ++i) {
            if (used[i]) continue;
            ++diag.slotsChecked;
            const auto lockStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
            LittleFsLock fsLock;
            diag.lockWaitUs += static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(esp_timer_get_time()) - lockStartedUs);
            const auto statStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
            const bool present = rawSpoolPresent(rawSpoolPath(i));
            const auto statUs = static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(esp_timer_get_time()) - statStartedUs);
            diag.statTotalUs += statUs;
            diag.statMaxUs = std::max(diag.statMaxUs, statUs);
            if (!present) continue;
            ++diag.headersPresent;
            const auto headerStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
            RawSpoolHeader header;
            const bool headerReadable = rawSpoolHeaderUnlocked(i, header);
            const auto headerUs = static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(esp_timer_get_time()) - headerStartedUs);
            diag.headerTotalUs += headerUs;
            diag.headerMaxUs = std::max(diag.headerMaxUs, headerUs);
            if (!headerReadable) {
                ++diag.headerReadFail;
                ++dropMetrics.spool_corrupt_or_unreadable;
                Serial.printf("[SPOOL-READ-FAIL] stage=header slot=%u retained=1\n", i);
                logRawSelectorDiag(diag);
                return 0;
            }
            const bool currentBoot = std::strncmp(header.bootId, boot,
                                                   sizeof(header.bootId)) == 0;
            if (currentBoot) ++diag.currentBootRecords;
            else {
                ++diag.restoredBootRecords;
                if (!diag.firstRestoredStartUs) diag.firstRestoredStartUs = header.startUs;
            }
            if (header.reserved[2] == RawSpoolQuarantined) continue;
            if (header.startUs > cutoffUs) {
                ++diag.cutoffRejected;
                continue;
            }
            const bool isPriority = header.reserved[0] ==
                static_cast<std::uint8_t>(AdaptiveTransmission::Mode::Priority);
            if (priorityOnly != isPriority) continue;
            ++diag.eligible;
            if (selected == RawSpoolSlots || header.index < selectedIndex) {
                selected = i;
                selectedIndex = header.index;
                selectedHeader = header;
            }
        }
        if (selected == RawSpoolSlots) break;
        {
            const auto lockStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
            LittleFsLock fsLock;
            diag.lockWaitUs += static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(esp_timer_get_time()) - lockStartedUs);
            const auto payloadStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
            const bool payloadReadable = readRawSpoolUnlocked(selected, destination[loaded]);
            const auto payloadUs = static_cast<std::uint32_t>(
                static_cast<std::uint64_t>(esp_timer_get_time()) - payloadStartedUs);
            diag.payloadTotalUs += payloadUs;
            diag.payloadMaxUs = std::max(diag.payloadMaxUs, payloadUs);
            if (!payloadReadable) {
                ++dropMetrics.spool_corrupt_or_unreadable;
                Serial.printf("[SPOOL-READ-FAIL] stage=payload slot=%u retained=1\n", selected);
                logRawSelectorDiag(diag);
                return 0;
            }
        }
        slots[loaded++] = selected;
        if (bootIds) std::strncpy(bootIds[loaded - 1], selectedHeader.bootId, 32);
        if (bootIds) bootIds[loaded - 1][32] = '\0';
        used[selected] = true;
    }
    diag.selected = loaded;
    logRawSelectorDiag(diag);
    return loaded;
}

bool oldestRawSpoolIndex(std::uint32_t& index, std::uint64_t cutoffUs = UINT64_MAX) {
    // Hold one filesystem transaction across the complete slot scan.  Taking
    // the lock per header left other VFS work able to interleave between slots.
    LittleFsLock fsLock;
    bool found = false;
    for (unsigned i = 0; i < RawSpoolSlots; ++i) {
        if (!rawSpoolPresent(rawSpoolPath(i))) continue;
        RawSpoolHeader header;
        if (!rawSpoolHeaderUnlocked(i, header)) {
            ++dropMetrics.spool_corrupt_or_unreadable;
            continue;
        }
        if (
            header.reserved[2] != RawSpoolQuarantined &&
            header.startUs <= cutoffUs &&
            (!found || header.index < index)) {
            index = header.index;
            found = true;
        }
    }
    return found;
}

// Fairness guard: forced auxiliary turns may run only when no persisted or
// pending priority window is waiting. Periodic backlog remains eligible for
// the bounded auxiliary turn after this check.
bool priorityRawPending() {
    xSemaphoreTake(mutex, portMAX_DELAY);
    for (unsigned i = 0; i < pending.size(); ++i) {
        if (pending.at(i).transmissionMode == AdaptiveTransmission::Mode::Priority) {
            xSemaphoreGive(mutex);
            return true;
        }
    }
    xSemaphoreGive(mutex);
    for (unsigned i = 0; i < RawSpoolSlots; ++i) {
        {
            LittleFsLock fsLock;
            RawSpoolHeader header;
            if (!rawSpoolPresent(rawSpoolPath(i))) continue;
            if (!rawSpoolHeaderUnlocked(i, header)) {
                ++dropMetrics.spool_corrupt_or_unreadable;
                continue;
            }
            if (header.reserved[2] != RawSpoolQuarantined &&
                header.reserved[0] == static_cast<std::uint8_t>(AdaptiveTransmission::Mode::Priority))
                return true;
        }
    }
    return false;
}

bool quarantineRawSpoolBatch(const unsigned* slots, unsigned count) {
    if (!slots || !count) return false;
    LittleFsLock fsLock;
    bool changed = true;
    for (unsigned i = 0; i < count; ++i) {
        if (slots[i] >= RawSpoolSlots) { changed = false; continue; }
        File file = LittleFS.open(rawSpoolPath(slots[i]), "r+");
        if (!file || !file.seek(offsetof(RawSpoolHeader, reserved) + 2)) {
            if (file) file.close();
            changed = false;
            continue;
        }
        const std::uint8_t state = RawSpoolQuarantined;
        if (file.write(&state, sizeof(state)) != sizeof(state)) {
            Serial.printf("[SPOOL-WRITE-FAIL] stage=quarantine slot=%u retained=1\n", slots[i]);
            changed = false;
        }
        file.flush();
        file.close();
    }
    return changed;
}

void rawSpoolStateCounts(unsigned& ready, unsigned& quarantined) {
    ready = 0;
    quarantined = 0;
    LittleFsLock fsLock;
    for (unsigned i = 0; i < RawSpoolSlots; ++i) {
        if (!rawSpoolPresent(rawSpoolPath(i))) continue;
        RawSpoolHeader header;
        if (!rawSpoolHeaderUnlocked(i, header)) {
            ++dropMetrics.spool_corrupt_or_unreadable;
            continue;
        }
        if (header.reserved[2] == RawSpoolQuarantined) ++quarantined;
        else ++ready;
    }
}

bool removeRawSpoolBatch(const unsigned* slots, unsigned count) {
    LittleFsLock fsLock;
    bool removed = true;
    for (unsigned i = 0; i < count; ++i)
        if (rawSpoolPresent(rawSpoolPath(slots[i])) && !LittleFS.remove(rawSpoolPath(slots[i]))) {
            Serial.printf("[SPOOL-WRITE-FAIL] stage=delete slot=%u retained=1\n", slots[i]);
            removed = false;
        }
    return removed;
}

bool strictRawAck(int status, const String& response, const Raw* windows, unsigned count,
                  const char (*bootIds)[33], std::size_t* matchedCount = nullptr) {
    if (!windows || !count) return false;
    RawAck::Expected expected[BatchCapacity] = {};
    if (count > BatchCapacity) return false;
    for (unsigned i = 0; i < count; ++i) {
        expected[i].bootId = bootIds[i];
        expected[i].windowIndex = windows[i].index;
    }
    return RawAck::matches(status, response.c_str(), DEVICE_ID, expected, count, matchedCount);
}
#endif
#if !RAW_VIBRATION_ENABLED
inline bool priorityRawPending() { return false; }
#endif
Workspace* workspace = nullptr;
Raw capturing;
QueueHandle_t rawQueue = nullptr;
SemaphoreHandle_t mutex = nullptr;
Features latest;
AcousticFeatures latestAudio;
bool latestAudioValid = false;
std::uint32_t latestAudioGeneration = 0;
bool hasLatest = false;
std::atomic<bool> sensorReady{false};
std::atomic<std::uint32_t> processingDrops{0};
std::atomic<std::uint32_t> audioDrops{0};
std::atomic<bool> rawNetworkBusy{false};
std::atomic<bool> healthNetworkRequested{false};
std::atomic<bool> telemetryReplayRequested{false};
// 0=free, 1=raw reserved/in flight, 2=health reserved, 3=telemetry reserved.
// A single atomic
// reservation closes the check-then-acquire race without splitting the gate.
std::atomic<std::uint8_t> networkReservation{0};
char boot[33];
CaptureHealth captureHealth;

void updateQueueHighWater(std::atomic<std::uint32_t>& highWater, unsigned depth) {
    auto observed = highWater.load(std::memory_order_relaxed);
    while (observed < depth &&
           !highWater.compare_exchange_weak(observed, depth,
                                             std::memory_order_relaxed,
                                             std::memory_order_relaxed)) {}
}

void maybeLogDropSummary() {
    static std::uint32_t lastSampleMs = 0;
    static std::uint32_t lastSummaryMs = 0;
    static unsigned lastPendingDepth = 0;
    static bool initialized = false;
    const auto nowMs = millis();
    if (initialized && nowMs - lastSampleMs < 100U) return;
    initialized = true;
    lastSampleMs = nowMs;

    const unsigned rawQueueDepth = rawQueue ?
        static_cast<unsigned>(uxQueueMessagesWaiting(rawQueue)) : 0U;
    if (mutex && xSemaphoreTake(mutex, 0) == pdTRUE) {
        lastPendingDepth = pending.size();
        xSemaphoreGive(mutex);
    }
    const unsigned pendingDepth = lastPendingDepth;
#if RAW_VIBRATION_ENABLED
    const auto rawHoldWriteSnapshot = rawHoldWrite.load(std::memory_order_acquire);
    const auto rawHoldReadSnapshot = rawHoldRead.load(std::memory_order_acquire);
    const unsigned rawHoldDepth = static_cast<unsigned>(rawHoldWriteSnapshot - rawHoldReadSnapshot);
#else
    const unsigned rawHoldDepth = 0;
#endif
    updateQueueHighWater(dropMetrics.raw_queue_high_water, rawQueueDepth);
    updateQueueHighWater(dropMetrics.pending_high_water, pendingDepth);
    updateQueueHighWater(dropMetrics.raw_hold_high_water, rawHoldDepth);
    if (lastSummaryMs != 0 && nowMs - lastSummaryMs < 1000U) return;
    lastSummaryMs = nowMs;
    Serial.printf(
        "[DROP-SUMMARY] capture_queue_full=%llu processing_pending_full_events=%llu processing_pending_full_unique=%llu actual_drop_total=%llu raw_hold_full=%llu spool_write_fail=%llu spool_capacity_full=%llu spool_corrupt_or_unreadable=%llu raw_queue_depth=%u raw_queue_high_water=%u pending_depth=%u pending_high_water=%u raw_hold_depth=%u raw_hold_high_water=%u storage_samples=%lu storage_lock_wait_us=%lu storage_slot_lookup_us=%lu storage_open_us=%lu storage_write_us=%lu storage_flush_close_us=%lu storage_rename_us=%lu storage_total_us=%lu storage_lock_wait_max_us=%lu storage_slot_lookup_max_us=%lu storage_open_max_us=%lu storage_write_max_us=%lu storage_flush_close_max_us=%lu storage_rename_max_us=%lu storage_total_max_us=%lu storage_total_le_640ms=%llu storage_total_641_to_1280ms=%llu storage_total_gt_1280ms=%llu\n",
        static_cast<unsigned long long>(dropMetrics.capture_queue_full.load()),
        static_cast<unsigned long long>(dropMetrics.processing_pending_full.load()),
        static_cast<unsigned long long>(dropMetrics.processing_pending_full_unique.load()),
        static_cast<unsigned long long>(dropMetrics.actual_drop_total.load()),
        static_cast<unsigned long long>(dropMetrics.raw_hold_full.load()),
        static_cast<unsigned long long>(dropMetrics.spool_write_fail.load()),
        static_cast<unsigned long long>(dropMetrics.spool_capacity_full.load()),
        static_cast<unsigned long long>(dropMetrics.spool_corrupt_or_unreadable.load()),
        rawQueueDepth,
        static_cast<unsigned>(dropMetrics.raw_queue_high_water.load()),
        pendingDepth,
        static_cast<unsigned>(dropMetrics.pending_high_water.load()),
        rawHoldDepth,
        static_cast<unsigned>(dropMetrics.raw_hold_high_water.load()),
        static_cast<unsigned long>(dropMetrics.storage_samples.load()),
        static_cast<unsigned long>(dropMetrics.storage_lock_wait_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_slot_lookup_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_open_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_write_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_flush_close_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_rename_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_total_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_lock_wait_max_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_slot_lookup_max_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_open_max_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_write_max_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_flush_close_max_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_rename_max_us.load()),
        static_cast<unsigned long>(dropMetrics.storage_total_max_us.load()),
        static_cast<unsigned long long>(dropMetrics.storage_total_le_640ms.load()),
        static_cast<unsigned long long>(dropMetrics.storage_total_641_to_1280ms.load()),
        static_cast<unsigned long long>(dropMetrics.storage_total_gt_1280ms.load()));
}

bool tryReserveRawNetwork() {
    if (healthNetworkRequested.load(std::memory_order_acquire) ||
        telemetryReplayRequested.load(std::memory_order_acquire)) return false;
    std::uint8_t free = 0;
    return networkReservation.compare_exchange_strong(free, 1,
        std::memory_order_acq_rel, std::memory_order_acquire);
}

bool rawQueueIsEmpty() {
    xSemaphoreTake(mutex, portMAX_DELAY);
    const bool pendingEmpty = pending.size() == 0;
    xSemaphoreGive(mutex);
    const bool rawQueueEmpty = !rawQueue || uxQueueMessagesWaiting(rawQueue) == 0;
#if RAW_VIBRATION_ENABLED
    const bool rawHoldEmpty = rawHoldRead.load(std::memory_order_acquire) ==
                              rawHoldWrite.load(std::memory_order_acquire);
#else
    const bool rawHoldEmpty = true;
#endif
    return pendingEmpty && rawQueueEmpty && rawHoldEmpty;
}

void releaseRawNetwork() {
    networkReservation.store(0, std::memory_order_release);
}

bool tryReserveHealthNetwork() {
    std::uint8_t free = 0;
    return networkReservation.compare_exchange_strong(free, 2,
        std::memory_order_acq_rel, std::memory_order_acquire);
}

bool tryReserveTelemetryNetwork() {
    if (healthNetworkRequested.load(std::memory_order_acquire)) return false;
    std::uint8_t free = 0;
    return networkReservation.compare_exchange_strong(free, 3,
        std::memory_order_acq_rel, std::memory_order_acquire);
}

void releaseTelemetryNetwork() {
    std::uint8_t expected = 3;
    networkReservation.compare_exchange_strong(expected, 0,
        std::memory_order_acq_rel, std::memory_order_acquire);
}

void releaseHealthNetwork() {
    std::uint8_t expected = 2;
    networkReservation.compare_exchange_strong(expected, 0,
        std::memory_order_acq_rel, std::memory_order_acquire);
}

void reportCaptureHealth(CaptureHealth::State state) {
    captureHealth.record(state, healthUptimeMs(),
        [](DeviceHealth::Fault fault, bool active, std::uint64_t observedMs) {
            const SensorObservation observation{fault, active, observedMs};
            // A health transition must never pause the sensor reader.
            return xQueueSend(sensorObservations, &observation, 0) == pdTRUE;
        });
}

void resetFifo() {
    adxlWrite(0x38, 0); // Bypass clears stale FIFO; then continuous stream.
    adxlWrite(0x38, 0x80);
}
void finish(Quality quality) {
    capturing.quality = quality;
    if (quality == Quality::Valid) reportCaptureHealth(CaptureHealth::State::Healthy);
    if (capturing.audioWindow) {
        heap_caps_free(capturing.audioWindow);
        capturing.audioWindow = nullptr;
    }
    // Copy the exact synchronized window once, while capture still owns the
    // Raw. The bounded wait only covers the audio samples trailing vibration.
    if (capturing.count > 0 && audioReady.load() &&
        capturing.audioGeneration == audioErrorGeneration.load()) {
        auto* window = static_cast<std::int32_t*>(heap_caps_malloc(
            sizeof(std::int32_t) * COMMON_AUDIO_SAMPLES,
            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
        if (!window) window = static_cast<std::int32_t*>(heap_caps_malloc(
            sizeof(std::int32_t) * COMMON_AUDIO_SAMPLES,
            MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
        const auto readyAt = millis();
        while (window && getAudioTotalSamples() < capturing.audioStart + COMMON_AUDIO_SAMPLES &&
               millis() - readyAt < 20) vTaskDelay(1);
        if (window && copyAudioWindow(capturing.audioStart, COMMON_AUDIO_SAMPLES, window))
            capturing.audioWindow = window;
        else if (window) heap_caps_free(window);
    }
    // Never block the sensor reader. rawHold is owned by processingTask only;
    // a full rawQueue is recorded as a bounded drop rather than a second
    // producer racing the hold queue.
    const bool queued = xQueueSend(rawQueue, &capturing, 0) == pdTRUE;
    if (queued) {
        // The queue copy now owns the captured audio buffer.
        capturing.audioWindow = nullptr;
    } else {
        if (capturing.audioWindow) {
            heap_caps_free(capturing.audioWindow);
            capturing.audioWindow = nullptr;
        }
        ++dropMetrics.capture_queue_full;
        ++dropMetrics.actual_drop_total;
        ++processingDrops;
    }
    ++capturing.index; // Includes invalid/dropped windows, never renumber.
    capturing.count = 0;
    capturing.quality = Quality::Valid;
}
void captureTask(void*) {
    Serial.printf("[STACK] VibrationFIFO free=%u bytes\n",
                  static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr) * sizeof(StackType_t)));
    std::uint64_t lastData = esp_timer_get_time();
    while (true) {
        maybeLogDropSummary();
        if (!sensorReady.load()) {
            if (!initADXL345()) {
                reportCaptureHealth(CaptureHealth::State::InitFailed);
                capturing.startUs = esp_timer_get_time();
                finish(Quality::SensorUnavailable);
                vTaskDelay(pdMS_TO_TICKS(640));
                continue;
            }
            resetFifo();
            sensorReady.store(true);
            lastData = esp_timer_get_time();
        }
        const auto now = static_cast<std::uint64_t>(esp_timer_get_time());
        const auto readStarted = static_cast<std::uint64_t>(esp_timer_get_time());
        const unsigned entries = adxlRead(0x39) & 0x3f;
        const unsigned devid = adxlRead(REG_DEVID);
        const auto readDuration = static_cast<std::uint64_t>(esp_timer_get_time()) - readStarted;
        // ADXL345 FIFO_ENTRIES is a 6-bit count with 32 as the valid full state;
        // full means imminent loss if servicing remains late, not loss by itself.
        const bool disconnected = devid != 0xE5 || entries > 32;
        if (entries > 32 || devid != 0xE5) {
            Serial.printf("[FIFO] fault=%s rawEntries=%u devid=0x%02X readUs=%llu captureGapUs=%llu rawQueue=%u windowIndex=%lu count=%u startUs=%llu\n",
                          devid != 0xE5 ? "sensor_unavailable" : "fifo_invalid",
                          entries,
                          devid,
                          static_cast<unsigned long long>(readDuration),
                          static_cast<unsigned long long>(now - lastData),
                          rawQueue ? static_cast<unsigned>(uxQueueMessagesWaiting(rawQueue)) : 0,
                          static_cast<unsigned long>(capturing.index),
                          capturing.count,
                          static_cast<unsigned long long>(capturing.startUs));
            reportCaptureHealth(disconnected ? CaptureHealth::State::ChannelFailed :
                                              CaptureHealth::State::TimedOut);
            if (!capturing.count) capturing.startUs = now;
            finish(disconnected ? Quality::SensorUnavailable : Quality::FifoOverrun);
            sensorReady.store(false);
            continue;
        }
        if (!entries) {
            if (now - lastData > 100000) {
                reportCaptureHealth(CaptureHealth::State::TimedOut);
                if (!capturing.count) capturing.startUs = now;
                finish(Quality::SampleGap);
                sensorReady.store(false);
            }
            vTaskDelay(1);
            continue;
        }
        lastData = now;
        for (unsigned n=0; n<entries; ++n) {
            if (!capturing.count) {
                // Estimate first sample instant from FIFO depth, not POST time.
                capturing.startUs = now - (entries-n-1)*1250ULL;
                const auto audioNow = getAudioTotalSamples();
                const auto audioLag = (entries-n-1)*20ULL;
                capturing.audioStart = audioNow > audioLag ? audioNow-audioLag : 0;
                capturing.audioGeneration = audioErrorGeneration.load();
            }
            auto& xyz = capturing.xyz[capturing.count];
            adxlReadXYZ(xyz[0], xyz[1], xyz[2]);
            // ADXL345 datasheet: minimum 5 us before the next FIFO access.
            delayMicroseconds(5);
            ++capturing.count;
            if (capturing.count == Samples) finish(Quality::Valid);
        }
        // The 5 us inter-read delay above is required by the ADXL345; do not
        // add another delay after a successful FIFO drain.
    }
}
void processingTask(void*) {
    Serial.printf("[STACK] WindowFeatures free=%u bytes\n",
                  static_cast<unsigned>(uxTaskGetStackHighWaterMark(nullptr) * sizeof(StackType_t)));
    Raw raw;
    AdaptiveTransmission::History adaptiveHistory;
    std::uint32_t pendingFullEvents = 0;
    std::uint32_t lastPendingFullLogMs = 0;
    bool havePendingFullIdentity = false;
    char lastPendingFullBoot[33] = {};
    std::uint32_t lastPendingFullIndex = 0;
    while (true) {
        bool received = false;
#if RAW_VIBRATION_ENABLED
        // Only processingTask writes rawHold, and it writes back the item it
        // just removed from rawQueue when pending is full. It is therefore
        // older than anything still waiting in rawQueue.
        received = takeHeldRaw(raw);
        if (!received) received = xQueueReceive(rawQueue, &raw, pdMS_TO_TICKS(20)) == pdTRUE;
#else
        received = xQueueReceive(rawQueue, &raw, portMAX_DELAY) == pdTRUE;
#endif
        if (!received) continue;
        auto features = extract(raw, *workspace);
        AdaptiveTransmission::Sample adaptiveSample{};
        adaptiveSample.bootId = boot;
        adaptiveSample.index = raw.index;
        adaptiveSample.timestampUs = raw.startUs;
        std::copy(std::begin(features.values), std::end(features.values),
                  std::begin(adaptiveSample.values));
        adaptiveSample.qualityValid = features.quality == Quality::Valid;
        adaptiveSample.storagePressure = false;
        const auto transmission = AdaptiveTransmission::evaluate(
            adaptiveTransmissionConfig, adaptiveSample, adaptiveHistory);
        raw.transmissionMode = transmission.mode;
        raw.transmissionReason = transmission.reason;
#if RAW_VIBRATION_DEBUG
        static unsigned debugWindows = 0;
        if (raw.count > 0 && (++debugWindows % 10 == 1)) {
            double sum[3] = {}, sumSquares[3] = {};
            std::int16_t minimum[3] = {raw.xyz[0][0], raw.xyz[0][1], raw.xyz[0][2]};
            std::int16_t maximum[3] = {raw.xyz[0][0], raw.xyz[0][1], raw.xyz[0][2]};
            for (unsigned i = 0; i < raw.count; ++i) {
                for (unsigned axis = 0; axis < 3; ++axis) {
                    const auto value = raw.xyz[i][axis];
                    minimum[axis] = std::min(minimum[axis], value);
                    maximum[axis] = std::max(maximum[axis], value);
                    sum[axis] += value;
                    sumSquares[axis] += static_cast<double>(value) * value;
                }
            }
            Serial.printf("[RAW-DEBUG] samples=%u quality=%s X[min=%d max=%d mean=%.1f rms=%.1f] Y[min=%d max=%d mean=%.1f rms=%.1f] Z[min=%d max=%d mean=%.1f rms=%.1f]\n",
                raw.count, qualityName(features.quality), minimum[0], maximum[0], sum[0] / raw.count, std::sqrt(sumSquares[0] / raw.count),
                minimum[1], maximum[1], sum[1] / raw.count, std::sqrt(sumSquares[1] / raw.count), minimum[2], maximum[2], sum[2] / raw.count, std::sqrt(sumSquares[2] / raw.count));
        }
#endif
        const auto* audioWindow = raw.audioWindow;
        // Vibration quality is independent from acoustic validity. A FIFO
        // overrun must not turn a valid/silent audio window into audio_invalid.
        bool audioValid = audioWindow && audioReady.load() &&
            raw.audioGeneration==audioErrorGeneration.load();
        AcousticFeatures acoustic;
        if (audioValid) acoustic=analyzeCommonAudioWindow(audioWindow,COMMON_AUDIO_SAMPLES);
        audioValid = audioValid && raw.audioGeneration==audioErrorGeneration.load();
        if (raw.audioWindow) {
            heap_caps_free(raw.audioWindow);
            raw.audioWindow = nullptr;
        }
#if RAW_VIBRATION_ENABLED
        // Persist before making the window selectable. RAM remains the
        // bounded fallback only when storage is unavailable or full. The
        // filesystem write does not need the pending mutex.
        raw.quality = features.quality;
        const bool durable = writeRawSpool(raw);
#endif
        xSemaphoreTake(mutex, portMAX_DELAY);
#if RAW_VIBRATION_ENABLED
        if (pending.size() == PendingCapacity) {
            ++dropMetrics.processing_pending_full;
            xSemaphoreGive(mutex);
            bool held = durable;
            if (!held) {
                held = holdRaw(raw);
                if (!held) ++dropMetrics.raw_hold_full;
            }
            if (!held) {
                ++dropMetrics.actual_drop_total;
                ++processingDrops;
            }
            ++pendingFullEvents;
            if (!havePendingFullIdentity || lastPendingFullIndex != raw.index ||
                std::strncmp(lastPendingFullBoot, boot, sizeof(lastPendingFullBoot)) != 0) {
                ++dropMetrics.processing_pending_full_unique;
                havePendingFullIdentity = true;
                std::strncpy(lastPendingFullBoot, boot, sizeof(lastPendingFullBoot) - 1);
                lastPendingFullIndex = raw.index;
            }
            const auto nowMs = millis();
            if (pendingFullEvents == 1 || nowMs - lastPendingFullLogMs >= 1000U) {
                Serial.printf("[PROCESSING-DIAG] pending_full %s count=%lu bootId=%s windowIndex=%lu raw_queue=%u pending=%u\n",
                              held ? "held" : "unavailable",
                              static_cast<unsigned long>(pendingFullEvents),
                              boot,
                              static_cast<unsigned long>(raw.index),
                              rawQueue ? static_cast<unsigned>(uxQueueMessagesWaiting(rawQueue)) : 0U,
                              static_cast<unsigned>(PendingCapacity));
                lastPendingFullLogMs = nowMs;
            }
            vTaskDelay(1);
            continue;
        }
        if (!durable && !pending.push(raw)) {
            ++dropMetrics.processing_pending_full;
            ++dropMetrics.actual_drop_total;
            ++processingDrops;
        }
#else
        if (!pending.push(features)) {
            ++dropMetrics.processing_pending_full;
            ++dropMetrics.actual_drop_total;
            ++processingDrops;
        }
#endif
        latest = features;
        latestAudio=acoustic; latestAudioValid=audioValid;
        latestAudioGeneration = raw.audioGeneration;
        hasLatest = true;
        xSemaphoreGive(mutex);
#if RAW_VIBRATION_ENABLED
        vTaskDelay(1); // Keep raw backlog processing from starving the other tasks.
#endif
    }
}
String timestamp(std::uint64_t sampleUs, std::int64_t epochOffsetUs) {
    const std::int64_t epochUs = epochOffsetUs + sampleUs;
    const time_t seconds = epochUs / 1000000;
    struct tm utc;
    gmtime_r(&seconds, &utc);
    char date[32], value[40];
    strftime(date, sizeof(date), "%Y-%m-%dT%H:%M:%S", &utc);
    snprintf(value, sizeof(value), "%s.%06ldZ", date, static_cast<long>(epochUs % 1000000));
    return String(value);
}

unsigned transmissionReasonRank(AdaptiveTransmission::Reason reason) {
    switch (reason) {
        case AdaptiveTransmission::Reason::Severe: return 8;
        case AdaptiveTransmission::Reason::Quality: return 7;
        case AdaptiveTransmission::Reason::BaselineMissing: return 6;
        case AdaptiveTransmission::Reason::RmsHigh: return 5;
        case AdaptiveTransmission::Reason::RmsLow: return 4;
        case AdaptiveTransmission::Reason::RecoveryHold: return 3;
        case AdaptiveTransmission::Reason::StoragePressure: return 2;
        case AdaptiveTransmission::Reason::None: return 0;
    }
    return 0;
}

const char* transmissionModeName(AdaptiveTransmission::Mode mode, bool restored) {
    if (mode == AdaptiveTransmission::Mode::Priority) return "priority";
    return restored ? "replay" : "periodic";
}

void networkTask(void*) {
    // Frozen bytes/IDs remain identical across timeouts, auth errors and ACK loss.
    String body;
    unsigned size = 0;
    std::int64_t epochOffset = 0;
    std::uint32_t lastBatch = millis();
    std::uint32_t retryDelayMs = 2000;
    std::uint32_t retryCount = 0;
    std::uint32_t rawPostCount = 0;
    std::uint32_t batchCounter = 0;
    std::uint32_t activeBatchId = 0;
    AdaptiveTransmission::Mode batchTransmissionMode = AdaptiveTransmission::Mode::Periodic;
    AdaptiveTransmission::Reason batchTransmissionReason = AdaptiveTransmission::Reason::None;
    std::uint64_t activeDroppedWindows = 0;
    bool selectionLogged = false;
    unsigned rawBatchesSinceAux = 0;
    std::uint32_t lastAuxServiceMs = millis();
    std::uint32_t lastPendingAttemptLogMs = 0;
    std::uint32_t lastHealthHandoffCloseLogMs = 0;
    bool pendingAttemptLogInitialized = false;
    std::uint64_t reportedDrops = 0, reportedAudioDrops = 0;
    std::uint32_t txEventCounter = 0;
    std::uint32_t lastTxTraceMs = 0;
    bool detailedRawLogPrinted = false;
    char batchBootIds[BatchCapacity][33] = {};
#if RAW_VIBRATION_ENABLED
    bool batchFromSpool = false;
    bool batchFromPending = false;
    bool batchPriority = false;
    unsigned spoolSlots[BatchCapacity] = {};
#endif
    // Keep one owner for the Raw HTTP client.  The task never exits, so the
    // TLS client is not destroyed between requests and cannot close a shared
    // VFS handle from a per-request scope.
    WiFiClientSecure secure;
    BackendHttp http;
    while (true) {
#if RAW_VIBRATION_ENABLED
        // Raw batches are sent as soon as processing publishes one; this is
        // the only scheduler polling delay in the raw path.
        vTaskDelay(pdMS_TO_TICKS(20));
#else
        vTaskDelay(pdMS_TO_TICKS(200));
#endif
        xSemaphoreTake(mutex, portMAX_DELAY);
        const auto dropped = pending.dropped + processingDrops.load();
        xSemaphoreGive(mutex);
        if (dropped != reportedDrops) {
            Serial.printf("[WINDOW] Dropped windows=%llu (queue overflow; sequence gaps retained)\n", dropped);
            reportedDrops = dropped;
        }
        const auto droppedAudio = audioDrops.load();
        if (droppedAudio != reportedAudioDrops) {
            Serial.printf("[WINDOW] Dropped audio windows=%lu (best-effort metadata; vibration retained)\n",
                          static_cast<unsigned long>(droppedAudio));
            reportedAudioDrops = droppedAudio;
        }
        if (WiFi.status() != WL_CONNECTED) continue;
        const std::uint32_t traceNowMs = millis();
        const bool traceEvent = traceNowMs - lastTxTraceMs >= 1000U;
        const std::uint32_t txEventId = traceEvent ? ++txEventCounter : 0U;
        const std::uint64_t txEventStartedUs = static_cast<std::uint64_t>(esp_timer_get_time());
        if (traceEvent) {
            lastTxTraceMs = traceNowMs;
            Serial.printf("[RAW-TX-STAGE] event=%lu stage=loop_ready elapsed_us=0 size=0\n",
                          static_cast<unsigned long>(txEventId));
        }
        const auto traceStage = [&](const char* stage, unsigned stageSize) {
            if (!traceEvent) return;
            Serial.printf("[RAW-TX-STAGE] event=%lu stage=%s elapsed_us=%llu size=%u\n",
                          static_cast<unsigned long>(txEventId), stage,
                          static_cast<unsigned long long>(
                              static_cast<std::uint64_t>(esp_timer_get_time()) - txEventStartedUs),
                          stageSize);
        };
        struct timeval tv;
        gettimeofday(&tv, nullptr);
        if (tv.tv_sec < 1700000000) {
            // The API requires an RFC3339 timestamp. Reuse the existing
            // backend-time fallback instead of fabricating UTC or discarding
            // the pending window. A failed fallback leaves the batch intact.
            Serial.println("[TIME] Raw blocked: UTC invalid; trying backend fallback.");
            if (!syncTimeFromBackend()) {
                vTaskDelay(pdMS_TO_TICKS(250));
                continue;
            }
            gettimeofday(&tv, nullptr);
            if (tv.tv_sec < 1700000000) {
                Serial.println("[TIME] Raw deferred: backend fallback returned invalid UTC.");
                vTaskDelay(pdMS_TO_TICKS(250));
                continue;
            }
        }
        // One UTC anchor per boot: replays and buffered windows retain the same
        // mapping even if NTP adjusts the wall clock between batches.
        if (!epochOffset) epochOffset=static_cast<std::int64_t>(tv.tv_sec)*1000000 + tv.tv_usec - esp_timer_get_time();
        rawTimestampEpochOffsetUs.store(epochOffset, std::memory_order_relaxed);
        traceStage("time_ready", size);
        if (!size) {
#if !RAW_VIBRATION_ENABLED
            if (millis() - lastBatch < BatchIntervalMs) continue;
#endif
            const std::uint64_t periodicCutoffUs =
#if RAW_VIBRATION_ENABLED
                PeriodicScheduler::cutoff(static_cast<std::uint64_t>(esp_timer_get_time()));
#else
                UINT64_MAX;
#endif
            size = 0;
#if RAW_VIBRATION_ENABLED
            // Durable records are selected before RAM fallback. Priority
            // records still win over periodic records, but every selected
            // record has already survived a write/read-back check.
            batchFromSpool = false;
            batchFromPending = false;
            batchPriority = false;
            // Filesystem scans do not touch pending RAM; do not hold the
            // processing mutex across a potentially slow LittleFS scan.
            traceStage("priority_scan_begin", size);
            size = loadRawSpoolBatch(batch, spoolSlots, BatchCapacity,
                                     periodicCutoffUs, batchBootIds, true);
            batchFromSpool = size != 0;
            batchPriority = batchFromSpool;
            if (!size) {
                xSemaphoreTake(mutex, portMAX_DELAY);
                size = selectPriorityPending(batch, BatchCapacity);
                xSemaphoreGive(mutex);
                batchPriority = size != 0;
                for (unsigned i = 0; i < size; ++i) {
                    std::strncpy(batchBootIds[i], boot, 32);
                    batchBootIds[i][32] = '\0';
                }
                traceStage("pending_select", size);
            }
            traceStage("priority_scan_end", size);
#endif
#if !RAW_VIBRATION_ENABLED
            xSemaphoreTake(mutex, portMAX_DELAY);
            while (size < std::min(pending.size(), BatchCapacity) &&
                   pending.at(size).startUs <= periodicCutoffUs) {
                batch[size] = pending.at(size);
                ++size;
            }
            xSemaphoreGive(mutex);
#endif
#if RAW_VIBRATION_ENABLED
            // A legacy spool has no bootId and is held aside. If no priority
            // record was available, durable periodic records still precede
            // the RAM fallback.
            if (!size) {
                traceStage("periodic_scan_begin", size);
                size = loadRawSpoolBatch(batch, spoolSlots, BatchCapacity,
                                         periodicCutoffUs, batchBootIds, false);
                batchFromSpool = size != 0;
                batchPriority = false;
            }
            if (!size) {
                xSemaphoreTake(mutex, portMAX_DELAY);
                while (size < std::min(pending.size(), BatchCapacity) &&
                       pending.at(size).startUs <= periodicCutoffUs) {
                    batch[size] = pending.at(size);
                    std::strncpy(batchBootIds[size], boot, 32);
                    batchBootIds[size][32] = '\0';
                    ++size;
                }
                xSemaphoreGive(mutex);
                batchFromPending = size != 0;
                batchPriority = false;
                traceStage("pending_select", size);
            }
            traceStage("periodic_scan_end", size);
#endif
            if (!size) {
                serviceNetworkAuxiliary();
                continue;
            }
            JsonDocument doc;
            auto windows = doc["windows"].to<JsonArray>();
            for (unsigned i=0; i<size; ++i) {
                const auto& f = batch[i];
                auto w = windows.add<JsonObject>();
                w["schemaVersion"]=1; w["deviceId"]=DEVICE_ID;
                w["siteId"]=SITE_ID; w["assetId"]=ASSET_ID; w["bootId"]=
#if RAW_VIBRATION_ENABLED
                    batchBootIds[i];
#else
                    boot;
#endif
                w["windowIndex"]=f.index; w["startUptimeUs"]=f.startUs;
                w["timestamp"]=timestamp(f.startUs, epochOffset);
                w["profileId"]=Profile; w["sampleRateHz"]=Rate; w["sampleCount"]=f.count;
                w["unit"]="g";
                auto axes=w["axes"].to<JsonArray>(); axes.add("X"); axes.add("Y"); axes.add("Z");
                w["quality"]=qualityName(f.quality);
#if RAW_VIBRATION_ENABLED
                w["profileId"]="adxl345-800hz-xyz-counts-v1";
                w["unit"]="count"; w["gPerCount"]=0.0039;
                w["encoding"]="base64-int16le-xyz";
                if (!encodeRaw(f,w)) {size=0; break;}
#else
                if (f.quality==Quality::Valid) {
                    auto values=w["features"].to<JsonArray>();
                    for (double v:f.values) values.add(v);
                } else w["features"]=nullptr;
#endif
            }
            batchTransmissionMode = AdaptiveTransmission::Mode::Periodic;
            batchTransmissionReason = AdaptiveTransmission::Reason::None;
#if RAW_VIBRATION_ENABLED
            for (unsigned i = 0; i < size; ++i) {
                const auto reason = batch[i].transmissionReason;
                if (batch[i].transmissionMode == AdaptiveTransmission::Mode::Priority)
                    batchTransmissionMode = AdaptiveTransmission::Mode::Priority;
                if (transmissionReasonRank(reason) > transmissionReasonRank(batchTransmissionReason))
                    batchTransmissionReason = reason;
            }
#endif
            activeBatchId = ++batchCounter;
            selectionLogged = false;
            const bool restoredBatch =
#if RAW_VIBRATION_ENABLED
                batchFromSpool;
#else
                false;
#endif
            xSemaphoreTake(mutex, portMAX_DELAY);
            const std::uint64_t droppedAtBatchCreation =
                static_cast<std::uint64_t>(pending.dropped) + processingDrops.load();
            activeDroppedWindows = droppedAtBatchCreation;
            xSemaphoreGive(mutex);
            auto transmission = doc["transmission"].to<JsonObject>();
            TransmissionMetadata::write(
                transmission,
                transmissionModeName(batchTransmissionMode, restoredBatch),
                batchTransmissionReason,
                adaptiveBaselineConfigured ? adaptiveTransmissionConfig.baselineId : "unconfigured",
                droppedAtBatchCreation);
            if (!size || doc.overflowed()) {size=0; batchFromSpool=false; continue;}
            body="";
            serializeJson(doc, body);
#if RAW_VIBRATION_ENABLED
            if (body.length() > MaxRawRequestBytes) {
                Serial.printf("[WINDOW] batch rejected locally: %u bytes exceeds %u-byte limit\n",
                              static_cast<unsigned>(body.length()),
                              static_cast<unsigned>(MaxRawRequestBytes));
                size=0;
                continue;
            }
#endif
            traceStage("payload_ready", size);
        }
#if RAW_VIBRATION_ENABLED
        traceStage("pre_send_persist_begin", size);
        if (!batchFromSpool) {
            // Persist the exact batch before opening TLS.  A failed write keeps
            // the RAM batch and any completed spool records for retry; the
            // identity check in writeRawSpool prevents duplicate slots.
            bool persisted = true;
            for (unsigned i = 0; i < size; ++i) {
                if (!writeRawSpool(batch[i], &spoolSlots[i])) {
                    persisted = false;
                    break;
                }
            }
            if (!persisted) {
                traceStage("pre_send_persist_end", 0);
                Serial.println("[RAW-SPOOL] pre-send persistence failed; network send deferred and batch retained.");
                size = 0;
                body = "";
                batchFromPending = false;
                continue;
            }
            batchFromSpool = true;
            batchFromPending = true;
        }
        traceStage("pre_send_persist_end", size);
#endif
        if (!selectionLogged) {
        const char* batchQuality = size ? qualityName(batch[0].quality) : "unknown";
        for (unsigned i = 1; i < size; ++i)
            if (batch[i].quality != batch[0].quality) { batchQuality = "mixed"; break; }
        const bool restoredForLog =
#if RAW_VIBRATION_ENABLED
            batchFromSpool;
#else
            false;
#endif
        Serial.printf("[TX-SELECT] batch=%lu mode=%s reason=%s count=%u quality=%s stored=%u dropped=%llu indexes=",
                      static_cast<unsigned long>(activeBatchId),
                      transmissionModeName(batchTransmissionMode, restoredForLog),
                      AdaptiveTransmission::reasonName(batchTransmissionReason),
                      size, batchQuality,
#if RAW_VIBRATION_ENABLED
                      size,
#else
                      0U,
#endif
                      static_cast<unsigned long long>(activeDroppedWindows));
        for (unsigned i = 0; i < size; ++i) {
            if (i) Serial.print(',');
            Serial.print(batch[i].index);
        }
        Serial.print(" boots=");
        for (unsigned i = 0; i < size; ++i) {
            if (i) Serial.print(',');
#if RAW_VIBRATION_ENABLED
            Serial.print(batchBootIds[i]);
#else
            Serial.print(boot);
#endif
        }
        Serial.println();
        selectionLogged = true;
        }
        const String ingest(INGEST_URL);
        const String suffix("/api/telemetry/ingest");
        if (!ingest.endsWith(suffix)) continue;
        const String url=ingest.substring(0,ingest.length()-suffix.length())+
#if RAW_VIBRATION_ENABLED
                         "/api/devices/"+DEVICE_ID+"/raw-vibration-windows";
#else
                         "/api/devices/"+DEVICE_ID+"/vibration-windows";
#endif
        secure.setHandshakeTimeout(3); http.setConnectTimeout(1500); http.setTimeout(1500);
        // HTTPClient preserves a reusable connection when the response allows it.
        // Keep ownership in this task; HTTPClient::end() owns socket teardown.
        http.setReuse(true);
        http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
        HttpDiag httpDiag;
        xSemaphoreTake(mutex, portMAX_DELAY);
        const unsigned pendingBefore = pending.size();
        xSemaphoreGive(mutex);
        const std::uint32_t pendingAttemptNow = millis();
        if (!pendingAttemptLogInitialized ||
            pendingAttemptNow - lastPendingAttemptLogMs >= 1000U) {
            Serial.printf("[RAW-ATTEMPT] raw_attempt=%lu begin_result=pending gate_wait_ms=0 request_ms=0 status=pending fail_stage=none pending_before=%u pending_after=%u retry_count=%lu\n",
                          static_cast<unsigned long>(retryCount + 1),
                          pendingBefore, pendingBefore,
                          static_cast<unsigned long>(retryCount));
            lastPendingAttemptLogMs = pendingAttemptNow;
            pendingAttemptLogInitialized = true;
        }
        rawNetworkBusy.store(true);
        if (!tryReserveRawNetwork()) {
            rawNetworkBusy.store(false);
            continue;
        }
        const bool reuseCandidate = secure.connected();
        traceStage("http_begin", size);
        if (!beginBackendHttp(http, secure, url.c_str())) {
            xSemaphoreTake(mutex, portMAX_DELAY);
            const unsigned pendingAfterBegin = pending.size();
            xSemaphoreGive(mutex);
            Serial.printf("[RAW-ATTEMPT] raw_attempt=%lu reuse_candidate=%s begin_result=fail gate_wait_ms=%lu request_ms=0 status=-1 fail_stage=begin pending_before=%u pending_after=%u retry_count=%lu\n",
                          static_cast<unsigned long>(retryCount + 1),
                          reuseCandidate ? "yes" : "no",
                          static_cast<unsigned long>(http.lastGateWaitMs), pendingBefore,
                          pendingAfterBegin, static_cast<unsigned long>(retryCount));
            logHttpDiag("raw", url.c_str(), false, httpDiag, -1, 0, "begin");
            char tlsError[160]{};
            const int tlsCode = secure.lastError(tlsError, sizeof(tlsError));
            Serial.printf("[TLS-DIAG] stage=begin code=%d error=%s\n", tlsCode,
                          tlsError[0] ? tlsError : "none");
            releaseRawNetwork();
            rawNetworkBusy.store(false);
            // Keep the batch intact and use the same bounded transport
            // backoff as POST failures; a failed begin must not hot-loop.
            vTaskDelay(pdMS_TO_TICKS(retryDelayMs));
            retryDelayMs = std::min<std::uint32_t>(30000, retryDelayMs * 2);
            ++retryCount;
            continue;
        }
        http.addHeader("Authorization", String("Bearer ")+INGEST_TOKEN);
        http.addHeader("Content-Type", "application/json");
        const bool logRawStorageDiagnostic =
            ++rawPostCount <= 4U || (rawPostCount % 64U) == 0U;
        if (logRawStorageDiagnostic) {
            logRawBufferCanaries("raw_post_before");
            logStorageMemoryDiagnostic("raw_post_before");
        }
        const uint64_t requestStartedMs = static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;
        const int status=http.POST(body);
        httpDiag.requestMs = static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL - requestStartedMs;
        if (logRawStorageDiagnostic) {
            logRawBufferCanaries("raw_post_after");
            logStorageMemoryDiagnostic("raw_post_after");
        }
        String ackBody;
        bool accepted=false;
        std::size_t matchedAckCount = 0;
        if (status > 0 && http.getSize()>=0 && http.getSize()<8192) {
            const uint64_t bodyStartedMs = static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;
            ackBody=http.getString();
            httpDiag.bodyMs = static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL - bodyStartedMs;
            accepted = strictRawAck(status, ackBody, batch, size, batchBootIds, &matchedAckCount);
        }
        const bool healthHandoffRequested =
            healthNetworkRequested.load(std::memory_order_acquire);
        char tlsError[160]{};
        int tlsCode = 0;
        if (status <= 0) tlsCode = secure.lastError(tlsError, sizeof(tlsError));
        if (healthHandoffRequested) {
            // Let HTTPClient close the idle Raw socket exactly once so the
            // auxiliary Health TLS connection can acquire resources.
            http.setReuse(false);
            const std::uint32_t now = millis();
            if (now - lastHealthHandoffCloseLogMs >= 1000U) {
                Serial.println("[RAW] health handoff: closing reusable socket");
                lastHealthHandoffCloseLogMs = now;
            }
        }
        http.end();
        if (healthHandoffRequested) http.setReuse(true);
        releaseRawNetwork();
        rawNetworkBusy.store(false);
        xSemaphoreTake(mutex, portMAX_DELAY);
        const unsigned pendingAfter = pending.size();
        xSemaphoreGive(mutex);
        Serial.printf("[RAW-ATTEMPT] raw_attempt=%lu reuse_candidate=%s begin_result=ok gate_wait_ms=%lu request_ms=%llu status=%d fail_stage=%s pending_before=%u pending_after=%u retry_count=%lu\n",
                      static_cast<unsigned long>(retryCount + 1),
                      reuseCandidate ? "yes" : "no",
                      static_cast<unsigned long>(http.lastGateWaitMs),
                      static_cast<unsigned long long>(httpDiag.requestMs), status,
                      accepted ? "none" : (status > 0 ? "ack" : "post"), pendingBefore,
                      pendingAfter, static_cast<unsigned long>(retryCount));
        logHttpDiag("raw", url.c_str(), true, httpDiag, status, ackBody.length(), status > 0 ? "none" : "post");
        if (status <= 0) {
            Serial.printf("[TLS-DIAG] stage=post code=%d error=%s\n", tlsCode,
                          tlsError[0] ? tlsError : "none");
        }
        Serial.printf("[TX-HTTP] batch=%lu status=%d request_ms=%llu body_read_ms=%llu total_ms=%llu\n",
                      static_cast<unsigned long>(activeBatchId), status,
                      static_cast<unsigned long long>(httpDiag.requestMs),
                      static_cast<unsigned long long>(httpDiag.bodyMs),
                      static_cast<unsigned long long>(static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL - httpDiag.startedMs));
        Serial.printf("[TX-ACK] batch=%lu expected=%u matched=%u status=%d\n",
                      static_cast<unsigned long>(activeBatchId), size,
                      static_cast<unsigned>(matchedAckCount), status);
        if (accepted) {
            if (!detailedRawLogPrinted) {
                Serial.printf("[WINDOW-DETAIL] URL=%s bootId=%s windowIndex=", url.c_str(), batchBootIds[0]);
                for (unsigned i=0; i<size; ++i) {
                    if (i) Serial.print(',');
                    Serial.print(batch[i].index);
                }
                Serial.printf(" HTTP=%d ACK=%s\n", status, ackBody.c_str());
                detailedRawLogPrinted = true;
            }
            bool cleared = true;
#if RAW_VIBRATION_ENABLED
            if (batchFromSpool && accepted && (status == 200 || status == 202))
                cleared = removeRawSpoolBatch(spoolSlots, size);
            if (cleared && batchFromPending) {
                xSemaphoreTake(mutex, portMAX_DELAY);
                cleared = batchPriority
                    ? pending.acknowledgeMatching(batch, size, sameRawIdentity)
                    : pending.acknowledge(size);
                xSemaphoreGive(mutex);
            }
#else
            xSemaphoreTake(mutex, portMAX_DELAY);
            cleared = pending.acknowledge(size);
            xSemaphoreGive(mutex);
#endif
            if (!cleared) Serial.println("[RAW-SPOOL] ACK received but file removal failed; retained for retry.");
#if RAW_VIBRATION_ENABLED
            unsigned pendingAfterAck = 0;
            xSemaphoreTake(mutex, portMAX_DELAY);
            pendingAfterAck = pending.size();
            xSemaphoreGive(mutex);
            // The full 512-slot occupancy scan is diagnostic-only and held
            // LittleFS long enough to starve capture. Counters remain the
            // source of truth for write/capacity failures.
            Serial.printf("[STORE-DELETE] batch=%lu removed=%u failed=%u pending_after=%u spool_state=deferred spool_capacity=%u\n",
                          static_cast<unsigned long>(activeBatchId), size,
                          cleared ? 0U : size, pendingAfterAck, RawSpoolSlots);
#else
            Serial.printf("[STORE-DELETE] batch=%lu removed=%u failed=%u remaining=%u\n",
                          static_cast<unsigned long>(activeBatchId), size,
                          cleared ? 0U : size, pendingAfter);
#endif
            Serial.printf("[WINDOW] ACK %u windows through index %lu\n",size,static_cast<unsigned long>(batch[size-1].index));
            size=0; body="";
#if RAW_VIBRATION_ENABLED
            batchFromSpool=false; batchFromPending=false; batchPriority=false;
#endif
            lastBatch=millis();
            retryDelayMs = 2000;
            retryCount = 0;
            // Give the equal-priority auxiliary network tasks a bounded
            // reservation opportunity between successful Raw batches.
            ++rawBatchesSinceAux;
            if (rawBatchesSinceAux >= 4U || millis() - lastAuxServiceMs >= 5000U) {
                // Bounded fairness: wait for this Raw request to finish, then
                // give health/replay one turn without cancelling Raw backlog.
                serviceNetworkAuxiliary(true);
                rawBatchesSinceAux = 0;
                lastAuxServiceMs = millis();
            }
            vTaskDelay(pdMS_TO_TICKS(300));
        } else {
            const RawStatus::Kind statusKind =
                RawStatus::classify(status, ackBody.c_str(), accepted);
            Serial.printf("[TX-RESULT] batch=%lu status=%d class=%s retained=1 retry=%lu\n",
                          static_cast<unsigned long>(activeBatchId), status,
                          RawStatus::name(statusKind),
                          static_cast<unsigned long>(retryCount + 1));
            Serial.printf("[WINDOW] HTTP %d; batch retained\n",status);
            if (status == 409 && ackBody.length())
                Serial.printf("[RAW-409-BODY] %s\n", ackBody.c_str());
            if (statusKind == RawStatus::Kind::PermanentQuarantine ||
                statusKind == RawStatus::Kind::OrderingQuarantine) {
                const bool quarantined = batchFromSpool &&
                    quarantineRawSpoolBatch(spoolSlots, size);
                if (batchFromPending) {
                    xSemaphoreTake(mutex, portMAX_DELAY);
                    const bool moved = pending.acknowledgeMatching(batch, size, sameRawIdentity);
                    xSemaphoreGive(mutex);
                    if (!moved) Serial.println("[RAW-SPOOL-QUARANTINE] RAM mirror was not removed; durable record retained.");
                }
                Serial.printf("[RAW-SPOOL-QUARANTINE] batch=%lu class=%s persisted=%s\n",
                              static_cast<unsigned long>(activeBatchId),
                              RawStatus::name(statusKind), quarantined ? "yes" : "no");
                size = 0;
                body = "";
                batchFromSpool = false;
                batchFromPending = false;
                batchPriority = false;
                retryDelayMs = 2000;
                retryCount = 0;
                continue;
            }
            const std::uint32_t delayMs = retryDelayMs;
            vTaskDelay(pdMS_TO_TICKS(delayMs));
            retryDelayMs = std::min<std::uint32_t>(30000, retryDelayMs * 2);
            ++retryCount;
        }
    }
}
bool snapshot(VibrationFeatures& vib, AcousticFeatures& audio) {
    Features f;
    std::uint32_t generation;
    bool available;
    bool hasLatestSnapshot;
    bool latestAudioValidSnapshot;
    xSemaphoreTake(mutex, portMAX_DELAY);
    available=hasLatest;
    hasLatestSnapshot=hasLatest;
    latestAudioValidSnapshot=latestAudioValid;
    f=latest; generation=latestAudioGeneration;
    audio=latestAudio;
    xSemaphoreGive(mutex);
    // The raw capture already copied the exact audio window at completion.
    // Processing/network backpressure may make the cached feature older than
    // 1.5 s; age alone does not make this vibration/audio pair unsynchronized.
    const bool validVibration=f.quality==Quality::Valid;
    const bool ready=audioReady.load();
    const std::uint32_t currentGeneration=audioErrorGeneration.load();
    if (!available || !validVibration || !ready || generation!=currentGeneration) {
        const char* reason=!hasLatestSnapshot ? "no_latest" :
            !latestAudioValidSnapshot ? "audio_invalid" :
            !validVibration ? "vibration_invalid" :
            !ready ? "audio_not_ready" : "audio_generation_changed";
        static std::uint32_t lastLogMs=0;
        const std::uint32_t now=millis();
        if (now-lastLogMs>=1000) {
            Serial.printf("[SYNC] snapshot unavailable: reason=%s generation=%lu current=%lu ready=%s quality=%u\n",
                          reason, static_cast<unsigned long>(generation),
                          static_cast<unsigned long>(currentGeneration),
                          ready ? "yes" : "no", static_cast<unsigned>(f.quality));
            lastLogMs=now;
        }
        return false;
    }
    vib.rmsX=f.values[0]; vib.rmsY=f.values[7]; vib.rmsZ=f.values[14];
    vib.totalRms=std::sqrt(vib.rmsX*vib.rmsX+vib.rmsY*vib.rmsY+vib.rmsZ*vib.rmsZ);
    unsigned axis=vib.rmsY>vib.rmsX?1:0;
    if (vib.rmsZ>f.values[axis*7]) axis=2;
    vib.fftAxis="XYZ"[axis]; vib.peakHz=f.peakHz[axis];
    if (generation!=audioErrorGeneration.load()) {
        Serial.printf("[SYNC] snapshot invalidated during copy: generation=%lu current=%lu\n",
                      static_cast<unsigned long>(generation),
                      static_cast<unsigned long>(audioErrorGeneration.load()));
        return false;
    }
    return true;
}
bool start() {
    Serial.printf("[PSRAM] found=%s size=%u free=%u\n",
                  psramFound() ? "yes" : "no",
                  static_cast<unsigned>(ESP.getPsramSize()),
                  static_cast<unsigned>(ESP.getFreePsram()));
    snprintf(boot,sizeof(boot),"%08lx%08lx%08lx%08lx",
             static_cast<unsigned long>(esp_random()),static_cast<unsigned long>(esp_random()),
             static_cast<unsigned long>(esp_random()),static_cast<unsigned long>(esp_random()));
    mutex=xSemaphoreCreateMutex();
    rawQueue=xQueueCreate(RawQueueCapacity,sizeof(Raw));
#if RAW_VIBRATION_ENABLED
    initializeRawSpoolAdmission();
    initializeRawSpoolCursor();
#endif
    const auto alloc = [](size_t bytes) {
        void* value = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        return value ? value : heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    };
    workspace = static_cast<Workspace*>(alloc(sizeof(Workspace)));
    batch = static_cast<PendingWindow*>(alloc(sizeof(PendingWindow) * BatchCapacity));
#if RAW_VIBRATION_ENABLED
    rawHoldAllocation = static_cast<unsigned char*>(allocateGuardedRawBuffer(sizeof(Raw) * RawHoldCapacity));
    rawHold = rawHoldAllocation ? reinterpret_cast<Raw*>(rawHoldAllocation + DiagnosticCanaryBytes) : nullptr;
    if (rawHold) for (unsigned i = 0; i < RawHoldCapacity; ++i)
        ::new (static_cast<void*>(rawHold + i)) Raw();
    rawBytesAllocation = static_cast<unsigned char*>(allocateGuardedRawBuffer(RawBytesCapacity));
    rawBytes = rawBytesAllocation ? rawBytesAllocation + DiagnosticCanaryBytes : nullptr;
    encodedRawAllocation = static_cast<unsigned char*>(allocateGuardedRawBuffer(EncodedRawCapacity));
    encodedRaw = encodedRawAllocation ? encodedRawAllocation + DiagnosticCanaryBytes : nullptr;
#endif
    Serial.printf("[PSRAM] buffers rawQueue=%u processingQueue=%u workspace=%s batch=%s",
                  RawQueueCapacity, PendingCapacity,
                  workspace && esp_ptr_external_ram(workspace) ? "psram" : "internal",
                  batch && esp_ptr_external_ram(batch) ? "psram" : "internal");
#if RAW_VIBRATION_ENABLED
    Serial.printf(" rawHold=%u(%s) rawBytes=%u(%s) encodedRaw=%u(%s)",
                  RawHoldCapacity,
                  rawHold && esp_ptr_external_ram(rawHold) ? "psram" : "internal",
                  static_cast<unsigned>(RawBytesCapacity),
                  rawBytes && esp_ptr_external_ram(rawBytes) ? "psram" : "internal",
                  static_cast<unsigned>(EncodedRawCapacity),
                  encodedRaw && esp_ptr_external_ram(encodedRaw) ? "psram" : "internal");
#endif
    Serial.println();
    if (!mutex || !rawQueue || !workspace || !batch
#if RAW_VIBRATION_ENABLED
        || !rawHold || !rawBytes || !encodedRaw
#endif
    ) return false;
    return xTaskCreatePinnedToCore(processingTask,"WindowFeatures",16384,nullptr,2,nullptr,0)==pdPASS &&
           xTaskCreatePinnedToCore(captureTask,"VibrationFIFO",8192,nullptr,4,nullptr,0)==pdPASS &&
           xTaskCreatePinnedToCore(networkTask,"WindowHTTPS",12288,nullptr,1,&networkTaskHandle,1)==pdPASS;
}
}
#endif
