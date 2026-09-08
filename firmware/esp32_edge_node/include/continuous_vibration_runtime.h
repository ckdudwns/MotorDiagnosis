#pragma once
// Included by main.cpp after sensor helpers: one SPI owner, separate compute
// and network tasks. No LittleFS, NVS, HTTPS or feature FFT in the FIFO reader.
#include "vibration_window.h"
#include "continuous_vibration_health.h"
#include <mbedtls/base64.h>
#include <esp_heap_caps.h>

#ifndef RAW_VIBRATION_ENABLED
#define RAW_VIBRATION_ENABLED 0
#endif

#ifndef CONTINUOUS_VIBRATION_ENABLED
#define CONTINUOUS_VIBRATION_ENABLED 0
#endif

#if CONTINUOUS_VIBRATION_ENABLED
bool beginBackendHttp(BackendHttp&, WiFiClientSecure&, const char*);
namespace ContinuousVibration {
using namespace VibrationWindow;
#if RAW_VIBRATION_ENABLED
constexpr unsigned PendingCapacity = 8; // PSRAM-backed processing keeps capture from stalling.
// Keep one raw window per HTTPS request on the N8 internal heap.
constexpr unsigned BatchCapacity = 2;
using PendingWindow = Raw;
constexpr unsigned BatchIntervalMs = 400;
#else
constexpr unsigned PendingCapacity = 64; // 40.96 s, internal RAM only.
constexpr unsigned BatchCapacity = 8;
using PendingWindow = Features;
constexpr unsigned BatchIntervalMs = 1500;
#endif
Queue<PendingWindow, PendingCapacity> pending;
// Static network-task-owned buffers: do not place raw batches on its stack.
PendingWindow* batch = nullptr;
#if RAW_VIBRATION_ENABLED
constexpr size_t RawBytesCapacity = Samples*3*2;
constexpr size_t EncodedRawCapacity = RawBytesCapacity*4/3+1;
unsigned char* rawBytes = nullptr;
unsigned char* encodedRaw = nullptr;
bool encodeRaw(const Raw& raw, JsonObject w) {
    if (!rawBytes || !encodedRaw || !serializeCountsLE(raw,rawBytes,RawBytesCapacity)) return false;
    size_t length=0;
    if (mbedtls_base64_encode(encodedRaw,EncodedRawCapacity,&length,rawBytes,raw.count*6)) return false;
    encodedRaw[length]=0;
    // Own each string; subsequent windows reuse encodedRaw.
    w["samples"] = String(reinterpret_cast<const char*>(encodedRaw));
    return true;
}
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
char boot[33];
CaptureHealth captureHealth;

void reportCaptureHealth(CaptureHealth::State state) {
    captureHealth.record(state, healthUptimeMs(),
        [](DeviceHealth::Fault fault, bool active, std::uint64_t observedMs) {
            const SensorObservation observation{fault, active, observedMs};
            // Transitions only, not every sample/window. Backpressure pauses
            // acquisition rather than silently losing a fault/recovery. FIFO
            // overflow after a pause is reported as an invalid window.
            return xQueueSend(sensorObservations, &observation, portMAX_DELAY) == pdTRUE;
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
    // Audio validity is independent from vibration quality. A partial
    // vibration capture may still have a valid, silent acoustic window.
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
    // Backpressure instead of silently dropping a raw window when processing
    // is behind; the sample index remains continuous and the queue is bounded.
    const bool queued = xQueueSend(rawQueue, &capturing, portMAX_DELAY) == pdTRUE;
    if (queued) {
        // Ownership of the copied audio buffer moves to the queue item.
        capturing.audioWindow = nullptr;
    } else if (capturing.audioWindow) {
        heap_caps_free(capturing.audioWindow);
        capturing.audioWindow = nullptr;
    }
    ++capturing.index; // Includes invalid/dropped windows, never renumber.
    capturing.count = 0;
    capturing.quality = Quality::Valid;
}
void captureTask(void*) {
    std::uint64_t lastData = esp_timer_get_time();
    while (true) {
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
        const unsigned entries = adxlRead(0x39) & 0x3f;
        const bool disconnected = adxlRead(REG_DEVID) != 0xE5 || entries > 32;
        if (entries >= 32 || disconnected) {
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
        vTaskDelay(1);
    }
}
void processingTask(void*) {
    Raw raw;
    while (true) {
        if (xQueueReceive(rawQueue, &raw, portMAX_DELAY) != pdTRUE) continue;
        auto features = extract(raw, *workspace);
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
        xSemaphoreTake(mutex, portMAX_DELAY);
#if RAW_VIBRATION_ENABLED
        while (pending.size() == PendingCapacity) {
            xSemaphoreGive(mutex);
            vTaskDelay(pdMS_TO_TICKS(50));
            xSemaphoreTake(mutex, portMAX_DELAY);
        }
        raw.quality = features.quality;
        pending.push(raw);
#else
        pending.push(features);
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
void networkTask(void*) {
    // Frozen bytes/IDs remain identical across timeouts, auth errors and ACK loss.
    String body;
    unsigned size = 0;
    std::int64_t epochOffset = 0;
    std::uint32_t lastBatch = millis();
    std::uint32_t retryDelayMs = 2000;
    std::uint64_t reportedDrops = 0;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(200));
        xSemaphoreTake(mutex, portMAX_DELAY);
        const auto dropped = pending.dropped + processingDrops.load();
        xSemaphoreGive(mutex);
        if (dropped != reportedDrops) {
            Serial.printf("[WINDOW] Dropped windows=%llu (queue overflow; sequence gaps retained)\n", dropped);
            reportedDrops = dropped;
        }
        if (WiFi.status() != WL_CONNECTED) continue;
        struct timeval tv;
        gettimeofday(&tv, nullptr);
        if (tv.tv_sec < 1700000000) continue; // Never invent acquisition UTC.
        // One UTC anchor per boot: replays and buffered windows retain the same
        // mapping even if NTP adjusts the wall clock between batches.
        if (!epochOffset) epochOffset=static_cast<std::int64_t>(tv.tv_sec)*1000000 + tv.tv_usec - esp_timer_get_time();
        if (!size) {
            if (millis() - lastBatch < BatchIntervalMs) continue;
            xSemaphoreTake(mutex, portMAX_DELAY);
            size = std::min(pending.size(), BatchCapacity);
            for (unsigned i=0; i<size; ++i) batch[i] = pending.at(i);
            xSemaphoreGive(mutex);
            if (!size) continue;
            JsonDocument doc;
            auto windows = doc["windows"].to<JsonArray>();
            for (unsigned i=0; i<size; ++i) {
                const auto& f = batch[i];
                auto w = windows.add<JsonObject>();
                w["schemaVersion"]=1; w["deviceId"]=DEVICE_ID;
                w["siteId"]=SITE_ID; w["assetId"]=ASSET_ID; w["bootId"]=boot;
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
            if (!size || doc.overflowed()) {size=0; continue;}
            body="";
            serializeJson(doc, body);
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
        WiFiClientSecure secure;
        BackendHttp http;
        const auto cleanupHttp = [&]() {
            http.end();
            secure.stop();
        };
        secure.setHandshakeTimeout(3); http.setConnectTimeout(1500); http.setTimeout(1500);
        http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
        if (!beginBackendHttp(http, secure, url.c_str())) {
            cleanupHttp();
            vTaskDelay(pdMS_TO_TICKS(retryDelayMs));
            retryDelayMs = std::min<std::uint32_t>(60000, retryDelayMs * 2);
            continue;
        }
        http.addHeader("Authorization", String("Bearer ")+INGEST_TOKEN);
        http.addHeader("Content-Type", "application/json");
        const int status=http.POST(body);
        bool accepted=false;
        if ((status==200 || status==202) && http.getSize()>=0 && http.getSize()<8192) {
            JsonDocument response;
            if (!deserializeJson(response,http.getString(),DeserializationOption::NestingLimit(5))) {
                auto ack=response["acknowledged"].as<JsonArray>();
                accepted=response["deviceId"]==DEVICE_ID && ack.size()==size;
                for (unsigned i=0; accepted && i<size; ++i)
                    accepted=ack[i]["bootId"]==boot && ack[i]["windowIndex"].is<std::uint32_t>() &&
                             ack[i]["windowIndex"].as<std::uint32_t>()==batch[i].index;
            }
        }
        cleanupHttp();
        if (accepted) {
            xSemaphoreTake(mutex, portMAX_DELAY);
            pending.acknowledge(size);
            xSemaphoreGive(mutex);
            Serial.printf("[WINDOW] ACK %u windows through index %lu\n",size,static_cast<unsigned long>(batch[size-1].index));
            size=0; body=""; lastBatch=millis();
            retryDelayMs = 2000;
        } else {
            Serial.printf("[WINDOW] HTTP %d; unchanged batch retained\n",status);
            vTaskDelay(pdMS_TO_TICKS(retryDelayMs));
            retryDelayMs = std::min<std::uint32_t>(60000, retryDelayMs * 2);
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
    rawQueue=xQueueCreate(1,sizeof(Raw));
    const auto alloc = [](size_t bytes) {
        void* value = heap_caps_malloc(bytes, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
        return value ? value : heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    };
    workspace = static_cast<Workspace*>(alloc(sizeof(Workspace)));
    batch = static_cast<PendingWindow*>(alloc(sizeof(PendingWindow) * BatchCapacity));
#if RAW_VIBRATION_ENABLED
    rawBytes = static_cast<unsigned char*>(alloc(RawBytesCapacity));
    encodedRaw = static_cast<unsigned char*>(alloc(EncodedRawCapacity));
#endif
    if (!mutex || !rawQueue || !workspace || !batch
#if RAW_VIBRATION_ENABLED
        || !rawBytes || !encodedRaw
#endif
    ) return false;
    return xTaskCreatePinnedToCore(processingTask,"WindowFeatures",8192,nullptr,2,nullptr,0)==pdPASS &&
           xTaskCreatePinnedToCore(captureTask,"VibrationFIFO",4096,nullptr,4,nullptr,0)==pdPASS &&
           xTaskCreatePinnedToCore(networkTask,"WindowHTTPS",12288,nullptr,1,nullptr,1)==pdPASS;
}
}
#endif
