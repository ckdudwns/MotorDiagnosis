#include "storage/littlefs_window_store.h"

#include <Arduino.h>
#include <LittleFS.h>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include <cstring>

#include "config/app_config.h"

namespace {

constexpr char kRetryDirectory[] = "/retry";
constexpr uint32_t kFeatureRecordMagic = 0x4D324654;
constexpr uint16_t kFeatureRecordVersion = 3;
constexpr size_t kRecordWriteBudgetBytes = 1024;
constexpr size_t kProtectedReserveBytes = kRecordWriteBudgetBytes * 2;

uint32_t crc32Update(uint32_t crc, const uint8_t* data, size_t length) {
    for (size_t index = 0; index < length; ++index) {
        crc ^= data[index];
        for (uint8_t bit = 0; bit < 8; ++bit) {
            crc = (crc & 1U) != 0U ? (crc >> 1U) ^ 0xEDB88320U
                                   : crc >> 1U;
        }
    }
    return crc;
}

bool validHeader(const FeatureRecordHeader& header) {
    return header.magic == kFeatureRecordMagic &&
           header.version == kFeatureRecordVersion &&
           header.headerBytes == sizeof(header) &&
           header.featureBytes ==
               (header.metadata.featuresValid ? sizeof(VibrationFeatures) : 0) &&
           header.recordBytes ==
               sizeof(header) + header.featureBytes + sizeof(uint32_t);
}

bool writeRecord(fs::File& file, const PendingWindow& pending,
                 size_t& bytesWritten) {
    const size_t featureBytes = pending.metadata.featuresValid
                                    ? sizeof(VibrationFeatures)
                                    : 0;
    FeatureRecordHeader header{};
    header.magic = kFeatureRecordMagic;
    header.version = kFeatureRecordVersion;
    header.headerBytes = sizeof(header);
    header.recordBytes = sizeof(header) + featureBytes + sizeof(uint32_t);
    header.metadata = pending.metadata;
    header.featureBytes = featureBytes;

    uint32_t crc = crc32Update(
        0xFFFFFFFFU, reinterpret_cast<const uint8_t*>(&header), sizeof(header));
    if (featureBytes > 0) {
        crc = crc32Update(
            crc, reinterpret_cast<const uint8_t*>(&pending.stats.features),
            featureBytes);
    }
    crc = ~crc;

    bytesWritten = 0;
    bytesWritten += file.write(reinterpret_cast<const uint8_t*>(&header),
                               sizeof(header));
    if (featureBytes > 0) {
        bytesWritten += file.write(
            reinterpret_cast<const uint8_t*>(&pending.stats.features),
            featureBytes);
    }
    bytesWritten += file.write(reinterpret_cast<const uint8_t*>(&crc),
                               sizeof(crc));
    return bytesWritten == header.recordBytes;
}

bool readExact(fs::File& file, void* data, size_t bytes) {
    return file.read(reinterpret_cast<uint8_t*>(data), bytes) == bytes;
}

const char* fileBaseName(const char* name) {
    const char* slash = strrchr(name, '/');
    return slash == nullptr ? name : slash + 1;
}

CandidateReason reasonFromName(const char* name) {
    const char* base = fileBaseName(name);
    const char type = strlen(base) > 9 && base[8] == '_' ? base[9] : base[0];
    switch (type) {
        case 'S':
            return CandidateReason::anomalyStart;
        case 'A':
            return CandidateReason::anomalyActive;
        case 'R':
            return CandidateReason::recovery;
        case 'P':
        default:
            return CandidateReason::periodic;
    }
}

bool sequenceFromName(const char* name, uint32_t& result) {
    const char* base = fileBaseName(name);
    if (strlen(base) < 10 || base[8] != '_') {
        return false;
    }

    uint32_t sequence = 0;
    for (size_t index = 0; index < 8; ++index) {
        if (base[index] < '0' || base[index] > '9') {
            return false;
        }
        sequence = sequence * 10U +
                   static_cast<uint32_t>(base[index] - '0');
    }
    result = sequence;
    return true;
}

void copyPath(char (&destination)[80], const char* source) {
    snprintf(destination, sizeof(destination), "%s", source);
}

void copyRetryPath(char (&destination)[80], const char* name) {
    if (name[0] == '/') {
        copyPath(destination, name);
    } else {
        snprintf(destination, sizeof(destination), "%s/%s", kRetryDirectory,
                 name);
    }
}

void copyBootKey(char (&destination)[9], const char* bootId) {
    snprintf(destination, sizeof(destination), "%.8s", bootId);
}

}  // namespace

void LittleFsWindowStore::lock() {
    if (mutex_ != nullptr) {
        xSemaphoreTake(static_cast<SemaphoreHandle_t>(mutex_), portMAX_DELAY);
    }
}

void LittleFsWindowStore::unlock() {
    if (mutex_ != nullptr) {
        xSemaphoreGive(static_cast<SemaphoreHandle_t>(mutex_));
    }
}

bool LittleFsWindowStore::begin() {
    if (!LittleFS.begin(false)) {
        Serial.println("[STORAGE] littlefs_mount_failed");
        return false;
    }

    if (mutex_ == nullptr) {
        mutex_ = xSemaphoreCreateMutex();
    }
    if (mutex_ == nullptr) {
        Serial.println("[STORAGE] mutex_create_failed");
        return false;
    }

    if (!LittleFS.exists(kRetryDirectory) &&
        !LittleFS.mkdir(kRetryDirectory)) {
        Serial.println("[STORAGE] retry_directory_create_failed");
        return false;
    }

    uint32_t maxSequence = 0;
    fs::File directory = LittleFS.open(kRetryDirectory, FILE_READ);
    if (directory && directory.isDirectory()) {
        fs::File file = directory.openNextFile();
        while (file) {
            uint32_t sequence = 0;
            if (strstr(file.name(), ".tmp") == nullptr &&
                sequenceFromName(file.name(), sequence) &&
                sequence > maxSequence) {
                maxSequence = sequence;
            }
            file.close();
            file = directory.openNextFile();
        }
        directory.close();
    }
    nextSequence_ = maxSequence == UINT32_MAX ? 1 : maxSequence + 1;

    const size_t totalBytes = LittleFS.totalBytes();
    const size_t usedBytes = LittleFS.usedBytes();
    Serial.print("[STORAGE] ready path=");
    Serial.print(kRetryDirectory);
    Serial.print(" fs_total_bytes=");
    Serial.print(totalBytes);
    Serial.print(" fs_used_bytes=");
    Serial.print(usedBytes);
    Serial.print(" fs_free_bytes=");
    Serial.println(totalBytes >= usedBytes ? totalBytes - usedBytes : 0);
    Serial.print("[STORAGE] record_write_budget_bytes=");
    Serial.print(kRecordWriteBudgetBytes);
    Serial.print(" protected_reserve_bytes=");
    Serial.println(kProtectedReserveBytes);
    return true;
}

bool LittleFsWindowStore::evictOldest(CandidateReason reason) {
    fs::File directory = LittleFS.open(kRetryDirectory, FILE_READ);
    if (!directory || !directory.isDirectory()) {
        return false;
    }

    char selectedPath[80] = {};
    uint32_t selectedSequence = UINT32_MAX;
    size_t selectedBytes = 0;
    fs::File file = directory.openNextFile();
    while (file) {
        const char* name = file.name();
        char candidatePath[80] = {};
        uint32_t sequence = 0;
        if (name != nullptr) {
            copyRetryPath(candidatePath, name);
        }
        const bool candidateEligible =
            name != nullptr && strstr(name, ".tmp") == nullptr &&
            strcmp(candidatePath, inFlightPath_) != 0 &&
            reasonFromName(name) == reason &&
            sequenceFromName(name, sequence);
        FeatureRecordHeader header{};
        if (candidateEligible && readExact(file, &header, sizeof(header)) &&
            validHeader(header) && sequence < selectedSequence) {
            copyPath(selectedPath, candidatePath);
            selectedSequence = sequence;
            selectedBytes = file.size();
        }
        file.close();
        file = directory.openNextFile();
    }
    directory.close();

    if (selectedPath[0] == '\0' || !LittleFS.remove(selectedPath)) {
        return false;
    }
    Serial.print("[STORAGE] evicted priority=");
    Serial.print(reason == CandidateReason::periodic ? "periodic"
                 : reason == CandidateReason::anomalyActive
                     ? "anomaly_active"
                     : "other");
    Serial.print(" seq=");
    Serial.print(selectedSequence);
    Serial.print(" bytes=");
    Serial.print(selectedBytes);
    Serial.print(" path=");
    Serial.println(selectedPath);
    return true;
}

bool LittleFsWindowStore::evictOldestCompletedPair() {
    char selectedStartPath[80] = {};
    char selectedRecoveryPath[80] = {};
    uint32_t selectedStartSequence = 0;
    uint32_t selectedRecoverySequence = 0;
    size_t selectedStartBytes = 0;
    size_t selectedRecoveryBytes = 0;

    uint32_t afterSequence = 0;
    for (;;) {
        char startPath[80] = {};
        uint32_t startSequence = UINT32_MAX;
        size_t startBytes = 0;
        FeatureRecordHeader startHeader{};

        fs::File directory = LittleFS.open(kRetryDirectory, FILE_READ);
        if (!directory || !directory.isDirectory()) {
            return false;
        }
        fs::File file = directory.openNextFile();
        while (file) {
            const char* name = file.name();
            char candidatePath[80] = {};
            uint32_t sequence = 0;
            if (name != nullptr) {
                copyRetryPath(candidatePath, name);
            }
            FeatureRecordHeader header{};
            const bool eligible =
                name != nullptr && strstr(name, ".tmp") == nullptr &&
                strcmp(candidatePath, inFlightPath_) != 0 &&
                reasonFromName(name) == CandidateReason::anomalyStart &&
                sequenceFromName(name, sequence) && sequence > afterSequence &&
                sequence < startSequence && readExact(file, &header, sizeof(header)) &&
                validHeader(header);
            if (eligible) {
                copyPath(startPath, candidatePath);
                startSequence = sequence;
                startBytes = file.size();
                startHeader = header;
            }
            file.close();
            file = directory.openNextFile();
        }
        directory.close();

        if (startPath[0] == '\0') {
            return false;
        }

        uint32_t nextStartSequence = UINT32_MAX;
        directory = LittleFS.open(kRetryDirectory, FILE_READ);
        if (directory && directory.isDirectory()) {
            file = directory.openNextFile();
            while (file) {
                const char* name = file.name();
                uint32_t sequence = 0;
                FeatureRecordHeader header{};
                if (name != nullptr && strstr(name, ".tmp") == nullptr &&
                    reasonFromName(name) == CandidateReason::anomalyStart &&
                    sequenceFromName(name, sequence) &&
                    sequence > startSequence && sequence < nextStartSequence &&
                    readExact(file, &header, sizeof(header)) &&
                    validHeader(header)) {
                    nextStartSequence = sequence;
                }
                file.close();
                file = directory.openNextFile();
            }
            directory.close();
        }

        char recoveryPath[80] = {};
        uint32_t recoverySequence = UINT32_MAX;
        size_t recoveryBytes = 0;
        directory = LittleFS.open(kRetryDirectory, FILE_READ);
        if (directory && directory.isDirectory()) {
            file = directory.openNextFile();
            while (file) {
                const char* name = file.name();
                char candidatePath[80] = {};
                uint32_t sequence = 0;
                if (name != nullptr) {
                    copyRetryPath(candidatePath, name);
                }
                FeatureRecordHeader header{};
                if (name != nullptr && strstr(name, ".tmp") == nullptr &&
                    strcmp(candidatePath, inFlightPath_) != 0 &&
                    reasonFromName(name) == CandidateReason::recovery &&
                    sequenceFromName(name, sequence) &&
                    sequence > startSequence && sequence < nextStartSequence &&
                    sequence < recoverySequence &&
                    readExact(file, &header, sizeof(header)) &&
                    validHeader(header) &&
                    strncmp(header.metadata.bootId, startHeader.metadata.bootId,
                            sizeof(header.metadata.bootId)) == 0) {
                    copyPath(recoveryPath, candidatePath);
                    recoverySequence = sequence;
                    recoveryBytes = file.size();
                }
                file.close();
                file = directory.openNextFile();
            }
            directory.close();
        }

        if (recoveryPath[0] != '\0') {
            copyPath(selectedStartPath, startPath);
            copyPath(selectedRecoveryPath, recoveryPath);
            selectedStartSequence = startSequence;
            selectedRecoverySequence = recoverySequence;
            selectedStartBytes = startBytes;
            selectedRecoveryBytes = recoveryBytes;
            break;
        }
        afterSequence = startSequence;
    }

    if (!LittleFS.remove(selectedRecoveryPath)) {
        Serial.println("[STORAGE] event_pair_recovery_remove_failed");
        return false;
    }
    if (!LittleFS.remove(selectedStartPath)) {
        Serial.println("[STORAGE] event_pair_start_remove_failed");
        return false;
    }
    Serial.print("[STORAGE] evicted priority=completed_event_pair start_seq=");
    Serial.print(selectedStartSequence);
    Serial.print(" recovery_seq=");
    Serial.print(selectedRecoverySequence);
    Serial.print(" bytes=");
    Serial.print(selectedStartBytes + selectedRecoveryBytes);
    Serial.print(" path_start=");
    Serial.print(selectedStartPath);
    Serial.print(" path_recovery=");
    Serial.println(selectedRecoveryPath);
    return true;
}

WindowWriteResult LittleFsWindowStore::saveCandidate(
    const TransmissionCandidate& candidate) {
    WindowWriteResult result;
    const bool validCandidate = candidate.window.metadata.featuresValid != 0 &&
                                candidate.window.stats.featuresValid;
    const bool invalidPeriodic =
        candidate.reason == CandidateReason::periodic &&
        candidate.window.metadata.featuresValid == 0 &&
        strcmp(candidate.window.metadata.quality, "invalid") == 0;
    if ((!validCandidate && !invalidPeriodic) ||
        !candidate.window.metadata.windowMeasuredAtValid ||
        (candidate.reason == CandidateReason::periodic &&
         !candidate.window.metadata.periodicSlotEpochValid)) {
        return result;
    }

    lock();
    for (;;) {
        const size_t totalBytes = LittleFS.totalBytes();
        const size_t usedBytes = LittleFS.usedBytes();
        const size_t freeBytes =
            totalBytes >= usedBytes ? totalBytes - usedBytes : 0;
        if (freeBytes >= kRecordWriteBudgetBytes + kProtectedReserveBytes) {
            break;
        }
        if (!evictOldest(CandidateReason::periodic) &&
            !evictOldest(CandidateReason::anomalyActive) &&
            !evictOldestCompletedPair()) {
            Serial.println("[STORAGE] storage_full_critical reason=no_evictable");
            result.fsUsedBytes = usedBytes;
            result.fsFreeBytes = freeBytes;
            unlock();
            return result;
        }
    }

    char path[80] = {};
    char temporaryPath[80] = {};
    char bootKey[9] = {};
    const char prefix = candidate.reason == CandidateReason::anomalyStart
                            ? 'S'
                            : candidate.reason == CandidateReason::anomalyActive
                                ? 'A'
                                : candidate.reason == CandidateReason::recovery
                                    ? 'R'
                                    : 'P';
    copyBootKey(bootKey, candidate.window.metadata.bootId);
    const uint32_t sequence = nextSequence_++;
    snprintf(path, sizeof(path), "%s/%08lu_%c_%s_%lu.bin", kRetryDirectory,
             static_cast<unsigned long>(sequence), prefix, bootKey,
             static_cast<unsigned long>(candidate.window.metadata.windowIndex));
    snprintf(temporaryPath, sizeof(temporaryPath), "%s.tmp", path);

    if (LittleFS.exists(temporaryPath)) {
        LittleFS.remove(temporaryPath);
    }
    fs::File file = LittleFS.open(temporaryPath, FILE_WRITE);
    if (!file) {
        unlock();
        return result;
    }

    const uint32_t startedAt = micros();
    result.ok = writeRecord(file, candidate.window, result.bytes);
    file.flush();
    file.close();
    result.writeUs = micros() - startedAt;
    if (result.ok) {
        result.ok = LittleFS.rename(temporaryPath, path);
    }
    if (!result.ok) {
        LittleFS.remove(temporaryPath);
    }
    if (result.ok) {
        copyPath(result.path, path);
    }

    result.fsUsedBytes = LittleFS.usedBytes();
    const size_t totalBytes = LittleFS.totalBytes();
    result.fsFreeBytes = totalBytes >= result.fsUsedBytes
                             ? totalBytes - result.fsUsedBytes
                             : 0;
    unlock();
    return result;
}

bool LittleFsWindowStore::claimNextRetryFile(RetryFileRef& result) {
    result = RetryFileRef{};
    lock();

    fs::File directory = LittleFS.open(kRetryDirectory, FILE_READ);
    if (!directory || !directory.isDirectory()) {
        unlock();
        return false;
    }

    char selectedPath[80] = {};
    uint32_t selectedSequence = 0;
    uint32_t selectedLegacyIndex = UINT32_MAX;
    bool selectedHasSequence = false;
    fs::File file = directory.openNextFile();
    while (file) {
        const char* name = file.name();
        char candidatePath[80] = {};
        if (name != nullptr) {
            copyRetryPath(candidatePath, name);
        }
        if (name != nullptr && strstr(name, ".tmp") == nullptr &&
            strcmp(candidatePath, inFlightPath_) != 0) {
            FeatureRecordHeader header{};
            uint32_t sequence = 0;
            const bool hasSequence = sequenceFromName(name, sequence);
            if (readExact(file, &header, sizeof(header)) &&
                validHeader(header)) {
                const bool earlierFile =
                    selectedPath[0] == '\0' ||
                    (hasSequence
                         ? (selectedHasSequence && sequence < selectedSequence)
                         : (!selectedHasSequence ||
                            header.metadata.windowIndex < selectedLegacyIndex));
                if (earlierFile) {
                    copyRetryPath(selectedPath, name);
                    selectedSequence = sequence;
                    selectedLegacyIndex = header.metadata.windowIndex;
                    selectedHasSequence = hasSequence;
                }
            }
        }
        file.close();
        file = directory.openNextFile();
    }
    directory.close();

    if (selectedPath[0] == '\0') {
        unlock();
        return false;
    }
    copyPath(result.path, selectedPath);
    result.reason = reasonFromName(selectedPath);
    result.sequence = selectedHasSequence ? selectedSequence : 0;
    copyPath(inFlightPath_, result.path);
    unlock();
    return true;
}

bool LittleFsWindowStore::readCandidate(const RetryFileRef& fileRef,
                                         PendingWindow& result) {
    result = PendingWindow{};
    lock();
    fs::File file = LittleFS.open(fileRef.path, FILE_READ);
    if (!file) {
        unlock();
        return false;
    }

    FeatureRecordHeader header{};
    bool ok = readExact(file, &header, sizeof(header)) && validHeader(header) &&
              file.size() == header.recordBytes;
    if (ok) {
        result.metadata = header.metadata;
        result.stats.sampleCount = header.metadata.sampleCount;
        result.stats.featuresValid = header.metadata.featuresValid != 0;
        if (header.featureBytes > 0 &&
            !readExact(file, &result.stats.features, header.featureBytes)) {
            ok = false;
        }

        uint32_t storedCrc = 0;
        uint32_t crc = crc32Update(
            0xFFFFFFFFU, reinterpret_cast<const uint8_t*>(&header),
            sizeof(header));
        if (ok && header.featureBytes > 0) {
            crc = crc32Update(
                crc, reinterpret_cast<const uint8_t*>(&result.stats.features),
                header.featureBytes);
        }
        crc = ~crc;
        if (ok && (!readExact(file, &storedCrc, sizeof(storedCrc)) ||
                   storedCrc != crc)) {
            ok = false;
        }
    }
    file.close();
    unlock();
    return ok;
}

bool LittleFsWindowStore::removeCandidate(const RetryFileRef& file) {
    lock();
    const bool removed = LittleFS.remove(file.path);
    if (strcmp(inFlightPath_, file.path) == 0) {
        inFlightPath_[0] = '\0';
    }
    unlock();
    return removed;
}

void LittleFsWindowStore::releaseCandidate(const RetryFileRef& file) {
    lock();
    if (strcmp(inFlightPath_, file.path) == 0) {
        inFlightPath_[0] = '\0';
    }
    unlock();
}
