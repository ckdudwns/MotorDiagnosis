#pragma once
// Included by main.cpp after sensor helpers: one SPI owner, separate compute
// and network tasks. No LittleFS, NVS, HTTPS or feature FFT in the FIFO reader.
#include "vibration_window.h"
#include "continuous_vibration_health.h"
#include <mbedtls/base64.h>

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
constexpr unsigned PendingCapacity = 8; // 5.12 s, internal RAM only; not the feature queue.
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
PendingWindow batch[BatchCapacity];
#if RAW_VIBRATION_ENABLED
unsigned char rawBytes[Samples*3*2];
unsigned char encodedRaw[Samples*3*2*4/3+1];
bool encodeRaw(const Raw& raw, JsonObject w) {
    if (!serializeCountsLE(raw,rawBytes,sizeof(rawBytes))) return false;
    size_t length=0;
    if (mbedtls_base64_encode(encodedRaw,sizeof(encodedRaw),&length,rawBytes,raw.count*6)) return false;
    encodedRaw[length]=0;
    // Own each string; subsequent windows reuse encodedRaw.
    w["samples"] = String(reinterpret_cast<const char*>(encodedRaw));
    return true;
}
#endif
Workspace workspace;
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
    if (xQueueSend(rawQueue, &capturing, 0) != pdTRUE) ++processingDrops;
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
        auto features = extract(raw, workspace);
        // Copy/analyze audio immediately after acquisition, not after HTTP.
        // The shorter ring covers 640ms plus 128ms scheduling margin.
        const std::uint32_t waitStart = millis();
        while (getAudioTotalSamples() < raw.audioStart + COMMON_AUDIO_SAMPLES &&
               millis() - waitStart < 20) vTaskDelay(1);
        bool audioValid = raw.quality==Quality::Valid && audioReady.load() &&
            raw.audioGeneration==audioErrorGeneration.load() &&
            copyAudioWindow(raw.audioStart,COMMON_AUDIO_SAMPLES,commonAudioWindow) &&
            DeviceHealth::hasPcmVariation(commonAudioWindow,COMMON_AUDIO_SAMPLES);
        AcousticFeatures acoustic;
        if (audioValid) acoustic=analyzeCommonAudioWindow(commonAudioWindow,COMMON_AUDIO_SAMPLES);
        audioValid = audioValid && raw.audioGeneration==audioErrorGeneration.load();
        xSemaphoreTake(mutex, portMAX_DELAY);
#if RAW_VIBRATION_ENABLED
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
        secure.setHandshakeTimeout(3); http.setConnectTimeout(1500); http.setTimeout(1500);
        http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
        if (!beginBackendHttp(http, secure, url.c_str())) continue;
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
        http.end();
        if (accepted) {
            xSemaphoreTake(mutex, portMAX_DELAY);
            pending.acknowledge(size);
            xSemaphoreGive(mutex);
            Serial.printf("[WINDOW] ACK %u windows through index %lu\n",size,static_cast<unsigned long>(batch[size-1].index));
            size=0; body=""; lastBatch=millis();
        } else {
            Serial.printf("[WINDOW] HTTP %d; unchanged batch retained\n",status);
            vTaskDelay(pdMS_TO_TICKS(2000));
        }
    }
}
bool snapshot(VibrationFeatures& vib, AcousticFeatures& audio) {
    Features f;
    std::uint32_t generation;
    xSemaphoreTake(mutex, portMAX_DELAY);
    const bool available=hasLatest && latestAudioValid;
    f=latest; generation=latestAudioGeneration;
    audio=latestAudio;
    xSemaphoreGive(mutex);
    if (!available || f.quality!=Quality::Valid || !audioReady.load() ||
        generation!=audioErrorGeneration.load() ||
        static_cast<std::uint64_t>(esp_timer_get_time())-f.startUs>1500000) return false;
    vib.rmsX=f.values[0]; vib.rmsY=f.values[7]; vib.rmsZ=f.values[14];
    vib.totalRms=std::sqrt(vib.rmsX*vib.rmsX+vib.rmsY*vib.rmsY+vib.rmsZ*vib.rmsZ);
    unsigned axis=vib.rmsY>vib.rmsX?1:0;
    if (vib.rmsZ>f.values[axis*7]) axis=2;
    vib.fftAxis="XYZ"[axis]; vib.peakHz=f.peakHz[axis];
    return generation==audioErrorGeneration.load();
}
bool start() {
    snprintf(boot,sizeof(boot),"%08lx%08lx%08lx%08lx",
             static_cast<unsigned long>(esp_random()),static_cast<unsigned long>(esp_random()),
             static_cast<unsigned long>(esp_random()),static_cast<unsigned long>(esp_random()));
    mutex=xSemaphoreCreateMutex();
    rawQueue=xQueueCreate(3,sizeof(Raw));
    if (!mutex || !rawQueue) return false;
    return xTaskCreatePinnedToCore(processingTask,"WindowFeatures",8192,nullptr,2,nullptr,0)==pdPASS &&
           xTaskCreatePinnedToCore(captureTask,"VibrationFIFO",4096,nullptr,4,nullptr,0)==pdPASS &&
           xTaskCreatePinnedToCore(networkTask,"WindowHTTPS",12288,nullptr,1,nullptr,1)==pdPASS;
}
}
#endif
