/*
 * MotorDiagnosis Edge Node
 *
 * Firmware Version : v1.3-signal-analysis.1
 * Revision Summary :
 *   P1 Atomic Queue Recovery
 *   P2 Unlabeled Real Telemetry Contract
 *   P3 API v1.3 Error-Code-Aware HTTP Classification / Rejected Packet Isolation
 *   P4 Non-Blocking Wi-Fi Recovery
 *   P5 Synchronized Vibration + Acoustic Acquisition Window
 *
 * Hardware:
 * - ESP32-S3-WROOM-1
 * - ADXL345 (SPI)
 * - INMP441 (I2S)
 *
 * IMPORTANT:
 * - P2 is now applied for ordinary ESP32 real telemetry:
 *   scenarioLabel = null
 *   knownVibrationLabel = null
 *   knownAcousticLabel = null
 * - The ESP32 does not infer or assert ground-truth labels.
 *   Verified labels are assigned only by trusted external workflows.
 * - P3 is aligned with API specification v1.3:
 *   TELEMETRY_LABEL_FORBIDDEN and SEQUENCE_CONFLICT are packet-permanent;
 *   auth/device/mapping/configuration errors preserve the queue.
 * - beta.11 replaces one-JSON-file-per-packet buffering with a
 *   CRC-protected fixed-record binary ring sized for at least 24 h at
 *   the current cadence. When full, the oldest record is dropped while
 *   acquisition continues.
 * - The physical ring stores 25,000 x 48-byte slots (~1.20 MB).
 *   One slot is kept spare, so 24,999 records are logically active; this
 *   still exceeds the 24-hour retention target.
 * - Full-ring append is write-first: the new record is verified in the
 *   spare slot before the oldest record is logically consumed.
 *
 * Unit policy:
 * - vibrationRmsRaw = ADXL345 acceleration RMS [g]
 * - vibrationRmsMmS = null
 * - acousticRmsRaw  = INMP441 raw PCM RMS
 * - acousticDb      = null
 */

#include <Arduino.h>
#include <SPI.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <WiFiClientSecure.h>
#include <Preferences.h>
#include <LittleFS.h>
#include "edge_analysis.h"
#include <ArduinoJson.h>
#include <arduinoFFT.h>
#include "driver/i2s.h"
#include "freertos/queue.h"
#include "esp_timer.h"

#include <time.h>
#include <sys/time.h>
#include <math.h>
#include <stddef.h>
#include <algorithm>
#include <cstring>
#include <atomic>

#include "firmware_logic.h"
#include "device_health.h"
#include "remote_config.h"
#include "communication_quality.h"
#include "backend_http.h"
SemaphoreHandle_t BackendHttp::gate = nullptr;

#ifndef CONTINUOUS_VIBRATION_ENABLED
#define CONTINUOUS_VIBRATION_ENABLED 0
#endif

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif

// =====================================================
// Firmware
// =====================================================

constexpr char FIRMWARE_VERSION[] =
    "v1.3-signal-analysis.1";

// =====================================================
// Test Config
// =====================================================

// false = normal operation
// true  = test backend-time fallback by skipping NTP
constexpr bool TEST_FORCE_NTP_FAIL =
    false;

// false = production behavior
// true  = simulate an NVS sequence write failure before a new sequence is
//         allowed to become a packet.
constexpr bool TEST_FORCE_SEQUENCE_NVS_FAIL =
    false;

// false = production behavior
// true  = simulate repeated LittleFS mount failure. The firmware must stop
//         without calling format; after setting this back to false, the
//         previous backlog should still recover.
constexpr bool TEST_FORCE_LITTLEFS_MOUNT_FAIL =
    false;

// Runtime fault-injection hooks for regression testing. Leave false in
// production. These never classify an I/O failure as acknowledged data.
constexpr bool TEST_FORCE_RING_READ_IO_FAIL =
    false;

constexpr bool TEST_FORCE_RING_WRITE_FAIL =
    false;

constexpr bool TEST_FORCE_ISOLATION_WRITE_FAIL =
    false;

// =====================================================
// Wi-Fi / Backend Secrets
// =====================================================

const char* WIFI_SSID =
    WIFI_SSID_VALUE;

const char* WIFI_PASSWORD =
    WIFI_PASSWORD_VALUE;

const char* INGEST_URL =
    INGEST_URL_VALUE;

const char* HEALTH_URL =
    HEALTH_URL_VALUE;

const char* INGEST_TOKEN =
    INGEST_TOKEN_VALUE;

// Existing ignored secrets.h files still compile; reporting stays disabled
// until a separate limited-scope credential and matching device URL are set.
#ifndef DEVICE_HEALTH_URL_VALUE
#define DEVICE_HEALTH_URL_VALUE HEALTH_URL_VALUE
#endif
#ifndef DEVICE_HEALTH_TOKEN_VALUE
#define DEVICE_HEALTH_TOKEN_VALUE ""
#endif
const char* DEVICE_HEALTH_URL = DEVICE_HEALTH_URL_VALUE;
const char* DEVICE_HEALTH_TOKEN = DEVICE_HEALTH_TOKEN_VALUE;

#ifndef DEVICE_CONFIG_URL_VALUE
#define DEVICE_CONFIG_URL_VALUE ""
#endif
#ifndef DEVICE_CONFIG_TOKEN_VALUE
#define DEVICE_CONFIG_TOKEN_VALUE ""
#endif
const char* DEVICE_CONFIG_URL = DEVICE_CONFIG_URL_VALUE;
const char* DEVICE_CONFIG_TOKEN = DEVICE_CONFIG_TOKEN_VALUE;

#ifndef DEVICE_QUALITY_URL_VALUE
#define DEVICE_QUALITY_URL_VALUE ""
#endif
#ifndef DEVICE_QUALITY_TOKEN_VALUE
#define DEVICE_QUALITY_TOKEN_VALUE ""
#endif
const char* DEVICE_QUALITY_URL = DEVICE_QUALITY_URL_VALUE;
const char* DEVICE_QUALITY_TOKEN = DEVICE_QUALITY_TOKEN_VALUE;

#ifndef BACKEND_CA_CERT_VALUE
#define BACKEND_CA_CERT_VALUE ""
#endif

#ifndef ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE
#define ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE false
#endif

const char* BACKEND_CA_CERT =
    BACKEND_CA_CERT_VALUE;

constexpr bool ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV =
    ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE;

// =====================================================
// Device
// =====================================================

const char* SITE_ID =
    "SITE-01";

const char* ASSET_ID =
    "SITE-01-MOT-02";

const char* DEVICE_ID =
    "DEV-01-MOT-02";

// =====================================================
// ADXL345 Pins
// =====================================================

constexpr int ADXL_CS   = 10;
constexpr int ADXL_MOSI = 11;
constexpr int ADXL_SCK  = 12;
constexpr int ADXL_MISO = 13;

// =====================================================
// INMP441 Pins
// =====================================================

constexpr int MIC_SCK = 4;
constexpr int MIC_WS  = 5;
constexpr int MIC_SD  = 6;

constexpr i2s_port_t I2S_PORT =
    I2S_NUM_0;

// =====================================================
// ADXL345 Registers
// =====================================================

constexpr uint8_t REG_DEVID       = 0x00;
constexpr uint8_t REG_BW_RATE     = 0x2C;
constexpr uint8_t REG_POWER_CTL   = 0x2D;
constexpr uint8_t REG_DATA_FORMAT = 0x31;
constexpr uint8_t REG_DATAX0      = 0x32;

// =====================================================
// Sampling
// =====================================================

// Vibration common window:
// 512 / 800 Hz = 0.64 s
constexpr uint16_t VIB_SAMPLES =
    512;

constexpr double VIB_SAMPLE_RATE =
    800.0;

constexpr uint32_t VIB_PERIOD_US =
    1250;

constexpr double G_PER_LSB =
    0.0039;

// Acoustic stream:
// 16 kHz continuous capture.
//
// P5:
// 0.64 s * 16 kHz = 10,240 samples.
// These 10,240 acoustic samples correspond to the same logical
// acquisition window used by the 512 vibration samples.
constexpr uint32_t AUDIO_SAMPLE_RATE =
    16000;

constexpr size_t COMMON_AUDIO_SAMPLES =
    10240;

// Continuous mode copies immediately in the feature task: 0.768 s suffices
// for 640 ms plus 128 ms margin. Legacy main-loop collection keeps 1.024 s.
constexpr size_t AUDIO_RING_CAPACITY =
    CONTINUOUS_VIBRATION_ENABLED ? 12288 : 16384;

// Continuous I2S reader consumes small blocks to keep DMA backlog low.
// 128 samples = 8 ms at 16 kHz.
constexpr size_t AUDIO_DMA_READ_SAMPLES =
    128;

// Acoustic FFT uses 2048-point blocks.
// Five blocks exactly cover 10,240 samples.
// Their magnitude spectra are averaged, so acousticPeakHz represents
// the entire common 0.64 s window rather than only one 0.128 s slice.
constexpr size_t AUDIO_FFT_SAMPLES =
    2048;

constexpr size_t AUDIO_FFT_BLOCKS =
    COMMON_AUDIO_SAMPLES /
    AUDIO_FFT_SAMPLES;

static_assert(
    AUDIO_FFT_BLOCKS * AUDIO_FFT_SAMPLES ==
        COMMON_AUDIO_SAMPLES,
    "Common audio window must be divisible by FFT block size."
);

static_assert(
    COMMON_AUDIO_SAMPLES <
        AUDIO_RING_CAPACITY,
    "Audio ring must be larger than common acquisition window."
);

// =====================================================
// Offline Buffer - 24H Binary Ring
// =====================================================
//
// The physical LittleFS ring remains exactly 25,000 x 48-byte slots
// (1.20 MB), so existing files do not require a destructive resize.
//
// One slot is intentionally kept spare. Therefore at most 24,999 records
// are logically active. When the queue is full, the new record is written
// and read-back verified into the spare slot BEFORE the oldest record is
// consumed. A failed write therefore cannot destroy both the old and new
// telemetry. The retention loss is only one ~4 s record and still exceeds
// the 24-hour target.
//

constexpr size_t RING_SLOT_COUNT =
    25000;

constexpr size_t QUEUE_CAPACITY =
    RING_SLOT_COUNT - 1;

static_assert(
    QUEUE_CAPACITY + 1 == RING_SLOT_COUNT,
    "One physical ring slot must remain spare."
);

constexpr char RING_FILE[] =
    "/telemetry_ring.bin";

constexpr char REJECTED_DIR[] =
    "/telemetry_rejected";

constexpr size_t MAX_REJECTED_ARCHIVE_FILES =
    128;

// Archive maintenance is deliberately bounded. A legacy archive can contain
// hundreds of .json/.meta files; deleting one file and rescanning the entire
// directory repeatedly can stall replay and fresh sensor acquisition.
constexpr size_t MAX_ISOLATION_EVICTIONS_PER_PASS =
    8;

constexpr size_t MAX_STALE_TEMP_REMOVALS_PER_PASS =
    4;

constexpr size_t MAX_ISOLATION_BACKEND_RESPONSE_CHARS =
    1024;

constexpr uint32_t RING_MAGIC =
    0x4D445231UL; // "MDR1"

constexpr uint16_t RING_LEGACY_SCHEMA_VERSION =
    1;

// v2 makes the session/monotonic unresolved-time encoding explicit.
// Recovery remains backward compatible with schema-v1 records:
// - v1 resolved records are replayed normally.
// - v1 unresolved records with epochSeconds=0 are recognized as the
//   pre-migration format and moved to isolation without fabricating UTC.
// - v1 unresolved records that already contain packed session metadata
//   (beta.11.8.x transition builds) remain readable.
constexpr uint16_t RING_SCHEMA_VERSION =
    2;

// Cold-boot timestamp handling:
//
// No RTC means absolute UTC before the first network time sync is unknown.
// Such records are persisted immediately. In schema v2, epochSeconds is
// repurposed while RING_FLAG_TIME_UNRESOLVED is set:
//
//   high 32 bits = durable boot session ID
//   low  32 bits = millis() at capture
//
// Once UTC becomes available in THE SAME boot session, a single NVS blob
// maps sessionId + millis() -> UTC. Actual observed monotonic deltas are
// used; no fixed 3.99 s timestamp is invented. Records from an older boot
// session cannot be timestamped reliably after power loss, so they are
// preserved in the bounded isolation archive instead of being transmitted
// with fabricated UTC.
constexpr uint16_t RING_FLAG_TIME_UNRESOLVED =
    0x0001U;

constexpr uint32_t TIME_ANCHOR_MAGIC =
    0x4D445441UL; // "MDTA"

constexpr uint16_t TIME_ANCHOR_VERSION =
    1;

constexpr size_t TIME_ANCHOR_SLOT_COUNT =
    32;

constexpr uint8_t LITTLEFS_MOUNT_ATTEMPTS =
    3;

// Fast replay watermark. ACK loss before a batch commit may cause only a
// bounded duplicate replay; the backend's sequence contract handles this
// idempotently.
constexpr size_t ACK_WATERMARK_BATCH_SIZE =
    32;

// Only the main task changes these after verified configuration persistence.
// Bounds preserve the existing queue capacity and maximum replay starvation.
size_t REPLAY_MAX_RECORDS_PER_LOOP = RemoteConfig::DEFAULT_REPLAY_BATCH;

uint32_t MEASUREMENT_INTERVAL_MS = RemoteConfig::DEFAULT_INTERVAL_MS;

constexpr uint32_t NETWORK_RETRY_MS =
    5000;

constexpr uint32_t WIFI_ATTEMPT_TIMEOUT_MS =
    10000;

// =====================================================
// SPI
// =====================================================

SPIClass adxlSPI(FSPI);

SPISettings adxlSettings(
    5000000,
    MSBFIRST,
    SPI_MODE3
);

// =====================================================
// Vibration Buffers
// =====================================================

double vibX[VIB_SAMPLES];
double vibY[VIB_SAMPLES];
double vibZ[VIB_SAMPLES];

double vibFFTReal[VIB_SAMPLES];
double vibFFTImag[VIB_SAMPLES];

// =====================================================
// Acoustic Continuous Ring + Analysis Buffers
// =====================================================

int32_t audioRing[
    AUDIO_RING_CAPACITY
];

int32_t commonAudioWindow[
    COMMON_AUDIO_SAMPLES
];

#if CONTINUOUS_VIBRATION_ENABLED
using AudioFftValue = float; // Audio is not a vibration-model input.
#else
using AudioFftValue = double;
#endif
AudioFftValue audioFFTReal[
    AUDIO_FFT_SAMPLES
];

AudioFftValue audioFFTImag[
    AUDIO_FFT_SAMPLES
];

AudioFftValue audioSpectrumAverage[
    AUDIO_FFT_SAMPLES / 2
];

// Monotonic count of samples committed to audioRing.
// It is NOT the ring index. The ring index is total % capacity.
uint64_t audioTotalSamples =
    0;

SemaphoreHandle_t audioRingMutex;

// =====================================================
// FFT Objects
// =====================================================

#if !CONTINUOUS_VIBRATION_ENABLED
ArduinoFFT<double> vibFFT(
    vibFFTReal,
    vibFFTImag,
    VIB_SAMPLES,
    VIB_SAMPLE_RATE
);
#endif

ArduinoFFT<AudioFftValue> audioFFT(
    audioFFTReal,
    audioFFTImag,
    AUDIO_FFT_SAMPLES,
    static_cast<double>(
        AUDIO_SAMPLE_RATE
    )
);

// =====================================================
// Feature Structures
// =====================================================

struct VibrationFeatures
{
    double rmsX = 0.0;
    double rmsY = 0.0;
    double rmsZ = 0.0;
    double totalRms = 0.0;
    double peakHz = 0.0;
    char fftAxis = 'X';
};

struct AcousticFeatures
{
    double rmsRaw = 0.0;
    double peakHz = 0.0;
    int32_t peakToPeak = 0;
};

struct TelemetryPacket
{
    uint32_t sequence = 0;

    uint64_t epochSeconds = 0;
    bool timestampResolved = false;

    // Used only while timestampResolved=false. These values are persisted
    // in the existing 64-bit epochSeconds field of BinaryTelemetryRecord.
    uint32_t offlineSessionId = 0;
    uint32_t captureMonotonicMs = 0;

    float vibrationRmsRaw = 0.0f;
    float vibrationPeakHz = 0.0f;
    float acousticRmsRaw = 0.0f;
    float acousticPeakHz = 0.0f;

    String payload;
};

using TimeAnchorBlob =
    FirmwareLogic::DurableTimeAnchorBlob;

struct __attribute__((packed)) BinaryTelemetryRecord
{
    uint32_t magic = RING_MAGIC;
    uint16_t schemaVersion = RING_SCHEMA_VERSION;
    uint16_t flags = 0;

    uint64_t ordinal = 0;
    uint32_t sequence = 0;
    uint64_t epochSeconds = 0;

    float vibrationRmsRaw = 0.0f;
    float vibrationPeakHz = 0.0f;
    float acousticRmsRaw = 0.0f;
    float acousticPeakHz = 0.0f;

    uint32_t crc32 = 0;
};

static_assert(
    sizeof(BinaryTelemetryRecord) == 48,
    "BinaryTelemetryRecord must remain 48 bytes."
);

// =====================================================
// HTTP Outcome
// =====================================================

enum class PostResult
{
    SUCCESS,
    RETRYABLE,
    PERMANENT_PACKET_REJECT,
    CONFIGURATION_ERROR
};

struct PostOutcome
{
    PostResult result =
        PostResult::RETRYABLE;

    int statusCode =
        -1;

    String response;
};

// =====================================================
// Persistent Binary Ring State
// =====================================================
//
// Ordinals are local storage positions, independent of telemetry sequence.
// This matters because online-success sequences are not written to the ring.
//

size_t queueCount =
    0;

uint64_t ringHeadOrdinal =
    0;

uint64_t ringNextOrdinal =
    0;

uint64_t droppedOldestCount =
    0;

CommunicationQuality::Collector qualityCollector;
CommunicationQuality::Schedule qualitySchedule;
CommunicationQuality::Outbox qualityOutbox;
const CommunicationQuality::Identity qualityIdentity{DEVICE_ID, SITE_ID, ASSET_ID};
bool qualityStorageUsable = true;

// Highest ring ordinal durably known to be consumed.
// Stored in Preferences key "ringConsumed".
uint64_t committedConsumedOrdinal =
    0;

// Highest consumed ordinal in RAM. This may be ahead of the durable
// watermark by at most ACK_WATERMARK_BATCH_SIZE - 1 during replay.
uint64_t pendingConsumedOrdinal =
    0;

size_t pendingConsumedCount =
    0;

// =====================================================
// Vibration Task State
// =====================================================

VibrationFeatures vibrationResult;

SemaphoreHandle_t vibrationResultMutex;

TaskHandle_t vibrationTaskHandle =
    nullptr;

TaskHandle_t audioCaptureTaskHandle =
    nullptr;

volatile bool vibrationFinished =
    false;

std::atomic<bool> vibrationBusy{false};
bool vibrationChannelValid = false; // protected by vibrationResultMutex
std::atomic<bool> audioReady{false};
std::atomic<bool> audioReinitializeRequested{false};
std::atomic<uint32_t> audioErrorGeneration{0};
struct SensorObservation {
    DeviceHealth::Fault fault;
    bool active;
    uint64_t observedMs;
};
QueueHandle_t sensorObservations = nullptr;
DeviceHealth::SensorRetry adxlRetry;
bool adxlInitFailed = false;
DeviceHealth::Journal healthJournal = DeviceHealth::emptyJournal();
DeviceHealth::ReportSchedule healthSchedule;
DeviceHealth::Metrics lastHealthMetrics;
bool healthJournalUsable = true;
bool healthJournalDirty = false;
bool healthObservationBlocked = false;

// =====================================================
// Persistent / Network State
// =====================================================

Preferences preferences;

uint32_t telemetrySequence =
    0;

// True only when the current sequence floor is known durable in NVS.
// A packet is never created from a sequence that failed persistence.
bool sequencePersistenceReady =
    false;

// Active binary-ring records captured before absolute UTC was available.
size_t unresolvedTimestampCount =
    0;

// Every boot receives a durable monotonically increasing session ID before
// sensing starts. Unresolved records are only resolved against an anchor
// carrying this exact session ID.
uint32_t bootSessionId =
    0;

bool bootSessionReady =
    false;

TimeAnchorBlob timeAnchors[
    TIME_ANCHOR_SLOT_COUNT
];

bool timeAnchorValid[
    TIME_ANCHOR_SLOT_COUNT
] = {};

bool timeReady =
    false;

uint32_t lastNetworkRetry =
    0;

bool wifiReconnectInProgress =
    false;

uint32_t wifiReconnectStartedAt =
    0;

bool wifiWasConnected =
    false;

// =====================================================
// Prototypes
// =====================================================

String getTimestamp();

bool syncTime();

bool syncTimeFromBackend();

uint64_t healthUptimeMs();
int64_t currentHealthEpochMs();
bool observeSensorFault(DeviceHealth::Fault fault, bool active, uint64_t observedMs);
void serviceDeviceHealth();
void serviceSensors();
bool initializeDeviceHealth();
void queueAudioFault(DeviceHealth::Fault fault, uint64_t observedMs);

// =====================================================
// ADXL345
// =====================================================

void adxlWrite(
    uint8_t reg,
    uint8_t value
)
{
    adxlSPI.beginTransaction(
        adxlSettings
    );

    digitalWrite(
        ADXL_CS,
        LOW
    );

    adxlSPI.transfer(reg);
    adxlSPI.transfer(value);

    digitalWrite(
        ADXL_CS,
        HIGH
    );

    adxlSPI.endTransaction();
}

uint8_t adxlRead(
    uint8_t reg
)
{
    adxlSPI.beginTransaction(
        adxlSettings
    );

    digitalWrite(
        ADXL_CS,
        LOW
    );

    adxlSPI.transfer(
        reg | 0x80
    );

    uint8_t value =
        adxlSPI.transfer(
            0x00
        );

    digitalWrite(
        ADXL_CS,
        HIGH
    );

    adxlSPI.endTransaction();

    return value;
}

void adxlReadXYZ(
    int16_t& x,
    int16_t& y,
    int16_t& z
)
{
    uint8_t data[6];

    adxlSPI.beginTransaction(
        adxlSettings
    );

    digitalWrite(
        ADXL_CS,
        LOW
    );

    adxlSPI.transfer(
        REG_DATAX0 |
        0x80 |
        0x40
    );

    for (
        int i = 0;
        i < 6;
        i++
    )
    {
        data[i] =
            adxlSPI.transfer(
                0x00
            );
    }

    digitalWrite(
        ADXL_CS,
        HIGH
    );

    adxlSPI.endTransaction();

    x =
        static_cast<int16_t>(
            (data[1] << 8) |
            data[0]
        );

    y =
        static_cast<int16_t>(
            (data[3] << 8) |
            data[2]
        );

    z =
        static_cast<int16_t>(
            (data[5] << 8) |
            data[4]
        );
}

bool initADXL345()
{
    uint8_t id =
        adxlRead(
            REG_DEVID
        );

    Serial.printf(
        "[ADXL345] Device ID : 0x%02X\n",
        id
    );

    if (
        id != 0xE5
    )
    {
        return false;
    }

    // Full resolution, +/-16 g.
    adxlWrite(
        REG_DATA_FORMAT,
        0x0B
    );

    // 800 Hz output data rate.
    adxlWrite(
        REG_BW_RATE,
        0x0D
    );

    // Measurement mode.
    adxlWrite(
        REG_POWER_CTL,
        0x08
    );

    // SPI has no ACK: verify identity and configuration read-back.
    return adxlRead(REG_DEVID) == 0xE5 &&
           (adxlRead(REG_DATA_FORMAT) & 0x0F) == 0x0B &&
           (adxlRead(REG_POWER_CTL) & 0x08) != 0 &&
           (adxlRead(REG_BW_RATE) & 0x0F) == 0x0D;
}

// =====================================================
// INMP441
// =====================================================

bool initINMP441()
{
    i2s_config_t config = {};

    config.mode =
        static_cast<i2s_mode_t>(
            I2S_MODE_MASTER |
            I2S_MODE_RX
        );

    config.sample_rate =
        AUDIO_SAMPLE_RATE;

    config.bits_per_sample =
        I2S_BITS_PER_SAMPLE_32BIT;

    config.channel_format =
        I2S_CHANNEL_FMT_ONLY_LEFT;

    config.communication_format =
        I2S_COMM_FORMAT_STAND_I2S;

    config.intr_alloc_flags =
        ESP_INTR_FLAG_LEVEL1;

    config.dma_buf_count =
        8;

    // Smaller DMA block lowers the age of unread audio data.
    config.dma_buf_len =
        AUDIO_DMA_READ_SAMPLES;

    config.use_apll =
        false;

    config.tx_desc_auto_clear =
        false;

    config.fixed_mclk =
        0;

    i2s_pin_config_t pins = {};

    pins.bck_io_num =
        MIC_SCK;

    pins.ws_io_num =
        MIC_WS;

    pins.data_out_num =
        I2S_PIN_NO_CHANGE;

    pins.data_in_num =
        MIC_SD;

    esp_err_t result =
        i2s_driver_install(
            I2S_PORT,
            &config,
            0,
            nullptr
        );

    if (
        result != ESP_OK
    )
    {
        Serial.printf(
            "[I2S] Driver install failed: %d\n",
            result
        );

        return false;
    }

    result =
        i2s_set_pin(
            I2S_PORT,
            &pins
        );

    if (
        result != ESP_OK
    )
    {
        Serial.printf(
            "[I2S] Pin setup failed: %d\n",
            result
        );

        i2s_driver_uninstall(I2S_PORT);

        return false;
    }

    // Clear stale DMA data once before continuous capture begins.
    i2s_zero_dma_buffer(
        I2S_PORT
    );

    return true;
}

// =====================================================
// Math
// =====================================================

double calculateMean(
    const double* samples,
    uint16_t count
)
{
    double sum =
        0.0;

    for (
        uint16_t i = 0;
        i < count;
        i++
    )
    {
        sum +=
            samples[i];
    }

    return (
        sum /
        count
    );
}

double calculateRms(
    const double* samples,
    uint16_t count,
    double mean
)
{
    double sumSquares =
        0.0;

    for (
        uint16_t i = 0;
        i < count;
        i++
    )
    {
        double value =
            samples[i] -
            mean;

        sumSquares +=
            value *
            value;
    }

    return sqrt(
        sumSquares /
        count
    );
}

// =====================================================
// Vibration Acquisition
// =====================================================

VibrationFeatures acquireVibration()
{
#if CONTINUOUS_VIBRATION_ENABLED
    return VibrationFeatures();
#else
    VibrationFeatures result;

    uint32_t nextSample =
        micros();

    for (
        uint16_t i = 0;
        i < VIB_SAMPLES;
        i++
    )
    {
        while (
            static_cast<int32_t>(
                micros() -
                nextSample
            ) < 0
        )
        {
            taskYIELD();
        }

        nextSample +=
            VIB_PERIOD_US;

        int16_t x;
        int16_t y;
        int16_t z;

        adxlReadXYZ(
            x,
            y,
            z
        );

        vibX[i] =
            x * G_PER_LSB;

        vibY[i] =
            y * G_PER_LSB;

        vibZ[i] =
            z * G_PER_LSB;
    }

    double meanX =
        calculateMean(
            vibX,
            VIB_SAMPLES
        );

    double meanY =
        calculateMean(
            vibY,
            VIB_SAMPLES
        );

    double meanZ =
        calculateMean(
            vibZ,
            VIB_SAMPLES
        );

    result.rmsX =
        calculateRms(
            vibX,
            VIB_SAMPLES,
            meanX
        );

    result.rmsY =
        calculateRms(
            vibY,
            VIB_SAMPLES,
            meanY
        );

    result.rmsZ =
        calculateRms(
            vibZ,
            VIB_SAMPLES,
            meanZ
        );

    result.totalRms =
        sqrt(
            result.rmsX * result.rmsX +
            result.rmsY * result.rmsY +
            result.rmsZ * result.rmsZ
        );

    const double* fftSource =
        vibX;

    double fftMean =
        meanX;

    result.fftAxis =
        'X';

    if (
        result.rmsY >
            result.rmsX &&
        result.rmsY >=
            result.rmsZ
    )
    {
        fftSource =
            vibY;

        fftMean =
            meanY;

        result.fftAxis =
            'Y';
    }
    else if (
        result.rmsZ >
            result.rmsX &&
        result.rmsZ >
            result.rmsY
    )
    {
        fftSource =
            vibZ;

        fftMean =
            meanZ;

        result.fftAxis =
            'Z';
    }

    for (
        uint16_t i = 0;
        i < VIB_SAMPLES;
        i++
    )
    {
        vibFFTReal[i] =
            fftSource[i] -
            fftMean;

        vibFFTImag[i] =
            0.0;
    }

    vibFFT.windowing(
        FFTWindow::Hamming,
        FFTDirection::Forward
    );

    vibFFT.compute(
        FFTDirection::Forward
    );

    vibFFT.complexToMagnitude();

    result.peakHz =
        vibFFT.majorPeak();

    return result;
#endif
}

// =====================================================
// P5: Continuous Acoustic Capture
// =====================================================

void appendAudioSamplesToRing(
    const int32_t* samples,
    size_t count
)
{
    xSemaphoreTake(
        audioRingMutex,
        portMAX_DELAY
    );

    for (
        size_t i = 0;
        i < count;
        i++
    )
    {
        size_t index =
            static_cast<size_t>(
                audioTotalSamples %
                AUDIO_RING_CAPACITY
            );

        // Existing code used >>8 for INMP441 scaling.
        audioRing[index] =
            samples[i] >> 8;

        audioTotalSamples++;
    }

    xSemaphoreGive(
        audioRingMutex
    );
}

uint64_t getAudioTotalSamples()
{
    xSemaphoreTake(
        audioRingMutex,
        portMAX_DELAY
    );

    uint64_t value =
        audioTotalSamples;

    xSemaphoreGive(
        audioRingMutex
    );

    return value;
}

void audioCaptureTask(void* parameter)
{
    (void)parameter;
    int32_t dmaSamples[AUDIO_DMA_READ_SAMPLES];
    DeviceHealth::SensorRetry retry;
    while (true)
    {
        if (audioReinitializeRequested.exchange(false) && retry.ready())
        {
            audioReady.store(false);
            const uint64_t errorAt = healthUptimeMs();
            audioErrorGeneration.fetch_add(1);
            i2s_driver_uninstall(I2S_PORT);
            retry.failed(millis());
            queueAudioFault(DeviceHealth::Fault::I2S_CHANNEL, errorAt);
        }
        if (!retry.ready())
        {
            if (retry.due(millis()))
            {
                const bool initialized = initINMP441();
                retry.attempted(millis(), initialized);
                audioReady.store(initialized);
                if (!initialized) queueAudioFault(DeviceHealth::Fault::I2S_INIT, healthUptimeMs());
            }
            if (!retry.ready()) { vTaskDelay(pdMS_TO_TICKS(100)); continue; }
        }
        size_t bytesRead = 0;
        const esp_err_t status = i2s_read(I2S_PORT, dmaSamples, sizeof(dmaSamples),
                                         &bytesRead, pdMS_TO_TICKS(100));
        if (status != ESP_OK || bytesRead == 0 ||
            bytesRead > sizeof(dmaSamples) || bytesRead % sizeof(int32_t) != 0)
        {
            audioReady.store(false);
            const uint64_t errorAt = healthUptimeMs();
            audioErrorGeneration.fetch_add(1);
            Serial.printf("[AUDIO] Channel error/timeout: %d, bytes=%u\n", status,
                          static_cast<unsigned>(bytesRead));
            // This task alone owns I2S; no concurrent uninstall/read.
            i2s_driver_uninstall(I2S_PORT);
            retry.failed(millis());
            queueAudioFault(DeviceHealth::Fault::I2S_CHANNEL, errorAt);
            vTaskDelay(pdMS_TO_TICKS(100));
            continue;
        }
        appendAudioSamplesToRing(dmaSamples, bytesRead / sizeof(int32_t));
    }
}

// =====================================================
// P5: Copy Exact Audio Sample Range
// =====================================================

bool copyAudioWindow(
    uint64_t startSample,
    size_t sampleCount,
    int32_t* destination
)
{
    xSemaphoreTake(
        audioRingMutex,
        portMAX_DELAY
    );

    uint64_t availableEnd =
        audioTotalSamples;

    if (
        availableEnd <
        startSample +
            sampleCount
    )
    {
        xSemaphoreGive(
            audioRingMutex
        );

        return false;
    }

    // If more than a full ring elapsed after the requested start,
    // the requested samples have already been overwritten.
    if (
        availableEnd -
            startSample >
        AUDIO_RING_CAPACITY
    )
    {
        xSemaphoreGive(
            audioRingMutex
        );

        Serial.println(
            "[AUDIO] Requested synchronized window was overwritten."
        );

        return false;
    }

    for (
        size_t i = 0;
        i < sampleCount;
        i++
    )
    {
        size_t ringIndex =
            static_cast<size_t>(
                (
                    startSample +
                    i
                ) %
                AUDIO_RING_CAPACITY
            );

        destination[i] =
            audioRing[
                ringIndex
            ];
    }

    xSemaphoreGive(
        audioRingMutex
    );

    return true;
}

// =====================================================
// P5: Acoustic Analysis Over Entire 0.64 s Window
// =====================================================

AcousticFeatures analyzeCommonAudioWindow(
    const int32_t* samples,
    size_t count
)
{
    AcousticFeatures result;

    if (
        count !=
        COMMON_AUDIO_SAMPLES
    )
    {
        return result;
    }

    double mean =
        0.0;

    int32_t minValue =
        INT32_MAX;

    int32_t maxValue =
        INT32_MIN;

    for (
        size_t i = 0;
        i < count;
        i++
    )
    {
        int32_t sample =
            samples[i];

        mean +=
            static_cast<double>(
                sample
            );

        if (
            sample <
            minValue
        )
        {
            minValue =
                sample;
        }

        if (
            sample >
            maxValue
        )
        {
            maxValue =
                sample;
        }
    }

    mean /=
        static_cast<double>(
            count
        );

    double sumSquares =
        0.0;

    for (
        size_t i = 0;
        i < count;
        i++
    )
    {
        double centered =
            static_cast<double>(
                samples[i]
            ) -
            mean;

        sumSquares +=
            centered *
            centered;
    }

    result.rmsRaw =
        sqrt(
            sumSquares /
            static_cast<double>(
                count
            )
        );

    result.peakToPeak =
        maxValue -
        minValue;

    // -------------------------------------------------
    // FFT over full common window via averaged spectra.
    // 5 x 2048-sample FFT blocks cover all 10,240 samples.
    // -------------------------------------------------

    for (
        size_t bin = 0;
        bin <
            AUDIO_FFT_SAMPLES / 2;
        bin++
    )
    {
        audioSpectrumAverage[bin] =
            0.0;
    }

    for (
        size_t block = 0;
        block <
            AUDIO_FFT_BLOCKS;
        block++
    )
    {
        size_t offset =
            block *
            AUDIO_FFT_SAMPLES;

        // Remove the mean of each FFT block independently.
        double blockMean =
            0.0;

        for (
            size_t i = 0;
            i <
                AUDIO_FFT_SAMPLES;
            i++
        )
        {
            blockMean +=
                static_cast<double>(
                    samples[
                        offset +
                        i
                    ]
                );
        }

        blockMean /=
            static_cast<double>(
                AUDIO_FFT_SAMPLES
            );

        for (
            size_t i = 0;
            i <
                AUDIO_FFT_SAMPLES;
            i++
        )
        {
            audioFFTReal[i] =
                static_cast<double>(
                    samples[
                        offset +
                        i
                    ]
                ) -
                blockMean;

            audioFFTImag[i] =
                0.0;
        }

        audioFFT.windowing(
            FFTWindow::Hamming,
            FFTDirection::Forward
        );

        audioFFT.compute(
            FFTDirection::Forward
        );

        audioFFT.complexToMagnitude();

        for (
            size_t bin = 1;
            bin <
                AUDIO_FFT_SAMPLES / 2;
            bin++
        )
        {
            audioSpectrumAverage[bin] +=
                audioFFTReal[bin];
        }
    }

    // Average the spectra and find dominant non-DC bin.
    size_t peakBin =
        1;

    double peakMagnitude =
        0.0;

    for (
        size_t bin = 1;
        bin <
            AUDIO_FFT_SAMPLES / 2;
        bin++
    )
    {
        double magnitude =
            audioSpectrumAverage[bin] /
            static_cast<double>(
                AUDIO_FFT_BLOCKS
            );

        if (
            magnitude >
            peakMagnitude
        )
        {
            peakMagnitude =
                magnitude;

            peakBin =
                bin;
        }
    }

    result.peakHz =
        (
            static_cast<double>(
                peakBin
            ) *
            static_cast<double>(
                AUDIO_SAMPLE_RATE
            )
        ) /
        static_cast<double>(
            AUDIO_FFT_SAMPLES
        );

    return result;
}

// =====================================================
// Vibration Task
// =====================================================

#include "continuous_vibration_runtime.h"

void vibrationTask(void* parameter)
{
    (void)parameter;
    while (true)
    {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
        bool channelValid = adxlRead(REG_DEVID) == 0xE5;
        VibrationFeatures local;
        if (channelValid) local = acquireVibration();
        channelValid = channelValid && adxlRead(REG_DEVID) == 0xE5;
        xSemaphoreTake(vibrationResultMutex, portMAX_DELAY);
        vibrationResult = local;
        vibrationChannelValid = channelValid;
        vibrationFinished = true;
        vibrationBusy.store(false);
        xSemaphoreGive(vibrationResultMutex);
    }
}

// =====================================================
// P5: Synchronized Sensor Acquisition
// =====================================================

bool acquireSynchronizedFeatures(
    VibrationFeatures& vib,
    AcousticFeatures& audio
)
{
#if CONTINUOUS_VIBRATION_ENABLED
    // Summary telemetry consumes a completed window; it never starts/stops ADC.
    return ContinuousVibration::snapshot(vib, audio);
#endif
    if (vibrationBusy.load())
    {
        observeSensorFault(DeviceHealth::Fault::VIBRATION_TIMEOUT, true, healthUptimeMs());
        return false; // A late worker must finish before another request.
    }
    const uint32_t audioGeneration = audioErrorGeneration.load();
    Serial.println();
    Serial.println(
        "[SYNC] Starting synchronized vibration + acoustic acquisition..."
    );

    // Snapshot the continuous audio stream before vibration starts.
    // The audio task continuously drains I2S DMA, so this point is close
    // to the current acoustic stream position rather than old queued DMA.
    uint64_t audioWindowStart =
        getAudioTotalSamples();

    xSemaphoreTake(
        vibrationResultMutex,
        portMAX_DELAY
    );

    vibrationFinished =
        false;

    xSemaphoreGive(
        vibrationResultMutex
    );

    uint32_t startedMs =
        millis();

    vibrationBusy.store(true);
    xTaskNotifyGive(
        vibrationTaskHandle
    );

    // Wait for the 0.64 s vibration acquisition.
    while (true)
    {
        bool done;

        xSemaphoreTake(
            vibrationResultMutex,
            portMAX_DELAY
        );

        done =
            vibrationFinished;

        xSemaphoreGive(
            vibrationResultMutex
        );

        if (done)
        {
            break;
        }

        if (
            millis() -
                startedMs >
            1500
        )
        {
            Serial.println(
                "[SYNC] Vibration acquisition timeout."
            );

            observeSensorFault(DeviceHealth::Fault::VIBRATION_TIMEOUT, true, healthUptimeMs());

            return false;
        }

        delay(1);
    }

    xSemaphoreTake(vibrationResultMutex, portMAX_DELAY);
    const bool validVibration = vibrationChannelValid;
    xSemaphoreGive(vibrationResultMutex);
    if (!validVibration)
    {
        adxlRetry.failed(millis());
        observeSensorFault(DeviceHealth::Fault::ADXL_CHANNEL, true, healthUptimeMs());
        return false;
    }

    // The common acoustic interval is exactly 10,240 samples = 0.64 s.
    uint64_t requiredAudioEnd =
        audioWindowStart +
        COMMON_AUDIO_SAMPLES;

    uint32_t audioWaitStarted =
        millis();

    while (
        getAudioTotalSamples() <
        requiredAudioEnd
    )
    {
        if (
            millis() -
                audioWaitStarted >
            500
        )
        {
            Serial.println(
                "[SYNC] Acoustic common-window timeout."
            );

            observeSensorFault(DeviceHealth::Fault::AUDIO_WINDOW, true, healthUptimeMs());

            return false;
        }

        delay(1);
    }

    if (
        !copyAudioWindow(
            audioWindowStart,
            COMMON_AUDIO_SAMPLES,
            commonAudioWindow
        )
    )
    {
        Serial.println(
            "[SYNC] Failed to copy synchronized acoustic window."
        );

        observeSensorFault(DeviceHealth::Fault::AUDIO_WINDOW, true, healthUptimeMs());

        return false;
    }

    xSemaphoreTake(
        vibrationResultMutex,
        portMAX_DELAY
    );

    vib =
        vibrationResult;

    xSemaphoreGive(
        vibrationResultMutex
    );

    if (!audioReady.load() || audioGeneration != audioErrorGeneration.load())
    {
        // The audio owner queues the actual occurrence time; serviceSensors()
        // drains it after this acquisition fails, without replacing it with now.
        return false; // No features across a discontinuous DMA stream.
    }

    if (!DeviceHealth::hasPcmVariation(commonAudioWindow, COMMON_AUDIO_SAMPLES))
    {
        // A whole constant digital window is a suspected stuck/missing channel,
        // not evidence of a perfectly healthy silent machine.
        audioReinitializeRequested.store(true);
        observeSensorFault(DeviceHealth::Fault::I2S_CHANNEL, true, healthUptimeMs());
        return false;
    }

    audio =
        analyzeCommonAudioWindow(
            commonAudioWindow,
            COMMON_AUDIO_SAMPLES
        );

    uint32_t elapsed =
        millis() -
        startedMs;

    Serial.println();
    Serial.println(
        "========== EDGE FEATURES =========="
    );

    Serial.printf(
        "Firmware            : %s\n",
        FIRMWARE_VERSION
    );

    Serial.printf(
        "Common window       : 640 ms\n"
    );

    Serial.printf(
        "Acquisition         : %lu ms\n",
        static_cast<unsigned long>(
            elapsed
        )
    );

    Serial.printf(
        "vibrationRmsRaw     : %.6f g\n",
        vib.totalRms
    );

    Serial.printf(
        "vibrationPeakHz     : %.2f Hz\n",
        vib.peakHz
    );

    Serial.printf(
        "acousticRmsRaw      : %.2f\n",
        audio.rmsRaw
    );

    Serial.printf(
        "acousticPeakHz      : %.2f Hz\n",
        audio.peakHz
    );

    Serial.println(
        "==================================="
    );

    return true;
}

// =====================================================
// Wi-Fi: Non-Blocking Recovery
// =====================================================

void startWiFiReconnect()
{
    if (
        WiFi.status() ==
        WL_CONNECTED
    )
    {
        return;
    }

    if (
        wifiReconnectInProgress
    )
    {
        return;
    }

    if (
        millis() -
            lastNetworkRetry <
        NETWORK_RETRY_MS
    )
    {
        return;
    }

    lastNetworkRetry =
        millis();

    Serial.println();

    Serial.printf(
        "[WiFi] Starting non-blocking connection to %s\n",
        WIFI_SSID
    );

    WiFi.mode(
        WIFI_STA
    );

    WiFi.begin(
        WIFI_SSID,
        WIFI_PASSWORD
    );

    wifiReconnectInProgress =
        true;

    wifiReconnectStartedAt =
        millis();
}

void serviceWiFi()
{
    wl_status_t status =
        WiFi.status();

    if (
        status ==
        WL_CONNECTED
    )
    {
        if (
            !wifiWasConnected
        )
        {
            Serial.println(
                "[OK] Wi-Fi connected."
            );

            Serial.print(
                "[WiFi] IP   : "
            );

            Serial.println(
                WiFi.localIP()
            );

            Serial.printf(
                "[WiFi] RSSI : %d dBm\n",
                WiFi.RSSI()
            );
        }

        wifiWasConnected =
            true;

        wifiReconnectInProgress =
            false;

        return;
    }

    if (
        wifiWasConnected
    )
    {
        Serial.println(
            "[WiFi] Connection lost."
        );

        wifiWasConnected =
            false;
    }

    if (
        wifiReconnectInProgress &&
        millis() -
            wifiReconnectStartedAt >=
            WIFI_ATTEMPT_TIMEOUT_MS
    )
    {
        Serial.println(
            "[WiFi] Reconnect attempt timed out."
        );

        wifiReconnectInProgress =
            false;
    }

    startWiFiReconnect();
}

// =====================================================
// Timestamp
// =====================================================

String getTimestamp()
{
    time_t now =
        time(nullptr);

    if (
        now <
        1700000000
    )
    {
        return "";
    }

    struct tm info;

    gmtime_r(
        &now,
        &info
    );

    char buffer[32];

    strftime(
        buffer,
        sizeof(buffer),
        "%Y-%m-%dT%H:%M:%SZ",
        &info
    );

    return String(
        buffer
    );
}


String formatTimestampFromEpoch(
    uint64_t epochSeconds
)
{
    if (
        epochSeconds <
        1700000000ULL
    )
    {
        return "";
    }

    time_t value =
        static_cast<time_t>(
            epochSeconds
        );

    struct tm info;

    gmtime_r(
        &value,
        &info
    );

    char buffer[32];

    strftime(
        buffer,
        sizeof(buffer),
        "%Y-%m-%dT%H:%M:%SZ",
        &info
    );

    return String(
        buffer
    );
}

// =====================================================
// Backend Transport + Time Fallback
// =====================================================

bool beginBackendHttp(
    BackendHttp& http,
    WiFiClientSecure& secureClient,
    const char* url
)
{
    if (!http.acquire()) return false;
    if (
        !FirmwareLogic::isBackendTransportAllowed(
            url,
            ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV
        )
    )
    {
        Serial.printf(
            "[SECURITY] Refusing backend URL without HTTPS: %s\n",
            url != nullptr ? url : "(null)"
        );

        return false;
    }

    const String target =
        String(url);

    if (
        target.startsWith(
            "https://"
        )
    )
    {
        if (
            BACKEND_CA_CERT == nullptr ||
            BACKEND_CA_CERT[0] == '\0'
        )
        {
            Serial.println(
                "[SECURITY] HTTPS requires BACKEND_CA_CERT_VALUE; refusing insecure TLS."
            );

            return false;
        }

        secureClient.setCACert(
            BACKEND_CA_CERT
        );

        return http.begin(
            secureClient,
            target
        );
    }

    // Development escape hatch only. The committed example/default is false.
    // Production must terminate TLS with a CA-validated certificate.
    Serial.println(
        "[SECURITY][DEV-ONLY] Plain HTTP explicitly enabled; Bearer token/ACK integrity are NOT protected."
    );

    return http.begin(
        target
    );
}

bool syncTimeFromBackend()
{
    if (
        WiFi.status() !=
        WL_CONNECTED
    )
    {
        Serial.println(
            "[TIME] Backend fallback unavailable."
        );

        return false;
    }

    WiFiClientSecure secureClient;
    BackendHttp http;

    http.setConnectTimeout(
        3000
    );

    http.setTimeout(
        3000
    );

    if (
        !beginBackendHttp(
            http,
            secureClient,
            HEALTH_URL
        )
    )
    {
        Serial.println(
            "[TIME] Backend health connection failed or transport policy rejected it."
        );

        return false;
    }

    http.addHeader(
        "Authorization",
        String("Bearer ") + DEVICE_HEALTH_TOKEN
    );

    const int statusCode =
        http.GET();

    if (
        statusCode !=
        200
    )
    {
        Serial.printf(
            "[TIME] Backend health HTTP error: %d\n",
            statusCode
        );

        http.end();
        return false;
    }

    const String response =
        http.getString();

    http.end();

    int64_t serverEpochMs =
        0;

    if (
        !FirmwareLogic::parseHealthTimestampJson(
            response.c_str(),
            serverEpochMs
        ) ||
        serverEpochMs <
            1700000000000LL
    )
    {
        Serial.println(
            "[TIME] /api/health timestamp is not the API-v1.3 RFC3339 contract."
        );

        return false;
    }

    struct timeval tv;

    tv.tv_sec =
        static_cast<time_t>(
            serverEpochMs /
            1000LL
        );

    tv.tv_usec =
        static_cast<suseconds_t>(
            (
                serverEpochMs %
                1000LL
            ) *
            1000LL
        );

    settimeofday(
        &tv,
        nullptr
    );

    timeReady =
        true;

    Serial.printf(
        "[OK] Backend RFC3339 time synchronized: %s\n",
        getTimestamp().c_str()
    );

    return true;
}

// =====================================================
// Time Sync
// =====================================================

bool syncTime()
{
    if (
        TEST_FORCE_NTP_FAIL
    )
    {
        Serial.println(
            "[TEST] Forced NTP failure enabled."
        );

        Serial.println(
            "[TIME] Trying backend time fallback..."
        );

        if (
            syncTimeFromBackend()
        )
        {
            return true;
        }

        timeReady =
            false;

        return false;
    }

    Serial.println(
        "[TIME] Synchronizing NTP..."
    );

    configTime(
        0,
        0,
        "pool.ntp.org",
        "time.google.com"
    );

    uint32_t started =
        millis();

    time_t now =
        time(nullptr);

    while (
        now <
            1700000000 &&
        millis() -
            started <
            8000
    )
    {
        Serial.print(".");
        delay(500);

        now =
            time(nullptr);
    }

    Serial.println();

    if (
        now >=
        1700000000
    )
    {
        timeReady =
            true;

        Serial.printf(
            "[OK] NTP synchronized: %s\n",
            getTimestamp().c_str()
        );

        return true;
    }

    Serial.println(
        "[WARNING] NTP unavailable."
    );

    Serial.println(
        "[TIME] Trying backend time fallback..."
    );

    if (
        syncTimeFromBackend()
    )
    {
        return true;
    }

    timeReady =
        false;

    Serial.println(
        "[ERROR] No valid time source available."
    );

    return false;
}

// =====================================================
// Sequence
// =====================================================

bool persistSequenceValue(
    uint32_t value
)
{
    if (
        TEST_FORCE_SEQUENCE_NVS_FAIL
    )
    {
        Serial.printf(
            "[TEST][SEQUENCE] Forced NVS failure for candidate %lu.\n",
            static_cast<unsigned long>(
                value
            )
        );

        sequencePersistenceReady =
            false;

        return false;
    }

    const size_t written =
        preferences.putUInt(
            "sequence",
            value
        );

    const bool writeSucceeded =
        written ==
        sizeof(uint32_t);

    if (
        !writeSucceeded
    )
    {
        Serial.printf(
            "[SEQUENCE] NVS write FAILED for sequence %lu; packet will not be created.\n",
            static_cast<unsigned long>(
                value
            )
        );

        sequencePersistenceReady =
            false;

        return false;
    }

    const uint32_t verified =
        preferences.getUInt(
            "sequence",
            0
        );

    const bool persistenceVerified =
        FirmwareLogic::shouldAdvanceSequence(
            writeSucceeded,
            true,
            value,
            verified
        );

    if (
        !persistenceVerified
    )
    {
        Serial.printf(
            "[SEQUENCE] NVS read-back mismatch: expected=%lu actual=%lu; packet will not be created.\n",
            static_cast<unsigned long>(
                value
            ),
            static_cast<unsigned long>(
                verified
            )
        );

        sequencePersistenceReady =
            false;

        return false;
    }

    sequencePersistenceReady =
        true;

    return true;
}

bool ensureCurrentSequenceFloorDurable()
{
    uint32_t stored =
        preferences.getUInt(
            "sequence",
            0
        );

    if (
        stored >=
        telemetrySequence
    )
    {
        sequencePersistenceReady =
            true;

        return true;
    }

    return persistSequenceValue(
        telemetrySequence
    );
}

bool allocateSequence(
    uint32_t& allocatedSequence
)
{
    allocatedSequence =
        0;

    // Recovery may discover a higher sequence in LittleFS than the value
    // that survived in NVS. Never allocate another ID until that recovered
    // floor itself has been made durable.
    if (
        !sequencePersistenceReady &&
        !ensureCurrentSequenceFloorDurable()
    )
    {
        return false;
    }

    if (
        telemetrySequence ==
        UINT32_MAX
    )
    {
        Serial.println(
            "[SEQUENCE] Sequence space exhausted; refusing unsafe reuse."
        );

        sequencePersistenceReady =
            false;

        return false;
    }

    uint32_t candidate =
        telemetrySequence + 1U;

    // Durability comes BEFORE use. If this write fails, candidate is not
    // attached to a packet, not written to the ring and not transmitted.
    if (
        !persistSequenceValue(
            candidate
        )
    )
    {
        return false;
    }

    telemetrySequence =
        candidate;

    allocatedSequence =
        candidate;

    return true;
}

// =====================================================
// JSON
// =====================================================
//
// P2 label contract:
// - scenarioLabel is REQUIRED by API but nullable.
// - Ordinary ESP32/MQTT real telemetry sends explicit null.
// - knownVibrationLabel and knownAcousticLabel also remain null.
// - The device never promotes model/rule output to ground truth.
// =====================================================

String createCanonicalTelemetryPayload(
    const TelemetryPacket& packet
)
{
    if (
        !packet.timestampResolved ||
        packet.epochSeconds <
            1700000000ULL
    )
    {
        return "";
    }

    String timestamp =
        formatTimestampFromEpoch(
            packet.epochSeconds
        );

    if (
        timestamp.length() ==
        0
    )
    {
        return "";
    }

    FirmwareLogic::CanonicalTelemetry telemetry;

    telemetry.sequence =
        packet.sequence;

    telemetry.vibrationRmsRaw =
        packet.vibrationRmsRaw;

    telemetry.vibrationPeakHz =
        packet.vibrationPeakHz;

    telemetry.acousticRmsRaw =
        packet.acousticRmsRaw;

    telemetry.acousticPeakHz =
        packet.acousticPeakHz;

    const std::string canonicalPayload =
        FirmwareLogic::buildCanonicalTelemetryPayload(
            timestamp.c_str(),
            SITE_ID,
            ASSET_ID,
            DEVICE_ID,
            telemetry
        );

    if (
        canonicalPayload.empty()
    )
    {
        return "";
    }

    return String(
        canonicalPayload.c_str()
    );
}

// =====================================================
// Bounded Isolation Archive Paths
// =====================================================

String rejectedPacketFilePath(
    uint32_t sequence
)
{
    const uint32_t slot =
        sequence %
        static_cast<uint32_t>(
            MAX_REJECTED_ARCHIVE_FILES
        );

    char path[64];

    snprintf(
        path,
        sizeof(path),
        "%s/slot_%03lu.json",
        REJECTED_DIR,
        static_cast<unsigned long>(
            slot
        )
    );

    return String(path);
}

String rejectedTempFilePath(
    uint32_t sequence
)
{
    const uint32_t slot =
        sequence %
        static_cast<uint32_t>(
            MAX_REJECTED_ARCHIVE_FILES
        );

    char path[64];

    snprintf(
        path,
        sizeof(path),
        "%s/slot_%03lu.tmp",
        REJECTED_DIR,
        static_cast<unsigned long>(
            slot
        )
    );

    return String(path);
}

// =====================================================
// Binary Ring CRC32
// =====================================================

uint32_t crc32Update(
    uint32_t crc,
    const uint8_t* data,
    size_t length
)
{
    crc =
        ~crc;

    for (
        size_t i = 0;
        i < length;
        i++
    )
    {
        crc ^=
            data[i];

        for (
            uint8_t bit = 0;
            bit < 8;
            bit++
        )
        {
            uint32_t mask =
                static_cast<uint32_t>(
                    -static_cast<int32_t>(
                        crc & 1U
                    )
                );

            crc =
                (crc >> 1) ^
                (0xEDB88320UL & mask);
        }
    }

    return ~crc;
}

uint32_t calculateRecordCrc(
    const BinaryTelemetryRecord& record
)
{
    return crc32Update(
        0,
        reinterpret_cast<const uint8_t*>(
            &record
        ),
        offsetof(
            BinaryTelemetryRecord,
            crc32
        )
    );
}

bool recordHasUnresolvedTimestamp(
    const BinaryTelemetryRecord& record
)
{
    return (
        record.flags &
        RING_FLAG_TIME_UNRESOLVED
    ) != 0;
}

FirmwareLogic::RingRecordTimeEncoding recordTimeEncoding(
    const BinaryTelemetryRecord& record
)
{
    return FirmwareLogic::classifyRingRecordTimeEncoding(
        record.schemaVersion,
        record.flags,
        record.epochSeconds,
        RING_LEGACY_SCHEMA_VERSION,
        RING_SCHEMA_VERSION,
        RING_FLAG_TIME_UNRESOLVED,
        1700000000ULL
    );
}

bool recordHasLegacyUnresolvedTimestamp(
    const BinaryTelemetryRecord& record
)
{
    return (
        recordTimeEncoding(
            record
        ) ==
        FirmwareLogic::RingRecordTimeEncoding::LEGACY_UNRESOLVED_V1
    );
}

FirmwareLogic::RingRecoveryDisposition recordRecoveryDisposition(
    const BinaryTelemetryRecord& record
)
{
    if (
        record.magic !=
            RING_MAGIC ||
        record.ordinal == 0 ||
        record.sequence == 0
    )
    {
        return FirmwareLogic::RingRecoveryDisposition::CORRUPT;
    }

    const bool measurementsFinite =
        isfinite(
            record.vibrationRmsRaw
        ) &&
        isfinite(
            record.vibrationPeakHz
        ) &&
        isfinite(
            record.acousticRmsRaw
        ) &&
        isfinite(
            record.acousticPeakHz
        );

    const bool crcMatches =
        record.crc32 ==
        calculateRecordCrc(
            record
        );

    return FirmwareLogic::classifyRingRecordForRecovery(
        record.schemaVersion,
        record.flags,
        record.epochSeconds,
        crcMatches,
        measurementsFinite,
        RING_LEGACY_SCHEMA_VERSION,
        RING_SCHEMA_VERSION,
        RING_FLAG_TIME_UNRESOLVED,
        1700000000ULL
    );
}

bool recordIsValid(
    const BinaryTelemetryRecord& record
)
{
    return (
        recordRecoveryDisposition(
            record
        ) !=
        FirmwareLogic::RingRecoveryDisposition::CORRUPT
    );
}

// =====================================================
// Binary Ring File
// =====================================================

uint64_t ringFileSizeBytes()
{
    return (
        static_cast<uint64_t>(
            RING_SLOT_COUNT
        ) *
        sizeof(
            BinaryTelemetryRecord
        )
    );
}

uint32_t ringSlotFromOrdinal(
    uint64_t ordinal
)
{
    return static_cast<uint32_t>(
        (ordinal - 1ULL) %
        RING_SLOT_COUNT
    );
}

uint64_t ringOffsetForOrdinal(
    uint64_t ordinal
)
{
    return (
        static_cast<uint64_t>(
            ringSlotFromOrdinal(
                ordinal
            )
        ) *
        sizeof(
            BinaryTelemetryRecord
        )
    );
}

bool ensureRingFile()
{
    if (
        LittleFS.exists(
            RING_FILE
        )
    )
    {
        File existing =
            LittleFS.open(
                RING_FILE,
                FILE_READ
            );

        if (!existing)
        {
            Serial.println(
                "[RING] Existing ring cannot be opened; preserving filesystem without format."
            );

            return false;
        }

        uint64_t existingSize =
            existing.size();

        existing.close();

        if (
            existingSize !=
            ringFileSizeBytes()
        )
        {
            Serial.printf(
                "[RING] Existing ring size mismatch: expected=%llu actual=%llu. "
                "Backlog preserved; automatic recreation is disabled.\n",
                static_cast<unsigned long long>(
                    ringFileSizeBytes()
                ),
                static_cast<unsigned long long>(
                    existingSize
                )
            );

            return false;
        }

        return true;
    }

    Serial.printf(
        "[RING] Initializing %llu-byte binary ring (%u records)...\n",
        static_cast<unsigned long long>(
            ringFileSizeBytes()
        ),
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        )
    );

    File file =
        LittleFS.open(
            RING_FILE,
            FILE_WRITE
        );

    if (!file)
    {
        return false;
    }

    uint64_t remaining =
        ringFileSizeBytes();

    if (
        remaining == 0
    )
    {
        file.close();
        return false;
    }

    uint8_t zeroBlock[512] = {};

    while (
        remaining > 0
    )
    {
        size_t chunk =
            remaining >
                sizeof(zeroBlock)
                ? sizeof(zeroBlock)
                : static_cast<size_t>(
                      remaining
                  );

        if (
            file.write(
                zeroBlock,
                chunk
            ) !=
            chunk
        )
        {
            file.close();
            return false;
        }

        remaining -=
            chunk;

        delay(0);
    }

    file.flush();
    file.close();

    Serial.println(
        "[RING] Binary ring initialized."
    );

    return true;
}

FirmwareLogic::RingReadClass readRingRecord(
    uint64_t ordinal,
    BinaryTelemetryRecord& record
)
{
    if (
        TEST_FORCE_RING_READ_IO_FAIL
    )
    {
        Serial.println(
            "[TEST][RING] Forced transient read I/O failure."
        );

        return FirmwareLogic::RingReadClass::IO_ERROR;
    }

    if (
        ordinal == 0
    )
    {
        return FirmwareLogic::RingReadClass::CORRUPT;
    }

    File file =
        LittleFS.open(
            RING_FILE,
            "r"
        );

    if (!file)
    {
        return FirmwareLogic::RingReadClass::IO_ERROR;
    }

    if (
        !file.seek(
            ringOffsetForOrdinal(
                ordinal
            )
        )
    )
    {
        file.close();
        return FirmwareLogic::RingReadClass::IO_ERROR;
    }

    const size_t bytes =
        file.read(
            reinterpret_cast<uint8_t*>(
                &record
            ),
            sizeof(record)
        );

    file.close();

    if (
        bytes !=
        sizeof(record)
    )
    {
        return FirmwareLogic::RingReadClass::IO_ERROR;
    }

    if (
        record.ordinal !=
            ordinal ||
        !recordIsValid(
            record
        )
    )
    {
        return FirmwareLogic::RingReadClass::CORRUPT;
    }

    return FirmwareLogic::RingReadClass::VALID;
}

bool writeRingRecord(
    BinaryTelemetryRecord record
)
{
    if (
        TEST_FORCE_RING_WRITE_FAIL
    )
    {
        Serial.println(
            "[TEST][RING] Forced write/read-back failure."
        );

        return false;
    }

    record.magic =
        RING_MAGIC;

    record.schemaVersion =
        RING_SCHEMA_VERSION;

    record.crc32 =
        calculateRecordCrc(
            record
        );

    File file =
        LittleFS.open(
            RING_FILE,
            "r+"
        );

    if (!file)
    {
        return false;
    }

    if (
        !file.seek(
            ringOffsetForOrdinal(
                record.ordinal
            )
        )
    )
    {
        file.close();
        return false;
    }

    size_t bytes =
        file.write(
            reinterpret_cast<const uint8_t*>(
                &record
            ),
            sizeof(record)
        );

    file.flush();
    file.close();

    if (
        bytes !=
        sizeof(record)
    )
    {
        return false;
    }

    // Read-after-write protects against torn/corrupt local persistence
    // before the record becomes part of the active queue.
    BinaryTelemetryRecord verify;

    return (
        readRingRecord(
            record.ordinal,
            verify
        ) ==
            FirmwareLogic::RingReadClass::VALID &&
        verify.sequence ==
            record.sequence
    );
}

bool invalidateRingRecord(
    uint64_t ordinal
)
{
    File file =
        LittleFS.open(
            RING_FILE,
            "r+"
        );

    if (!file)
    {
        return false;
    }

    if (
        !file.seek(
            ringOffsetForOrdinal(
                ordinal
            )
        )
    )
    {
        file.close();
        return false;
    }

    // Zeroing magic invalidates the slot. A power loss during this small
    // update is also detected by the record CRC on the next boot.
    uint32_t zero =
        0;

    size_t bytes =
        file.write(
            reinterpret_cast<const uint8_t*>(
                &zero
            ),
            sizeof(zero)
        );

    file.flush();
    file.close();

    return (
        bytes ==
        sizeof(zero)
    );
}

// =====================================================
// Consumed-Ordinal Watermark
// =====================================================
//
// Why this exists:
// invalidateRingRecord() changes only 4 bytes, but LittleFS.flush() on the
// 1.2 MB preallocated ring was measured at ~10.4-10.6 seconds per ACK.
// That made a 650-record recovery take roughly two hours.
//
// New behavior:
// - ACK/reject/corrupt-skip advances the in-RAM FIFO head immediately.
// - Every 32 consumed records (or when replay pauses/completes), the highest
//   consumed ordinal is committed to NVS Preferences.
// - Recovery ignores valid ring records with ordinal <= this watermark.
// - Physical ring slots are naturally overwritten as the circular ring wraps.
//
// Crash safety:
// If power is lost before a pending watermark commit, those few already-ACKed
// records can replay after reboot. The backend duplicate-sequence contract
// safely ACKs them again, so no accepted telemetry is lost.

bool commitConsumedWatermark()
{
    if (
        pendingConsumedOrdinal <=
            committedConsumedOrdinal
    )
    {
        pendingConsumedCount =
            0;

        return true;
    }

    size_t written =
        preferences.putULong64(
            "ringConsumed",
            pendingConsumedOrdinal
        );

    if (
        written !=
        sizeof(uint64_t)
    )
    {
        Serial.printf(
            "[WATERMARK] Commit FAILED at ordinal %llu. "
            "Safe fallback: duplicates may replay after reboot.\n",
            static_cast<unsigned long long>(
                pendingConsumedOrdinal
            )
        );

        return false;
    }

    committedConsumedOrdinal =
        pendingConsumedOrdinal;

    pendingConsumedCount =
        0;

    Serial.printf(
        "[WATERMARK] Committed consumed ordinal %llu.\n",
        static_cast<unsigned long long>(
            committedConsumedOrdinal
        )
    );

    return true;
}

void noteConsumedOrdinal(
    uint64_t ordinal,
    bool forceCommit = false
)
{
    if (
        ordinal == 0
    )
    {
        return;
    }

    if (
        ordinal >
        pendingConsumedOrdinal
    )
    {
        pendingConsumedOrdinal =
            ordinal;
    }

    pendingConsumedCount++;

    if (
        FirmwareLogic::shouldCommitWatermark(
            pendingConsumedCount,
            ACK_WATERMARK_BATCH_SIZE,
            forceCommit
        )
    )
    {
        commitConsumedWatermark();
    }
}

// =====================================================
// Boot Session + Durable Time Anchors
// =====================================================

String timeAnchorKey(
    size_t slot
)
{
    char key[8];

    snprintf(
        key,
        sizeof(key),
        "ta%02u",
        static_cast<unsigned int>(
            slot
        )
    );

    return String(key);
}

void loadTimeAnchorHistory()
{
    for (
        size_t slot = 0;
        slot < TIME_ANCHOR_SLOT_COUNT;
        slot++
    )
    {
        timeAnchorValid[slot] =
            false;

        TimeAnchorBlob candidate;

        const String key =
            timeAnchorKey(slot);

        if (
            !preferences.isKey(
                key.c_str()
            )
        )
        {
            continue;
        }

        const size_t bytes =
            preferences.getBytes(
                key.c_str(),
                &candidate,
                sizeof(candidate)
            );

        if (
            bytes != sizeof(candidate) ||
            !FirmwareLogic::isDurableTimeAnchorValid(
                candidate,
                TIME_ANCHOR_MAGIC,
                TIME_ANCHOR_VERSION,
                1700000000000LL
            )
        )
        {
            continue;
        }

        timeAnchors[slot] =
            candidate;

        timeAnchorValid[slot] =
            true;
    }
}

bool findTimeAnchor(
    uint32_t sessionId,
    FirmwareLogic::TimeAnchor& anchor
)
{
    if (
        sessionId == 0
    )
    {
        return false;
    }

    for (
        size_t slot = 0;
        slot < TIME_ANCHOR_SLOT_COUNT;
        slot++
    )
    {
        if (
            !timeAnchorValid[slot] ||
            timeAnchors[slot].sessionId !=
                sessionId
        )
        {
            continue;
        }

        anchor.sessionId =
            timeAnchors[slot].sessionId;

        anchor.monotonicMs =
            timeAnchors[slot].monotonicMs;

        anchor.epochMs =
            timeAnchors[slot].epochMs;

        return true;
    }

    return false;
}

bool initializeBootSession()
{
    const uint32_t stored =
        preferences.getUInt(
            "bootSession",
            0
        );

    if (
        stored == UINT32_MAX
    )
    {
        Serial.println(
            "[TIME] Boot-session ID space exhausted; refusing unsafe reuse."
        );

        return false;
    }

    const uint32_t candidate =
        stored + 1U;

    const size_t written =
        preferences.putUInt(
            "bootSession",
            candidate
        );

    const uint32_t verified =
        preferences.getUInt(
            "bootSession",
            0
        );

    if (
        written != sizeof(uint32_t) ||
        verified != candidate
    )
    {
        Serial.println(
            "[TIME] Failed to durably allocate boot-session ID."
        );

        return false;
    }

    bootSessionId =
        candidate;

    bootSessionReady =
        true;

    Serial.printf(
        "[TIME] Boot session ID: %lu\n",
        static_cast<unsigned long>(
            bootSessionId
        )
    );

    return true;
}

bool persistCurrentSessionTimeAnchor()
{
    if (
        !timeReady ||
        !bootSessionReady
    )
    {
        return false;
    }

    FirmwareLogic::TimeAnchor existing;

    if (
        findTimeAnchor(
            bootSessionId,
            existing
        )
    )
    {
        return true;
    }

    struct timeval tv;

    if (
        gettimeofday(
            &tv,
            nullptr
        ) != 0 ||
        tv.tv_sec < 1700000000
    )
    {
        return false;
    }

    TimeAnchorBlob candidate;

    candidate.magic =
        TIME_ANCHOR_MAGIC;

    candidate.version =
        TIME_ANCHOR_VERSION;

    candidate.sessionId =
        bootSessionId;

    candidate.monotonicMs =
        millis();

    candidate.epochMs =
        static_cast<int64_t>(
            tv.tv_sec
        ) * 1000LL +
        static_cast<int64_t>(
            tv.tv_usec /
            1000
        );

    FirmwareLogic::finalizeDurableTimeAnchor(
        candidate
    );

    const size_t slot =
        static_cast<size_t>(
            candidate.sessionId %
            TIME_ANCHOR_SLOT_COUNT
        );

    const String key =
        timeAnchorKey(slot);

    const size_t written =
        preferences.putBytes(
            key.c_str(),
            &candidate,
            sizeof(candidate)
        );

    TimeAnchorBlob verified;

    const size_t readBack =
        preferences.getBytes(
            key.c_str(),
            &verified,
            sizeof(verified)
        );

    if (
        written != sizeof(candidate) ||
        readBack != sizeof(verified) ||
        !FirmwareLogic::isDurableTimeAnchorValid(
            verified,
            TIME_ANCHOR_MAGIC,
            TIME_ANCHOR_VERSION,
            1700000000000LL
        ) ||
        memcmp(
            &candidate,
            &verified,
            sizeof(candidate)
        ) != 0
    )
    {
        Serial.println(
            "[TIME] Durable session timestamp anchor write/read-back failed."
        );

        return false;
    }

    timeAnchors[slot] =
        candidate;

    timeAnchorValid[slot] =
        true;

    Serial.printf(
        "[TIME] Durable timestamp anchor: session=%lu monotonicMs=%lu epochMs=%lld\n",
        static_cast<unsigned long>(
            candidate.sessionId
        ),
        static_cast<unsigned long>(
            candidate.monotonicMs
        ),
        static_cast<long long>(
            candidate.epochMs
        )
    );

    return true;
}

// =====================================================
// Binary <-> HTTP Packet
// =====================================================

enum class RecordToPacketResult
{
    READY,
    WAITING_FOR_TIME_ANCHOR,
    PREVIOUS_SESSION_TIME_UNRESOLVABLE,
    LEGACY_UNRESOLVED_TIME,
    INVALID_RECORD
};

BinaryTelemetryRecord packetToRecord(
    const TelemetryPacket& packet,
    uint64_t ordinal
)
{
    BinaryTelemetryRecord record;

    record.ordinal =
        ordinal;

    record.sequence =
        packet.sequence;

    if (
        packet.timestampResolved
    )
    {
        record.flags =
            0;

        record.epochSeconds =
            packet.epochSeconds;
    }
    else
    {
        record.flags =
            RING_FLAG_TIME_UNRESOLVED;

        record.epochSeconds =
            FirmwareLogic::packOfflineCaptureMetadata(
                packet.offlineSessionId,
                packet.captureMonotonicMs
            );
    }

    record.vibrationRmsRaw =
        packet.vibrationRmsRaw;

    record.vibrationPeakHz =
        packet.vibrationPeakHz;

    record.acousticRmsRaw =
        packet.acousticRmsRaw;

    record.acousticPeakHz =
        packet.acousticPeakHz;

    return record;
}

RecordToPacketResult recordToPacket(
    const BinaryTelemetryRecord& record,
    TelemetryPacket& packet
)
{
    if (
        !recordIsValid(
            record
        )
    )
    {
        return RecordToPacketResult::INVALID_RECORD;
    }

    packet.sequence =
        record.sequence;

    packet.vibrationRmsRaw =
        record.vibrationRmsRaw;

    packet.vibrationPeakHz =
        record.vibrationPeakHz;

    packet.acousticRmsRaw =
        record.acousticRmsRaw;

    packet.acousticPeakHz =
        record.acousticPeakHz;

    const FirmwareLogic::RingRecordTimeEncoding timeEncoding =
        recordTimeEncoding(
            record
        );

    if (
        timeEncoding ==
        FirmwareLogic::RingRecordTimeEncoding::LEGACY_UNRESOLVED_V1
    )
    {
        // The pre-migration PR #13 format contains no boot generation or
        // monotonic capture time. Preserve it in isolation instead of
        // interpreting epochSeconds=0 as packed metadata or inventing UTC.
        return RecordToPacketResult::LEGACY_UNRESOLVED_TIME;
    }

    if (
        timeEncoding ==
        FirmwareLogic::RingRecordTimeEncoding::RESOLVED
    )
    {
        packet.epochSeconds =
            record.epochSeconds;

        packet.timestampResolved =
            true;
    }
    else
    {
        uint32_t recordSessionId =
            0;

        uint32_t captureMonotonicMs =
            0;

        if (
            timeEncoding !=
                FirmwareLogic::RingRecordTimeEncoding::PACKED_UNRESOLVED ||
            !FirmwareLogic::unpackOfflineCaptureMetadata(
                record.epochSeconds,
                recordSessionId,
                captureMonotonicMs
            )
        )
        {
            return RecordToPacketResult::INVALID_RECORD;
        }

        FirmwareLogic::TimeAnchor anchor;

        if (
            !findTimeAnchor(
                recordSessionId,
                anchor
            )
        )
        {
            if (
                recordSessionId ==
                    bootSessionId
            )
            {
                return RecordToPacketResult::WAITING_FOR_TIME_ANCHOR;
            }

            return RecordToPacketResult::PREVIOUS_SESSION_TIME_UNRESOLVABLE;
        }

        int64_t resolvedEpochMs =
            0;

        const FirmwareLogic::OfflineTimestampResult resolved =
            FirmwareLogic::resolveOfflineTimestampMs(
                recordSessionId,
                captureMonotonicMs,
                &anchor,
                resolvedEpochMs
            );

        if (
            resolved ==
            FirmwareLogic::OfflineTimestampResult::SESSION_MISMATCH
        )
        {
            return RecordToPacketResult::PREVIOUS_SESSION_TIME_UNRESOLVABLE;
        }

        if (
            resolved !=
            FirmwareLogic::OfflineTimestampResult::RESOLVED ||
            resolvedEpochMs <
                1700000000000LL
        )
        {
            return RecordToPacketResult::WAITING_FOR_TIME_ANCHOR;
        }

        packet.epochSeconds =
            static_cast<uint64_t>(
                resolvedEpochMs /
                1000LL
            );

        packet.timestampResolved =
            true;
    }

    packet.payload =
        createCanonicalTelemetryPayload(
            packet
        );

    if (
        packet.payload.length() ==
        0
    )
    {
        return RecordToPacketResult::INVALID_RECORD;
    }

    return RecordToPacketResult::READY;
}

// =====================================================
// Ring Queue
// =====================================================

enum class QueueReadResult
{
    READY,
    EMPTY,
    IO_ERROR,
    WAITING_FOR_TIME_ANCHOR,
    PREVIOUS_SESSION_TIME_UNRESOLVABLE,
    LEGACY_UNRESOLVED_TIME
};

bool queueIsEmpty()
{
    return (
        queueCount ==
        0
    );
}

bool queueIsFull()
{
    return (
        queueCount >=
        QUEUE_CAPACITY
    );
}

bool consumeHeadOrdinal(
    uint64_t ordinal,
    bool unresolvedTimestamp,
    uint32_t sequence,
    bool forceCommit
)
{
    if (
        queueIsEmpty() ||
        ordinal != ringHeadOrdinal
    )
    {
        return false;
    }

    ringHeadOrdinal =
        FirmwareLogic::calculateNextRingOrdinal(
            ringHeadOrdinal
        );

    queueCount--;
    qualityCollector.sampleBuffer(queueCount, droppedOldestCount);

    if (
        unresolvedTimestamp &&
        unresolvedTimestampCount > 0
    )
    {
        unresolvedTimestampCount--;
    }

    noteConsumedOrdinal(
        ordinal,
        forceCommit ||
            queueIsEmpty()
    );

    if (
        sequence > 0
    )
    {
        Serial.printf(
            "[RING] Consumed Sequence %lu (ordinal %llu)\n",
            static_cast<unsigned long>(
                sequence
            ),
            static_cast<unsigned long long>(
                ordinal
            )
        );
    }

    return true;
}

QueueReadResult readOldestPersistent(
    TelemetryPacket& packet,
    uint64_t& ordinal,
    BinaryTelemetryRecord& rawRecord
)
{
    while (
        !queueIsEmpty()
    )
    {
        ordinal =
            ringHeadOrdinal;

        const FirmwareLogic::RingReadClass readClass =
            readRingRecord(
                ordinal,
                rawRecord
            );

        if (
            readClass ==
            FirmwareLogic::RingReadClass::IO_ERROR
        )
        {
            Serial.printf(
                "[RING] Temporary/read I/O failure at ordinal %llu; queue preserved and replay paused.\n",
                static_cast<unsigned long long>(
                    ordinal
                )
            );

            return QueueReadResult::IO_ERROR;
        }

        if (
            readClass ==
            FirmwareLogic::RingReadClass::CORRUPT
        )
        {
            Serial.printf(
                "[RECOVERY] CRC/schema-corrupt ring ordinal %llu is unrecoverable and will be skipped.\n",
                static_cast<unsigned long long>(
                    ordinal
                )
            );

            if (
                !FirmwareLogic::shouldConsumeAfterReadFailure(
                    readClass
                ) ||
                !consumeHeadOrdinal(
                    ordinal,
                    false,
                    0,
                    true
                )
            )
            {
                return QueueReadResult::IO_ERROR;
            }

            continue;
        }

        const RecordToPacketResult conversion =
            recordToPacket(
                rawRecord,
                packet
            );

        if (
            conversion ==
            RecordToPacketResult::READY
        )
        {
            return QueueReadResult::READY;
        }

        if (
            conversion ==
            RecordToPacketResult::WAITING_FOR_TIME_ANCHOR
        )
        {
            return QueueReadResult::WAITING_FOR_TIME_ANCHOR;
        }

        if (
            conversion ==
            RecordToPacketResult::PREVIOUS_SESSION_TIME_UNRESOLVABLE
        )
        {
            return QueueReadResult::PREVIOUS_SESSION_TIME_UNRESOLVABLE;
        }

        if (
            conversion ==
            RecordToPacketResult::LEGACY_UNRESOLVED_TIME
        )
        {
            return QueueReadResult::LEGACY_UNRESOLVED_TIME;
        }

        // The file read succeeded but the record cannot satisfy the schema.
        // Treat this as durable corruption, not a transient I/O event.
        if (
            !consumeHeadOrdinal(
                ordinal,
                recordHasUnresolvedTimestamp(
                    rawRecord
                ),
                rawRecord.sequence,
                true
            )
        )
        {
            return QueueReadResult::IO_ERROR;
        }
    }

    return QueueReadResult::EMPTY;
}

bool enqueuePersistent(
    const TelemetryPacket& packet
)
{
    bool queueWasFull =
        queueIsFull();

    BinaryTelemetryRecord oldestRecord;
    bool oldestRecordValid =
        false;

    if (
        queueWasFull
    )
    {
        const FirmwareLogic::RingReadClass oldestRead =
            readRingRecord(
                ringHeadOrdinal,
                oldestRecord
            );

        if (
            oldestRead ==
            FirmwareLogic::RingReadClass::IO_ERROR
        )
        {
            Serial.println(
                "[BUFFER] Full ring oldest-read I/O failure; refusing append so existing telemetry remains intact."
            );

            return false;
        }

        if (
            oldestRead ==
            FirmwareLogic::RingReadClass::CORRUPT
        )
        {
            const uint64_t corruptOrdinal =
                ringHeadOrdinal;

            if (
                !consumeHeadOrdinal(
                    corruptOrdinal,
                    false,
                    0,
                    true
                )
            )
            {
                return false;
            }

            queueWasFull =
                false;
        }
        else
        {
            oldestRecordValid =
                true;
        }
    }

    uint64_t ordinal =
        ringNextOrdinal;

    if (
        ordinal == 0
    )
    {
        ordinal =
            1;
    }

    BinaryTelemetryRecord record =
        packetToRecord(
            packet,
            ordinal
        );

    // The logical queue leaves one physical slot spare. Even when full,
    // this write cannot overwrite the current oldest active slot.
    const bool writeVerified =
        writeRingRecord(
            record
        );

    if (
        !writeVerified
    )
    {
        Serial.println(
            "[BUFFER] New record write/read-back failed; oldest record was NOT consumed."
        );

        return false;
    }

    if (
        FirmwareLogic::shouldDropOldestAfterVerifiedWrite(
            queueWasFull,
            writeVerified
        )
    )
    {
        if (
            !oldestRecordValid ||
            !consumeHeadOrdinal(
                ringHeadOrdinal,
                recordHasUnresolvedTimestamp(
                    oldestRecord
                ),
                oldestRecord.sequence,
                true
            )
        )
        {
            // The new record is durable in the spare slot. Recovery will see
            // at most RING_SLOT_COUNT valid ordinals and retain the newest
            // QUEUE_CAPACITY window after reboot.
            Serial.println(
                "[BUFFER] New record is durable but oldest logical consume failed; reboot recovery will reconcile the spare-slot transaction."
            );

            return false;
        }

        droppedOldestCount++;

        const size_t dropWritten =
            preferences.putULong64(
                "ringDropped",
                droppedOldestCount
            );

        if (
            dropWritten != sizeof(uint64_t)
        )
        {
            Serial.println(
                "[BUFFER] Drop counter persistence failed; telemetry ordering remains safe."
            );
        }
    }

    if (
        recordHasUnresolvedTimestamp(
            record
        )
    )
    {
        unresolvedTimestampCount++;
    }

    if (
        queueCount == 0
    )
    {
        ringHeadOrdinal =
            ordinal;
    }

    ringNextOrdinal =
        FirmwareLogic::calculateNextRingOrdinal(
            ordinal
        );

    queueCount++;
    qualityCollector.sampleBuffer(queueCount, droppedOldestCount);

    Serial.printf(
        "[BUFFER] Queue : %u / %u | physical slots=%u | dropped=%llu\n",
        static_cast<unsigned int>(
            queueCount
        ),
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        ),
        static_cast<unsigned int>(
            RING_SLOT_COUNT
        ),
        static_cast<unsigned long long>(
            droppedOldestCount
        )
    );

    return true;
}

// =====================================================
// Ring Recovery
// =====================================================

bool restoreQueueFromFlash()
{
    queueCount = 0;
    ringHeadOrdinal = 0;
    ringNextOrdinal = 1;

    droppedOldestCount =
        preferences.getULong64(
            "ringDropped",
            0
        );

    committedConsumedOrdinal =
        preferences.getULong64(
            "ringConsumed",
            0
        );

    pendingConsumedOrdinal =
        committedConsumedOrdinal;

    pendingConsumedCount =
        0;

    unresolvedTimestampCount =
        0;

    if (
        !ensureRingFile()
    )
    {
        Serial.println(
            "[RING] Ring file initialization failed."
        );

        return false;
    }

    File file =
        LittleFS.open(
            RING_FILE,
            "r"
        );

    if (!file)
    {
        Serial.println(
            "[RECOVERY] Ring open I/O failure; refusing to mutate queue state."
        );

        return false;
    }

    uint64_t minimumActiveOrdinal =
        UINT64_MAX;

    uint64_t maximumActiveOrdinal =
        0;

    uint64_t maximumOrdinalSeen =
        committedConsumedOrdinal;

    size_t activeValidCount =
        0;

    size_t ignoredConsumedCount =
        0;

    size_t corruptCount =
        0;

    size_t legacyUnresolvedCount =
        0;

    for (
        size_t slot = 0;
        slot < RING_SLOT_COUNT;
        slot++
    )
    {
        BinaryTelemetryRecord record;

        const size_t bytes =
            file.read(
                reinterpret_cast<uint8_t*>(
                    &record
                ),
                sizeof(record)
            );

        if (
            bytes != sizeof(record)
        )
        {
            file.close();

            Serial.printf(
                "[RECOVERY] LittleFS read I/O failure at physical slot %u; recovery aborted without advancing watermark.\n",
                static_cast<unsigned int>(
                    slot
                )
            );

            queueCount = 0;
            ringHeadOrdinal = 0;
            ringNextOrdinal = 1;
            return false;
        }

        if (
            record.magic == 0
        )
        {
            continue;
        }

        if (
            !recordIsValid(
                record
            )
        )
        {
            corruptCount++;
            continue;
        }

        if (
            record.ordinal > maximumOrdinalSeen
        )
        {
            maximumOrdinalSeen =
                record.ordinal;
        }

        if (
            record.sequence > telemetrySequence
        )
        {
            telemetrySequence =
                record.sequence;
        }

        if (
            FirmwareLogic::shouldIgnoreRecoveredOrdinal(
                record.ordinal,
                committedConsumedOrdinal
            )
        )
        {
            ignoredConsumedCount++;
            continue;
        }

        activeValidCount++;

        if (
            recordHasUnresolvedTimestamp(
                record
            )
        )
        {
            unresolvedTimestampCount++;

            if (
                recordHasLegacyUnresolvedTimestamp(
                    record
                )
            )
            {
                legacyUnresolvedCount++;
            }
        }

        minimumActiveOrdinal =
            std::min(
                minimumActiveOrdinal,
                record.ordinal
            );

        maximumActiveOrdinal =
            std::max(
                maximumActiveOrdinal,
                record.ordinal
            );
    }

    file.close();

    ringNextOrdinal =
        FirmwareLogic::calculateNextRingOrdinal(
            maximumOrdinalSeen
        );

    if (
        activeValidCount > 0
    )
    {
        ringHeadOrdinal =
            FirmwareLogic::calculateRecoveryHeadOrdinal(
                minimumActiveOrdinal,
                maximumActiveOrdinal,
                QUEUE_CAPACITY
            );

        queueCount =
            FirmwareLogic::calculateRecoveredQueueCount(
                minimumActiveOrdinal,
                maximumActiveOrdinal,
                QUEUE_CAPACITY,
                activeValidCount
            );

        // A power cut may occur after a full-ring spare-slot write succeeds
        // but before the oldest-drop watermark is committed. In that state
        // all 25,000 physical slots are valid. The new record is already
        // durable, so recovery intentionally retains the newest 24,999 and
        // commits the now-safe oldest-drop boundary.
        if (
            ringHeadOrdinal > minimumActiveOrdinal
        )
        {
            pendingConsumedOrdinal =
                ringHeadOrdinal - 1ULL;

            pendingConsumedCount =
                ACK_WATERMARK_BATCH_SIZE;

            commitConsumedWatermark();
        }
    }

    if (
        !ensureCurrentSequenceFloorDurable()
    )
    {
        Serial.println(
            "[RECOVERY] Sequence floor is NOT durable. New packet creation remains blocked."
        );
    }

    Serial.printf(
        "[RECOVERY] Binary ring restored %u queued record(s).\n",
        static_cast<unsigned int>(
            queueCount
        )
    );

    Serial.printf(
        "[RECOVERY] Watermark ignored %u already-consumed valid slot(s).\n",
        static_cast<unsigned int>(
            ignoredConsumedCount
        )
    );

    Serial.printf(
        "[RECOVERY] Consumed watermark: %llu | next ordinal: %llu.\n",
        static_cast<unsigned long long>(
            committedConsumedOrdinal
        ),
        static_cast<unsigned long long>(
            ringNextOrdinal
        )
    );

    Serial.printf(
        "[RECOVERY] Active unresolved timestamps: %u.\n",
        static_cast<unsigned int>(
            unresolvedTimestampCount
        )
    );

    Serial.printf(
        "[RECOVERY] Legacy v1 unresolved records pending migration: %u.\n",
        static_cast<unsigned int>(
            legacyUnresolvedCount
        )
    );

    Serial.printf(
        "[RECOVERY] CRC/schema-invalid slots observed: %u.\n",
        static_cast<unsigned int>(
            corruptCount
        )
    );

    Serial.printf(
        "[RECOVERY] Logical capacity: %u | physical slots: %u | bytes: %llu.\n",
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        ),
        static_cast<unsigned int>(
            RING_SLOT_COUNT
        ),
        static_cast<unsigned long long>(
            ringFileSizeBytes()
        )
    );

    Serial.printf(
        "[RECOVERY] Oldest-drop count: %llu.\n",
        static_cast<unsigned long long>(
            droppedOldestCount
        )
    );

    return true;
}

// =====================================================
// P3: HTTP Classification
// =====================================================

bool responseHasErrorCode(
    const String& response,
    const char* errorCode
)
{
    if (
        errorCode == nullptr ||
        response.length() == 0
    )
    {
        return false;
    }

    String quoted =
        "\"" +
        String(errorCode) +
        "\"";

    return (
        response.indexOf(
            quoted
        ) >= 0
    );
}

bool validateAckResponse(
    const TelemetryPacket& packet,
    int statusCode,
    const String& response
)
{
    const FirmwareLogic::AckValidationResult validation =
        FirmwareLogic::validateAckJson(
            statusCode,
            response.c_str(),
            DEVICE_ID,
            packet.sequence
        );

    switch (
        validation
    )
    {
        case FirmwareLogic::AckValidationResult::VALID:
            return true;

        case FirmwareLogic::AckValidationResult::UNSUPPORTED_STATUS:
            Serial.printf(
                "[ACK-VALIDATION] Unsupported success status HTTP %d; telemetry preserved.\n",
                statusCode
            );
            break;

        case FirmwareLogic::AckValidationResult::MALFORMED_RESPONSE:
            Serial.println(
                "[ACK-VALIDATION] Missing or malformed ACK fields; telemetry preserved."
            );
            break;

        case FirmwareLogic::AckValidationResult::NOT_ACCEPTED:
            Serial.println(
                "[ACK-VALIDATION] accepted != true; telemetry preserved."
            );
            break;

        case FirmwareLogic::AckValidationResult::DEVICE_MISMATCH:
            Serial.println(
                "[ACK-VALIDATION] deviceId mismatch; telemetry preserved."
            );
            break;

        case FirmwareLogic::AckValidationResult::SEQUENCE_MISMATCH:
            Serial.printf(
                "[ACK-VALIDATION] sequence mismatch for expected Sequence %lu; telemetry preserved.\n",
                static_cast<unsigned long>(
                    packet.sequence
                )
            );
            break;

        case FirmwareLogic::AckValidationResult::DUPLICATE_CONTRACT_MISMATCH:
            Serial.printf(
                "[ACK-VALIDATION] HTTP %d duplicate contract mismatch; telemetry preserved.\n",
                statusCode
            );
            break;
    }

    return false;
}

PostResult classifyHttpOutcome(
    const TelemetryPacket& packet,
    int statusCode,
    const String& response
)
{
    // -------------------------------------------------
    // Validated success / idempotent duplicate
    //
    // Never consume a queued packet from HTTP status alone.
    // Only an ACK body matching DEVICE_ID + packet.sequence is success.
    // -------------------------------------------------
    if (
        statusCode >= 200 &&
        statusCode <= 299
    )
    {
        if (
            validateAckResponse(
                packet,
                statusCode,
                response
            )
        )
        {
            return PostResult::SUCCESS;
        }

        // A malformed/mismatched 2xx is a protocol/configuration fault,
        // not proof that this packet was safely accepted. Keep it queued.
        return PostResult::CONFIGURATION_ERROR;
    }

    // -------------------------------------------------
    // Temporary transport / server conditions
    // -------------------------------------------------
    if (
        statusCode < 0 ||
        statusCode == 408 ||
        statusCode == 425 ||
        statusCode == 429 ||
        (
            statusCode >= 500 &&
            statusCode <= 599
        )
    )
    {
        return PostResult::RETRYABLE;
    }

    // -------------------------------------------------
    // API v1.3 packet-specific permanent failures
    // -------------------------------------------------

    // Invalid telemetry/raw-only/body contract cannot be repaired by
    // sending the same payload again.
    if (
        statusCode == 400 ||
        statusCode == 413
    )
    {
        return PostResult::PERMANENT_PACKET_REJECT;
    }

    // The API explicitly requires ordinary ESP/MQTT non-null labels
    // to be isolated as a permanent error and not retried.
    if (
        statusCode == 403 &&
        responseHasErrorCode(
            response,
            "TELEMETRY_LABEL_FORBIDDEN"
        )
    )
    {
        return PostResult::PERMANENT_PACKET_REJECT;
    }

    // A reused (deviceId, sequence) with a different payload can never
    // succeed while replaying this same packet.
    if (
        statusCode == 409 &&
        responseHasErrorCode(
            response,
            "SEQUENCE_CONFLICT"
        )
    )
    {
        return PostResult::PERMANENT_PACKET_REJECT;
    }

    // -------------------------------------------------
    // Device/server configuration or authorization errors
    //
    // Preserve queued data instead of discarding it. These may become
    // valid after token, device registration, endpoint or mapping repair.
    // -------------------------------------------------
    if (
        statusCode == 401 ||
        statusCode == 403 ||
        statusCode == 404 ||
        statusCode == 405 ||
        statusCode == 415 ||
        (
            statusCode == 409 &&
            responseHasErrorCode(
                response,
                "DEVICE_MAPPING_MISMATCH"
            )
        ) ||
        (
            statusCode >= 300 &&
            statusCode <= 399
        )
    )
    {
        return PostResult::CONFIGURATION_ERROR;
    }

    // Unknown 409 or other 4xx are preserved rather than silently
    // discarded. Add a specific rule only after the API contract defines
    // the error as packet-permanent.
    if (
        statusCode >= 400 &&
        statusCode <= 499
    )
    {
        return PostResult::CONFIGURATION_ERROR;
    }

    return PostResult::RETRYABLE;
}


// =====================================================
// Bounded Atomic Isolation Archive
// =====================================================

String baseNameOfPath(
    const String& path
)
{
    const int slash =
        path.lastIndexOf('/');

    return (
        slash >= 0
            ? path.substring(
                  slash + 1
              )
            : path
    );
}

bool fileExistsQuiet(
    const String& absolutePath
)
{
    if (
        absolutePath.length() == 0
    )
    {
        return false;
    }

    String path =
        absolutePath;

    if (
        path[0] != '/'
    )
    {
        path =
            "/" +
            path;
    }

    const int slash =
        path.lastIndexOf('/');

    if (
        slash < 0 ||
        slash ==
            static_cast<int>(
                path.length() - 1
            )
    )
    {
        return false;
    }

    const String directoryPath =
        slash == 0
            ? String("/")
            : path.substring(
                  0,
                  slash
              );

    const String expectedName =
        path.substring(
            slash + 1
        );

    // Do not probe the target path with LittleFS.open(). ESP32 VFS logs an
    // error whenever the file does not exist. Enumerating the parent
    // directory keeps a normal archive miss genuinely quiet.
    File directory =
        LittleFS.open(
            directoryPath.c_str()
        );

    if (
        !directory ||
        !directory.isDirectory()
    )
    {
        if (directory)
        {
            directory.close();
        }

        return false;
    }

    File entry =
        directory.openNextFile();

    while (entry)
    {
        if (
            !entry.isDirectory()
        )
        {
            const String entryName =
                baseNameOfPath(
                    String(entry.name())
                );

            if (
                entryName ==
                expectedName
            )
            {
                entry.close();
                directory.close();
                return true;
            }
        }

        entry.close();
        entry =
            directory.openNextFile();
    }

    directory.close();
    return false;
}

void considerOldestArchiveCandidate(
    const String& name,
    String* candidates,
    size_t capacity,
    size_t& count
)
{
    if (
        candidates == nullptr ||
        capacity == 0
    )
    {
        return;
    }

    if (
        count < capacity
    )
    {
        candidates[count++] =
            name;
        return;
    }

    size_t newestCandidateIndex =
        0;

    for (
        size_t i = 1;
        i < count;
        i++
    )
    {
        if (
            candidates[i].compareTo(
                candidates[newestCandidateIndex]
            ) > 0
        )
        {
            newestCandidateIndex =
                i;
        }
    }

    if (
        name.compareTo(
            candidates[newestCandidateIndex]
        ) < 0
    )
    {
        candidates[newestCandidateIndex] =
            name;
    }
}

void sortArchiveCandidatesAscending(
    String* candidates,
    size_t count
)
{
    if (
        candidates == nullptr
    )
    {
        return;
    }

    for (
        size_t i = 0;
        i < count;
        i++
    )
    {
        for (
            size_t j = i + 1;
            j < count;
            j++
        )
        {
            if (
                candidates[j].compareTo(
                    candidates[i]
                ) < 0
            )
            {
                const String temporary =
                    candidates[i];

                candidates[i] =
                    candidates[j];

                candidates[j] =
                    temporary;
            }
        }
    }
}

bool ensureIsolationArchiveCapacity()
{
    File directory =
        LittleFS.open(
            REJECTED_DIR
        );

    if (
        !directory ||
        !directory.isDirectory()
    )
    {
        if (directory)
        {
            directory.close();
        }

        return false;
    }

    size_t entryCount =
        0;

    String oldestCandidates[
        MAX_ISOLATION_EVICTIONS_PER_PASS
    ];

    size_t oldestCandidateCount =
        0;

    String staleTempNames[
        MAX_STALE_TEMP_REMOVALS_PER_PASS
    ];

    size_t staleTempCount =
        0;

    // Scan exactly once. Removing files during traversal can invalidate
    // directory iteration on an embedded filesystem, so removals happen
    // only after the directory handle is closed.
    File entry =
        directory.openNextFile();

    while (entry)
    {
        if (
            !entry.isDirectory()
        )
        {
            const String name =
                baseNameOfPath(
                    String(entry.name())
                );

            if (
                name.endsWith(
                    ".tmp"
                )
            )
            {
                if (
                    staleTempCount <
                    MAX_STALE_TEMP_REMOVALS_PER_PASS
                )
                {
                    staleTempNames[
                        staleTempCount++
                    ] =
                        name;
                }
            }
            else
            {
                entryCount++;

                considerOldestArchiveCandidate(
                    name,
                    oldestCandidates,
                    MAX_ISOLATION_EVICTIONS_PER_PASS,
                    oldestCandidateCount
                );
            }
        }

        entry.close();
        entry =
            directory.openNextFile();
    }

    directory.close();

    for (
        size_t i = 0;
        i < staleTempCount;
        i++
    )
    {
        const String stalePath =
            String(REJECTED_DIR) +
            "/" +
            staleTempNames[i];

        if (
            LittleFS.remove(
                stalePath.c_str()
            )
        )
        {
            Serial.printf(
                "[ISOLATION] Removed stale temp file %s.\n",
                staleTempNames[i].c_str()
            );
        }
    }

    const size_t evictionCount =
        FirmwareLogic::calculateArchiveEvictionCount(
            entryCount,
            MAX_REJECTED_ARCHIVE_FILES,
            MAX_ISOLATION_EVICTIONS_PER_PASS
        );

    if (
        evictionCount == 0
    )
    {
        return true;
    }

    if (
        oldestCandidateCount <
        evictionCount
    )
    {
        return false;
    }

    sortArchiveCandidatesAscending(
        oldestCandidates,
        oldestCandidateCount
    );

    for (
        size_t i = 0;
        i < evictionCount;
        i++
    )
    {
        const String oldestPath =
            String(REJECTED_DIR) +
            "/" +
            oldestCandidates[i];

        if (
            !LittleFS.remove(
                oldestPath.c_str()
            )
        )
        {
            Serial.printf(
                "[ISOLATION] Archive cleanup could not remove %s; isolation write deferred.\n",
                oldestPath.c_str()
            );

            return false;
        }

        Serial.printf(
            "[ISOLATION] Archive cap=%u; evicted oldest diagnostic file %s.\n",
            static_cast<unsigned int>(
                MAX_REJECTED_ARCHIVE_FILES
            ),
            oldestCandidates[i].c_str()
        );
    }

    const size_t remainingEntryCount =
        entryCount -
        evictionCount;

    if (
        remainingEntryCount >=
        MAX_REJECTED_ARCHIVE_FILES
    )
    {
        // A large legacy archive is cleaned incrementally. Returning false
        // is safe: the unresolved/rejected packet remains durable and a
        // later loop performs the next bounded cleanup pass.
        Serial.printf(
            "[ISOLATION] Cleanup pass bounded at %u eviction(s); %u committed file(s) remain. "
            "Isolation write deferred so sensor acquisition can continue.\n",
            static_cast<unsigned int>(
                evictionCount
            ),
            static_cast<unsigned int>(
                remainingEntryCount
            )
        );

        return false;
    }

    return true;
}

void printJsonEscaped(
    File& file,
    const String& value
)
{
    for (
        size_t i = 0;
        i < value.length();
        i++
    )
    {
        const char c =
            value[i];

        switch (c)
        {
            case '"':
                file.print("\\\"");
                break;

            case '\\':
                file.print("\\\\");
                break;

            case '\n':
                file.print("\\n");
                break;

            case '\r':
                file.print("\\r");
                break;

            case '\t':
                file.print("\\t");
                break;

            default:
                if (
                    static_cast<uint8_t>(c) < 0x20
                )
                {
                    file.print('?');
                }
                else
                {
                    file.print(c);
                }
                break;
        }
    }
}

bool writeIsolationEnvelopeAtomic(
    uint32_t sequence,
    const char* reason,
    const String* canonicalPayload,
    const PostOutcome* outcome,
    const BinaryTelemetryRecord* unresolvedRecord
)
{
    if (
        TEST_FORCE_ISOLATION_WRITE_FAIL
    )
    {
        Serial.println(
            "[TEST][ISOLATION] Forced archive write failure."
        );

        return false;
    }

    const String finalPath =
        rejectedPacketFilePath(
            sequence
        );

    const String tempPath =
        rejectedTempFilePath(
            sequence
        );

    Serial.printf(
        "[ISOLATION] Begin Sequence %lu.\n",
        static_cast<unsigned long>(sequence)
    );

    // A fixed-slot temp path is safe to replace. Avoid a full parent-directory
    // scan here; on-device LittleFS traversal can block replay for a long time.
    LittleFS.remove(
        tempPath.c_str()
    );

    File file =
        LittleFS.open(
            tempPath.c_str(),
            FILE_WRITE
        );

    if (!file)
    {
        return false;
    }

    Serial.println("[ISOLATION] Temp file opened.");

    file.print("{\n  \"sequence\": ");
    file.print(sequence);
    file.print(",\n  \"reason\": \"");
    printJsonEscaped(
        file,
        String(reason != nullptr ? reason : "unknown")
    );
    file.print("\",");

    file.print("\n  \"recordedAt\": \"");
    const String timestamp =
        getTimestamp();
    printJsonEscaped(
        file,
        timestamp.length() > 0
            ? timestamp
            : String("time-unavailable")
    );
    file.print("\"");

    if (
        outcome != nullptr
    )
    {
        file.print(",\n  \"httpStatus\": ");
        file.print(outcome->statusCode);
        file.print(",\n  \"backendResponse\": \"");

        const String boundedResponse =
            outcome->response.substring(
                0,
                MAX_ISOLATION_BACKEND_RESPONSE_CHARS
            );

        printJsonEscaped(
            file,
            boundedResponse
        );
        file.print("\"");
    }

    if (
        canonicalPayload != nullptr &&
        canonicalPayload->length() > 0
    )
    {
        file.print(",\n  \"telemetry\": ");
        file.print(
            *canonicalPayload
        );
    }

    if (
        unresolvedRecord != nullptr
    )
    {
        const bool legacyV1 =
            recordHasLegacyUnresolvedTimestamp(
                *unresolvedRecord
            );

        uint32_t sessionId = 0;
        uint32_t captureMonotonicMs = 0;

        if (!legacyV1)
        {
            FirmwareLogic::unpackOfflineCaptureMetadata(
                unresolvedRecord->epochSeconds,
                sessionId,
                captureMonotonicMs
            );
        }

        file.print(",\n  \"unresolvedCapture\": {");
        file.print("\n    \"ringSchemaVersion\": ");
        file.print(unresolvedRecord->schemaVersion);
        file.print(",\n    \"legacyV1\": ");
        file.print(
            legacyV1
                ? "true"
                : "false"
        );
        file.print(",\n    \"sessionId\": ");

        if (legacyV1)
        {
            file.print("null");
        }
        else
        {
            file.print(sessionId);
        }

        file.print(",\n    \"captureMonotonicMs\": ");

        if (legacyV1)
        {
            file.print("null");
        }
        else
        {
            file.print(captureMonotonicMs);
        }

        file.print(",\n    \"vibrationRmsRaw\": ");
        file.print(unresolvedRecord->vibrationRmsRaw, 6);
        file.print(",\n    \"vibrationPeakHz\": ");
        file.print(unresolvedRecord->vibrationPeakHz, 2);
        file.print(",\n    \"acousticRmsRaw\": ");
        file.print(unresolvedRecord->acousticRmsRaw, 2);
        file.print(",\n    \"acousticPeakHz\": ");
        file.print(unresolvedRecord->acousticPeakHz, 2);
        file.print("\n  }");
    }

    file.print("\n}\n");
    file.flush();

    Serial.println("[ISOLATION] Temp file flushed.");

    const bool writeError =
        file.getWriteError() != 0;

    file.close();

    if (writeError)
    {
        LittleFS.remove(
            tempPath.c_str()
        );
        return false;
    }

    File verify =
        LittleFS.open(
            tempPath.c_str(),
            FILE_READ
        );

    if (
        !verify ||
        verify.size() == 0
    )
    {
        if (verify)
        {
            verify.close();
        }

        LittleFS.remove(
            tempPath.c_str()
        );
        return false;
    }

    Serial.println("[ISOLATION] Temp file verified.");

    verify.close();

    // Fixed-slot archive: the new temp file is fully written and verified
    // before replacing the previous diagnostic occupying this slot.
    //
    // Power-cut safety does NOT depend on this rename being atomic: every
    // caller now owns a durable ring source and consumes it only after this
    // function reports a verified successful replacement. Therefore a cut
    // after destination deletion but before rename cannot lose telemetry.
    //
    // Replace the fixed slot directly. Avoid another full directory scan;
    // missing destinations are harmless and rename remains the commit point.
    LittleFS.remove(
        finalPath.c_str()
    );

    if (
        !LittleFS.rename(
            tempPath.c_str(),
            finalPath.c_str()
        )
    )
    {
        LittleFS.remove(
            tempPath.c_str()
        );
        return false;
    }

    Serial.println("[ISOLATION] Temp file renamed.");

    Serial.printf(
        "[ISOLATION] Stored Sequence %lu in bounded slot %lu/%u.\n",
        static_cast<unsigned long>(sequence),
        static_cast<unsigned long>(
            sequence %
            static_cast<uint32_t>(MAX_REJECTED_ARCHIVE_FILES)
        ),
        static_cast<unsigned int>(MAX_REJECTED_ARCHIVE_FILES)
    );

    return true;
}

bool saveImmediateRejectedPacket(
    const TelemetryPacket& packet,
    const PostOutcome& outcome
)
{
    const bool saved =
        writeIsolationEnvelopeAtomic(
            packet.sequence,
            "PERMANENT_PACKET_REJECT",
            &packet.payload,
            &outcome,
            nullptr
        );

    if (saved)
    {
        Serial.printf(
            "[REJECT] Isolated Sequence %lu (HTTP %d).\n",
            static_cast<unsigned long>(
                packet.sequence
            ),
            outcome.statusCode
        );
    }

    return saved;
}

bool saveUnresolvedRecordToIsolation(
    const BinaryTelemetryRecord& record
)
{
    return writeIsolationEnvelopeAtomic(
        record.sequence,
        "UTC_UNRESOLVABLE_AFTER_REBOOT",
        nullptr,
        nullptr,
        &record
    );
}

bool saveLegacyUnresolvedRecordToIsolation(
    const BinaryTelemetryRecord& record
)
{
    return writeIsolationEnvelopeAtomic(
        record.sequence,
        "LEGACY_V1_UNRESOLVED_TIMESTAMP",
        nullptr,
        nullptr,
        &record
    );
}

bool moveQueuedPacketToRejected(
    const TelemetryPacket& packet,
    const PostOutcome& outcome
)
{
    return saveImmediateRejectedPacket(
        packet,
        outcome
    );
}

// =====================================================
// HTTP POST
// =====================================================

PostOutcome postPacket(
    const TelemetryPacket& packet,
    bool replay = false
)
{
    PostOutcome outcome;

    if (
        WiFi.status() !=
        WL_CONNECTED
    )
    {
        outcome.result =
            PostResult::RETRYABLE;

        outcome.statusCode =
            -1;

        outcome.response =
            "wifi-disconnected";

        return outcome;
    }

    WiFiClientSecure secureClient;
    BackendHttp http;

    http.setConnectTimeout(
        3000
    );

    http.setTimeout(
        3000
    );

    if (
        !beginBackendHttp(
            http,
            secureClient,
            INGEST_URL
        )
    )
    {
        outcome.result =
            PostResult::CONFIGURATION_ERROR;

        outcome.statusCode =
            -1;

        outcome.response =
            "backend-transport-policy-rejected";

        qualityCollector.attempt(packet.sequence, replay,
            CommunicationQuality::Outcome::CONFIGURATION_FAILURE, 0);
        return outcome;
    }

    http.addHeader(
        "Content-Type",
        "application/json"
    );

    http.addHeader(
        "Authorization",
        String("Bearer ") +
            INGEST_TOKEN
    );

    Serial.println();
    Serial.println(
        "========== POST =========="
    );

    Serial.printf(
        "Sequence : %lu\n",
        static_cast<unsigned long>(
            packet.sequence
        )
    );

    Serial.printf(
        "Queue    : %u / %u\n",
        static_cast<unsigned int>(
            queueCount
        ),
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        )
    );

    const uint64_t qualityStartedMs = static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;
    const int statusCode =
        http.POST(
            packet.payload
        );

    outcome.statusCode =
        statusCode;

    if (
        statusCode > 0
    )
    {
        outcome.response =
            http.getString();
    }
    else
    {
        outcome.response =
            http.errorToString(
                statusCode
            );
    }

    outcome.result = classifyHttpOutcome(packet, statusCode, outcome.response);
    using QualityOutcome = CommunicationQuality::Outcome;
    const QualityOutcome qualityOutcome = outcome.result == PostResult::SUCCESS ? QualityOutcome::ACK :
        outcome.result == PostResult::PERMANENT_PACKET_REJECT ? QualityOutcome::PACKET_REJECT :
        outcome.result == PostResult::CONFIGURATION_ERROR ? QualityOutcome::CONFIGURATION_FAILURE :
        statusCode <= 0 ? QualityOutcome::TRANSPORT_FAILURE : QualityOutcome::RETRYABLE_RESPONSE;
    qualityCollector.attempt(packet.sequence, replay, qualityOutcome,
        static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL - qualityStartedMs);

    Serial.printf(
        "HTTP     : %d\n",
        statusCode
    );

    if (
        outcome.response.length() > 0
    )
    {
        Serial.println(
            outcome.response
        );
    }

    http.end();

    return outcome;
}

// =====================================================
// P1 + P3: Replay
// =====================================================

void replayQueueBatch()
{
    if (
        queueIsEmpty() ||
        WiFi.status() !=
            WL_CONNECTED
    )
    {
        return;
    }

    Serial.println();

    Serial.printf(
        "[BUFFER] Persistent Replay Batch Started (max %u record(s))\n",
        static_cast<unsigned int>(
            REPLAY_MAX_RECORDS_PER_LOOP
        )
    );

    size_t consumedThisBatch =
        0;

    const size_t replayLimit =
        FirmwareLogic::calculateReplayBatchSize(
            queueCount,
            REPLAY_MAX_RECORDS_PER_LOOP
        );

    while (
        !queueIsEmpty() &&
        consumedThisBatch <
            replayLimit
    )
    {
        TelemetryPacket packet;
        BinaryTelemetryRecord rawRecord;

        uint64_t ordinal =
            0;

        const QueueReadResult readResult =
            readOldestPersistent(
                packet,
                ordinal,
                rawRecord
            );

        if (
            readResult ==
            QueueReadResult::EMPTY
        )
        {
            commitConsumedWatermark();
            return;
        }

        if (
            readResult ==
            QueueReadResult::IO_ERROR
        )
        {
            // IMPORTANT: no ACK, no corruption proof, no watermark advance.
            Serial.println(
                "[BUFFER] Replay paused on LittleFS I/O error; oldest record remains queued."
            );

            commitConsumedWatermark();
            return;
        }

        if (
            readResult ==
            QueueReadResult::WAITING_FOR_TIME_ANCHOR
        )
        {
            Serial.println(
                "[BUFFER] Replay waiting for a durable timestamp anchor for this boot session."
            );

            commitConsumedWatermark();
            return;
        }

        if (
            readResult ==
            QueueReadResult::LEGACY_UNRESOLVED_TIME
        )
        {
            Serial.printf(
                "[MIGRATION] Sequence %lu uses legacy schema-v1 unresolved timestamp encoding; preserving raw capture in isolation before consuming the ring source.\n",
                static_cast<unsigned long>(
                    rawRecord.sequence
                )
            );

            if (
                !saveLegacyUnresolvedRecordToIsolation(
                    rawRecord
                )
            )
            {
                Serial.println(
                    "[MIGRATION] Legacy isolation write failed; source record remains in ring."
                );

                commitConsumedWatermark();
                return;
            }

            if (
                !consumeHeadOrdinal(
                    ordinal,
                    true,
                    rawRecord.sequence,
                    false
                )
            )
            {
                commitConsumedWatermark();
                return;
            }

            consumedThisBatch++;
            continue;
        }

        if (
            readResult ==
            QueueReadResult::PREVIOUS_SESSION_TIME_UNRESOLVABLE
        )
        {
            Serial.printf(
                "[TIME] Sequence %lu belongs to a boot session with no durable UTC anchor; preserving raw capture without inventing UTC.\n",
                static_cast<unsigned long>(
                    rawRecord.sequence
                )
            );

            if (
                !saveUnresolvedRecordToIsolation(
                    rawRecord
                )
            )
            {
                Serial.println(
                    "[TIME] Isolation write failed; unresolved record remains in ring."
                );

                commitConsumedWatermark();
                return;
            }

            if (
                !consumeHeadOrdinal(
                    ordinal,
                    true,
                    rawRecord.sequence,
                    false
                )
            )
            {
                commitConsumedWatermark();
                return;
            }

            consumedThisBatch++;
            continue;
        }

        const auto order = DeviceHealth::dispatchOrder(
            healthJournal, true, packet.timestampResolved,
            static_cast<int64_t>(packet.epochSeconds) * 1000LL,
            bootSessionId, healthUptimeMs(), currentHealthEpochMs());
        if (!order.telemetry)
        {
            // The next health transition must commit before this later point.
            // Re-evaluate for EVERY head, not just once per replay batch.
            commitConsumedWatermark();
            return;
        }

        const uint32_t sequence =
            packet.sequence;

        Serial.printf(
            "[BUFFER] Replaying Sequence %lu (ring ordinal %llu)\n",
            static_cast<unsigned long>(
                sequence
            ),
            static_cast<unsigned long long>(
                ordinal
            )
        );

        const PostOutcome outcome =
            postPacket(
                packet,
                true
            );

        if (
            outcome.result ==
            PostResult::SUCCESS
        )
        {
            Serial.printf(
                "[BUFFER] ACK Sequence %lu\n",
                static_cast<unsigned long>(
                    sequence
                )
            );

            if (
                !consumeHeadOrdinal(
                    ordinal,
                    recordHasUnresolvedTimestamp(
                        rawRecord
                    ),
                    sequence,
                    false
                )
            )
            {
                commitConsumedWatermark();
                return;
            }

            consumedThisBatch++;
            delay(100);
            continue;
        }

        if (
            outcome.result ==
            PostResult::RETRYABLE
        )
        {
            Serial.printf(
                "[BUFFER] Retryable failure at Sequence %lu; replay paused.\n",
                static_cast<unsigned long>(
                    sequence
                )
            );

            commitConsumedWatermark();
            return;
        }

        if (
            outcome.result ==
            PostResult::CONFIGURATION_ERROR
        )
        {
            Serial.printf(
                "[BUFFER] Configuration/protocol error at Sequence %lu (HTTP %d); queue preserved.\n",
                static_cast<unsigned long>(
                    sequence
                ),
                outcome.statusCode
            );

            commitConsumedWatermark();
            return;
        }

        if (
            outcome.result ==
            PostResult::PERMANENT_PACKET_REJECT
        )
        {
            Serial.printf(
                "[BUFFER] Permanent packet reject Sequence %lu (HTTP %d).\n",
                static_cast<unsigned long>(
                    sequence
                ),
                outcome.statusCode
            );

            if (
                !moveQueuedPacketToRejected(
                    packet,
                    outcome
                )
            )
            {
                // Archive failure must never convert a permanent backend
                // reject into telemetry loss. Keep the ring head untouched.
                Serial.println(
                    "[BUFFER] Rejected archive write failed; ring record remains queued."
                );

                commitConsumedWatermark();
                return;
            }

            if (
                !consumeHeadOrdinal(
                    ordinal,
                    recordHasUnresolvedTimestamp(
                        rawRecord
                    ),
                    sequence,
                    false
                )
            )
            {
                commitConsumedWatermark();
                return;
            }

            consumedThisBatch++;
        }
    }

    if (
        queueIsEmpty()
    )
    {
        Serial.println(
            "[BUFFER] Persistent Replay Completed."
        );

        return;
    }

    Serial.printf(
        "[BUFFER] Replay batch finished after %u record(s); %u remain. Returning to sensor acquisition.\n",
        static_cast<unsigned int>(
            consumedThisBatch
        ),
        static_cast<unsigned int>(
            queueCount
        )
    );
}

// =====================================================
// Packet Creation
// =====================================================

bool createPacket(
    const VibrationFeatures& vib,
    const AcousticFeatures& audio,
    TelemetryPacket& packet
)
{
    uint32_t allocatedSequence =
        0;

    if (
        !allocateSequence(
            allocatedSequence
        )
    )
    {
        return false;
    }

    packet.sequence =
        allocatedSequence;

    packet.vibrationRmsRaw =
        FirmwareLogic::canonicalizeMeasurement(
            vib.totalRms
        );

    packet.vibrationPeakHz =
        FirmwareLogic::canonicalizeMeasurement(
            vib.peakHz
        );

    packet.acousticRmsRaw =
        FirmwareLogic::canonicalizeMeasurement(
            audio.rmsRaw
        );

    packet.acousticPeakHz =
        FirmwareLogic::canonicalizeMeasurement(
            audio.peakHz
        );

    time_t now =
        time(nullptr);

    if (
        timeReady &&
        now >=
            1700000000
    )
    {
        packet.timestampResolved =
            true;

        packet.epochSeconds =
            static_cast<uint64_t>(
                now
            );

        packet.payload =
            createCanonicalTelemetryPayload(
                packet
            );

        return (
            packet.payload.length() >
            0
        );
    }

    // True offline cold boot: absolute UTC is unknowable without an RTC.
    // Persist the exact boot session + capture millis() instead of inventing
    // a nominal 3.99 s cadence. If this same session later receives UTC,
    // the durable session anchor reconstructs the timestamp from the actual
    // monotonic delta. If power is lost first, the record is retained for
    // isolation rather than assigned a fabricated time.
    if (
        !bootSessionReady
    )
    {
        return false;
    }

    packet.timestampResolved =
        false;

    packet.epochSeconds =
        0;

    packet.offlineSessionId =
        bootSessionId;

    packet.captureMonotonicMs =
        millis();

    packet.payload =
        "";

    return true;
}

// =====================================================
// LittleFS
// =====================================================

bool initPersistentStorage()
{
    Serial.println(
        "[FLASH] Mounting LittleFS without automatic format..."
    );

    bool mounted =
        false;

    for (
        uint8_t attempt = 1;
        attempt <=
            LITTLEFS_MOUNT_ATTEMPTS;
        attempt++
    )
    {
        if (
            !TEST_FORCE_LITTLEFS_MOUNT_FAIL &&
            LittleFS.begin(
                false
            )
        )
        {
            mounted =
                true;

            break;
        }

        Serial.printf(
            "[FLASH] LittleFS mount attempt %u/%u failed. Backlog is preserved; no format will be performed.\n",
            static_cast<unsigned int>(
                attempt
            ),
            static_cast<unsigned int>(
                LITTLEFS_MOUNT_ATTEMPTS
            )
        );

        delay(250);
    }

    if (
        !mounted
    )
    {
        Serial.println(
            "[FLASH] Mount failed after retries. Refusing automatic format to protect existing backlog."
        );

        return false;
    }

    if (
        !LittleFS.exists(
            REJECTED_DIR
        ) &&
        !LittleFS.mkdir(
            REJECTED_DIR
        )
    )
    {
        return false;
    }

    if (
        !ensureRingFile()
    )
    {
        return false;
    }

    Serial.printf(
        "[FLASH] Total : %llu bytes\n",
        static_cast<unsigned long long>(
            LittleFS.totalBytes()
        )
    );

    Serial.printf(
        "[FLASH] Used  : %llu bytes\n",
        static_cast<unsigned long long>(
            LittleFS.usedBytes()
        )
    );

    return true;
}

// =====================================================
// Setup
// =====================================================

// Device-health outbox is independent of the telemetry ring and sequence.
// Only the main task mutates it or Preferences; sensor tasks expose observations.
uint64_t healthUptimeMs()
{
    return static_cast<uint64_t>(esp_timer_get_time()) / 1000ULL;
}

int64_t currentHealthEpochMs()
{
    if (!timeReady) return 0;
    timeval now;
    gettimeofday(&now, nullptr);
    return static_cast<int64_t>(now.tv_sec) * 1000LL + now.tv_usec / 1000;
}

bool persistHealthJournal(const DeviceHealth::Journal& candidate)
{
    if (!DeviceHealth::validJournal(candidate)) return false;
    const size_t written = preferences.putBytes("healthJournal", &candidate, sizeof(candidate));
    DeviceHealth::Journal readback;
    return written == sizeof(candidate) &&
           preferences.getBytes("healthJournal", &readback, sizeof(readback)) == sizeof(readback) &&
           DeviceHealth::validJournal(readback) && memcmp(&candidate, &readback, sizeof(candidate)) == 0;
}

RemoteConfig::Controller remoteConfiguration;
RemoteConfig::Schedule remoteConfigSchedule;
RemoteConfig::Result pendingConfigResult;
const RemoteConfig::Identity remoteConfigIdentity{DEVICE_ID, SITE_ID, ASSET_ID};
bool remoteConfigStorageUsable = true;

// Separate bounded outbox; never changes the existing 48-byte telemetry ring.
#ifndef EDGE_ANALYSIS_ENABLED_VALUE
#define EDGE_ANALYSIS_ENABLED_VALUE false
#endif
struct AnalysisHeader { uint32_t magic = 0x45414631, sequence = 0, bytes = 0, crc = 0; };
constexpr unsigned ANALYSIS_SLOTS = 8;
EdgeAnalysis::PendingRequest analysisRequest;
uint32_t analysisLastAttempt = 0;
uint32_t analysisLastCapture = 0;
bool analysisCaptured = false;
bool analysisAttempted = false, analysisStorageUsable = true;
String analysisPath(unsigned slot) { return String("/analysis-") + slot + ".bin"; }

bool readAnalysis(unsigned slot, AnalysisHeader& header, std::string& body)
{
    File file = LittleFS.open(analysisPath(slot), "r");
    if (!file || file.read(reinterpret_cast<uint8_t*>(&header), sizeof(header)) != sizeof(header) ||
        header.magic != 0x45414631 || !header.sequence || !header.bytes || header.bytes > EdgeAnalysis::MAX_FRAME_BYTES ||
        file.size() != sizeof(header) + header.bytes) return false;
    body.resize(header.bytes);
    return file.read(reinterpret_cast<uint8_t*>(&body[0]), header.bytes) == header.bytes &&
        EdgeAnalysis::checksum(body.data(), body.size()) == header.crc;
}

void preserveAnalysis(const TelemetryPacket& packet)
{
#if CONTINUOUS_VIBRATION_ENABLED
    // Legacy raw arrays belong to the old on-demand collector, not this stream.
    // Do not upload stale arrays as a newly measured waveform.
    return;
#endif
    if (!EDGE_ANALYSIS_ENABLED_VALUE || !analysisStorageUsable || !packet.timestampResolved || !psramFound()) return;
    timeval now{};
    gettimeofday(&now, nullptr);
    const char* request = analysisRequest.forCapture(packet.epochSeconds, static_cast<double>(now.tv_sec) + now.tv_usec / 1000000.0);
    // Analysis is a sampled 30-second stream, not the realtime summary channel.
    // A pending operator request captures the next valid window immediately.
    if (!request && analysisCaptured && millis() - analysisLastCapture < 30000U) return;
    unsigned slot = 0;
    while (slot < ANALYSIS_SLOTS && LittleFS.exists(analysisPath(slot))) ++slot;
    if (slot == ANALYSIS_SLOTS) {
        Serial.println("[ANALYSIS] Outbox full; existing captures retained, new analysis window not stored.");
        return;
    }
    const std::string body = EdgeAnalysis::frame(packet.payload.c_str(), vibX, vibY, vibZ, commonAudioWindow, request);
    if (body.empty()) return;
    AnalysisHeader header;
    header.sequence = packet.sequence; header.bytes = body.size();
    header.crc = EdgeAnalysis::checksum(body.data(), body.size());
    const String pending = analysisPath(slot) + ".tmp";
    File file = LittleFS.open(pending, "w");
    if (!file) return;
    const bool written = file.write(reinterpret_cast<const uint8_t*>(&header), sizeof(header)) == sizeof(header) &&
        file.write(reinterpret_cast<const uint8_t*>(body.data()), body.size()) == body.size();
    file.flush(); file.close();
    if (!written || !LittleFS.rename(pending, analysisPath(slot))) return;
    AnalysisHeader verified; std::string restored;
    if (!readAnalysis(slot, verified, restored) || restored != body) {
        analysisStorageUsable = false;
        Serial.println("[ANALYSIS] Verification failed; capture preserved, channel disabled.");
        return;
    }
    analysisRequest.clear(); // The request identity now lives in the durable outbox.
    analysisCaptured = true; analysisLastCapture = millis();
}

void serviceEdgeAnalysis()
{
#if CONTINUOUS_VIBRATION_ENABLED
    // Old raw-capture ownership is incompatible with the continuous FIFO owner.
    // Preserve files, but do not allocate/serve legacy raw captures in this mode.
    return;
#endif
    if (!EDGE_ANALYSIS_ENABLED_VALUE || !analysisStorageUsable || !psramFound() || WiFi.status() != WL_CONNECTED || !timeReady ||
        (analysisAttempted && millis() - analysisLastAttempt < 5000U)) return;
    const String ingest = INGEST_URL;
    const String suffix = "/api/telemetry/ingest";
    if (!ingest.endsWith(suffix)) return;
    const String endpoint = ingest.substring(0, ingest.length()-suffix.length()) + "/api/devices/" + DEVICE_ID + "/analysis";
    analysisAttempted = true; analysisLastAttempt = millis();
    unsigned head = ANALYSIS_SLOTS; AnalysisHeader selected; std::string body;
    for (unsigned i=0; i<ANALYSIS_SLOTS; ++i) {
        if (!LittleFS.exists(analysisPath(i))) continue;
        AnalysisHeader header; std::string candidate;
        if (!readAnalysis(i,header,candidate)) {
            analysisStorageUsable = false;
            Serial.println("[ANALYSIS] Corrupt/read-failed capture retained; channel disabled.");
            return;
        }
        if (head == ANALYSIS_SLOTS || header.sequence < selected.sequence) {head=i; selected=header; body=std::move(candidate);}
    }
    WiFiClientSecure secure; BackendHttp http;
    secure.setHandshakeTimeout(3); http.setConnectTimeout(1500); http.setTimeout(1500);
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
    // Drain exactly one saved frame before polling a new capture request.
    if (!beginBackendHttp(http,secure,(endpoint+(head==ANALYSIS_SLOTS?"/pending":"")).c_str())) return;
    http.addHeader("Authorization", String("Bearer ") + INGEST_TOKEN);
    int status;
    if (head == ANALYSIS_SLOTS) status=http.GET();
    else {
        http.addHeader("Content-Type","application/json");
        status=http.POST(reinterpret_cast<uint8_t*>(const_cast<char*>(body.data())),body.size());
    }
    const int length=http.getSize(); String response;
    if (length>=0 && length<=2048) response=http.getString();
    if (length==static_cast<int>(response.length())) {
        if (head != ANALYSIS_SLOTS) {
            if (EdgeAnalysis::accepted(status,response.c_str(),selected.sequence)) LittleFS.remove(analysisPath(head));
        } else if (status==200) {
            analysisRequest.load(response.c_str(), DEVICE_ID, SITE_ID, ASSET_ID);
        }
    }
    http.end();
}

void activateRemoteConfiguration()
{
    // The main loop is the sole reader/writer; no sensor window is in progress
    // here. Sampling frequency, packet format and queued records never change.
    MEASUREMENT_INTERVAL_MS = remoteConfiguration.active().measurementIntervalMs;
    REPLAY_MAX_RECORDS_PER_LOOP = remoteConfiguration.active().replayBatchSize;
    healthSchedule.setInterval(remoteConfiguration.active().healthReportIntervalMs);
}

bool persistRemoteConfiguration(const RemoteConfig::Blob& candidate, void*)
{
    if (!remoteConfigStorageUsable || !RemoteConfig::validBlob(candidate, remoteConfigIdentity)) return false;
    RemoteConfig::Blob readback;
    return preferences.putBytes("remoteConfigV2", &candidate, sizeof(candidate)) == sizeof(candidate) &&
        preferences.getBytesLength("remoteConfigV2") == sizeof(readback) &&
        preferences.getBytes("remoteConfigV2", &readback, sizeof(readback)) == sizeof(readback) &&
        RemoteConfig::validBlob(readback, remoteConfigIdentity) &&
        memcmp(&candidate, &readback, sizeof(candidate)) == 0;
}

void initializeRemoteConfiguration()
{
    if (!preferences.isKey("remoteConfigV2") && preferences.isKey("remoteConfig"))
    {
        RemoteConfig::LegacyBlob legacy;
        if (preferences.getBytesLength("remoteConfig") != sizeof(legacy) ||
            preferences.getBytes("remoteConfig", &legacy, sizeof(legacy)) != sizeof(legacy) ||
            !remoteConfiguration.restoreLegacy(legacy, remoteConfigIdentity) ||
            !persistRemoteConfiguration(remoteConfiguration.active(), nullptr))
        {
            remoteConfigStorageUsable = false;
            Serial.println("[CONFIG] Legacy migration failed; original value retained, channel disabled.");
            return;
        }
    }
    if (preferences.isKey("remoteConfigV2"))
    {
        RemoteConfig::Blob stored;
        if (preferences.getBytesLength("remoteConfigV2") != sizeof(stored) ||
            preferences.getBytes("remoteConfigV2", &stored, sizeof(stored)) != sizeof(stored) ||
            !remoteConfiguration.restore(stored, remoteConfigIdentity))
        {
            // Preserve the blob and sequence/telemetry state. Never lower the
            // version floor and accept an older command after corrupt storage.
            remoteConfigStorageUsable = false;
            Serial.println("[CONFIG] Invalid/mismatched stored configuration; channel disabled, safe defaults retained. Local repair required.");
            return;
        }
        activateRemoteConfiguration();
        pendingConfigResult = remoteConfiguration.appliedResult();
        Serial.printf("[CONFIG] Restored version %lu.\n", static_cast<unsigned long>(remoteConfiguration.active().version));
    }
}

bool remoteConfigurationProvisioned()
{
    const size_t length = strlen(DEVICE_CONFIG_TOKEN);
    if (length < 32 || length > 128) return false;
    for (const char* p = DEVICE_CONFIG_TOKEN; *p; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '_' || *p == '-')) return false;
    // The base URL is local provisioning, never supplied by a remote response.
    const String suffix = String("/api/devices/") + DEVICE_ID + "/configuration";
    return *DEVICE_CONFIG_URL && String(DEVICE_CONFIG_URL).endsWith(suffix);
}

void serviceRemoteConfiguration()
{
    if (!remoteConfigStorageUsable || !remoteConfigurationProvisioned() ||
        WiFi.status() != WL_CONNECTED || !timeReady || !remoteConfigSchedule.due(millis())) return;
    const bool reporting = pendingConfigResult.status != RemoteConfig::Status::NONE;
    const String endpoint = String(DEVICE_CONFIG_URL) + (reporting ? "/result" : "/pending");
    WiFiClientSecure secureClient;
    BackendHttp http;
    secureClient.setHandshakeTimeout(3);
    http.setConnectTimeout(1500);
    http.setTimeout(1500);
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
    bool success = false;
    int status = 0;
    if (beginBackendHttp(http, secureClient, endpoint.c_str()))
    {
        http.addHeader("Authorization", String("Bearer ") + DEVICE_CONFIG_TOKEN);
        if (reporting)
        {
            http.addHeader("Content-Type", "application/json");
            const std::string body = RemoteConfig::resultPayload(pendingConfigResult);
            status = http.POST(reinterpret_cast<uint8_t*>(const_cast<char*>(body.data())), body.size());
        }
        else status = http.GET();
        // The backend sends Content-Length. Refuse unbounded/chunked responses.
        String response;
        const int length = http.getSize();
        if (status == 200 && length >= 0 && length <= static_cast<int>(RemoteConfig::MAX_RESPONSE_BYTES))
            response = http.getString();
        if (reporting)
        {
            success = length == static_cast<int>(response.length()) &&
                RemoteConfig::resultAccepted(status, response.c_str(), DEVICE_ID, pendingConfigResult);
            if (success || status == 409)
            {
                // A superseded result must not block fetching the newer command.
                pendingConfigResult = RemoteConfig::Result{};
                success = true;
            }
        }
        else if (status == 200 && !response.isEmpty() && length == static_cast<int>(response.length()))
        {
            pendingConfigResult = remoteConfiguration.receive(response.c_str(), remoteConfigIdentity, persistRemoteConfiguration);
            activateRemoteConfiguration(); // Changed only after verified persistence.
            success = true;
        }
        http.end();
    }
    // At most one bounded request per service call. No redirects, arbitrary
    // commands, network changes, queue clearing, or busy retry loop.
    remoteConfigSchedule.completed(millis(), success, pendingConfigResult.status != RemoteConfig::Status::NONE);
    Serial.printf("[CONFIG] %s HTTP %d; active version %lu.\n", reporting ? "result" : "poll", status,
                  static_cast<unsigned long>(remoteConfiguration.active().version));
}

bool qualityProvisioned()
{
    const size_t length = strlen(DEVICE_QUALITY_TOKEN);
    if (length < 32 || length > 128) return false;
    for (const char* p = DEVICE_QUALITY_TOKEN; *p; ++p)
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '_' || *p == '-')) return false;
    const String suffix = String("/api/devices/") + DEVICE_ID + "/communication-quality";
    return *DEVICE_QUALITY_URL && String(DEVICE_QUALITY_URL).endsWith(suffix);
}

void initializeCommunicationQuality()
{
    if (preferences.isKey("qualityPending"))
    {
        CommunicationQuality::Window pendingQuality;
        if (preferences.getBytesLength("qualityPending") != sizeof(pendingQuality) ||
            preferences.getBytes("qualityPending", &pendingQuality, sizeof(pendingQuality)) != sizeof(pendingQuality) ||
            !qualityOutbox.restore(pendingQuality, qualityIdentity))
        {
            qualityStorageUsable = false;
            Serial.println("[QUALITY] Invalid/mismatched pending report preserved; quality reporting disabled. Telemetry continues.");
            return;
        }
    }
    if (!qualityProvisioned()) Serial.println("[QUALITY] Not provisioned; automatic quality reporting disabled.");
}

bool persistQualityWindow(const CommunicationQuality::Window& window, void*)
{
    CommunicationQuality::Window verified;
    return preferences.putBytes("qualityPending", &window, sizeof(window)) == sizeof(window) &&
        preferences.getBytesLength("qualityPending") == sizeof(verified) &&
        preferences.getBytes("qualityPending", &verified, sizeof(verified)) == sizeof(verified) &&
        CommunicationQuality::validWindow(verified, qualityIdentity) &&
        memcmp(&window, &verified, sizeof(verified)) == 0;
}

bool eraseQualityWindow(void*)
{
    // A prior ambiguous remove may already have committed. Absence is success
    // here only because Outbox first validates the server ACK for this report.
    return (!preferences.isKey("qualityPending") || preferences.remove("qualityPending")) &&
        !preferences.isKey("qualityPending");
}

void serviceCommunicationQuality()
{
    if (!qualityStorageUsable || !qualityProvisioned()) return;
    const uint64_t now = healthUptimeMs();
    if (!qualityCollector.started() && timeReady)
    {
        char bootId[33];
        // Existing durable boot generation refuses overflow. A device ID must
        // not be reused after erasing its NVS boot/sequence history.
        snprintf(bootId, sizeof(bootId), "%032lx", static_cast<unsigned long>(bootSessionId));
        if (!qualityCollector.begin(qualityIdentity, bootId, now, currentHealthEpochMs(), QUEUE_CAPACITY, droppedOldestCount))
        {
            qualityStorageUsable = false;
            Serial.println("[QUALITY] Cannot initialize observation scope/clock; telemetry continues.");
            return;
        }
    }
    qualityCollector.sampleBuffer(queueCount, droppedOldestCount);
    qualityCollector.sampleWifi(WiFi.status() == WL_CONNECTED);
    if (!qualitySchedule.due(millis())) return;
    if (!qualityOutbox.pending() && qualityCollector.ready(now))
    {
        if (!qualityOutbox.capture(qualityCollector, now)) return;
    }
    if (!qualityOutbox.pending()) return;
    if (!qualityOutbox.checkpoint(persistQualityWindow))
    {
        qualitySchedule.completed(millis(), false);
        Serial.println("[QUALITY] Report checkpoint failed; RAM report retained, telemetry continues.");
        return;
    }
    if (WiFi.status() != WL_CONNECTED || !timeReady)
    {
        qualitySchedule.completed(millis(), false);
        return;
    }
    WiFiClientSecure secureClient;
    BackendHttp http;
    secureClient.setHandshakeTimeout(3);
    http.setConnectTimeout(1500);
    http.setTimeout(1500);
    http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
    bool success = false;
    int status = 0;
    if (beginBackendHttp(http, secureClient, DEVICE_QUALITY_URL))
    {
        http.addHeader("Authorization", String("Bearer ") + DEVICE_QUALITY_TOKEN);
        http.addHeader("Content-Type", "application/json");
        const std::string body = CommunicationQuality::payload(qualityOutbox.window());
        status = http.POST(reinterpret_cast<uint8_t*>(const_cast<char*>(body.data())), body.size());
        String response;
        const int length = http.getSize();
        if ((status == 200 || status == 201) && length >= 0 && length <= 1024) response = http.getString();
        success = length == static_cast<int>(response.length()) &&
            qualityOutbox.acknowledge(status, response.c_str(), eraseQualityWindow);
        http.end();
    }
    qualitySchedule.completed(millis(), success);
    Serial.printf("[QUALITY] HTTP %d; pending=%s, counter overflow=%s.\n", status,
        qualityOutbox.pending() ? "yes" : "no", qualityCollector.overflowed() ? "yes" : "no");
}

bool initializeDeviceHealth()
{
    if (preferences.isKey("healthJournal"))
    {
        if (preferences.getBytesLength("healthJournal") != sizeof(healthJournal) ||
            preferences.getBytes("healthJournal", &healthJournal, sizeof(healthJournal)) != sizeof(healthJournal) ||
            !DeviceHealth::validJournal(healthJournal))
        {
            healthJournalUsable = false;
            Serial.println("[HEALTH] Journal invalid; preserved for inspection. Sensor acquisition paused.");
            return false;
        }
    }
    else
    {
        healthJournalDirty = !persistHealthJournal(healthJournal);
    }
    if (!*DEVICE_HEALTH_URL || !*DEVICE_HEALTH_TOKEN ||
        strcmp(DEVICE_HEALTH_TOKEN, "YOUR_DEVICE_HEALTH_TOKEN") == 0)
        Serial.println("[HEALTH] Configure the separate device-health URL/token; reporting is not provisioned.");
    return !healthJournalDirty;
}

bool observeSensorFault(DeviceHealth::Fault fault, bool active, uint64_t observedMs)
{
    if (!healthJournalUsable) return false;
    DeviceHealth::Journal candidate = healthJournal;
    const uint64_t age = healthUptimeMs() - observedMs;
    const int64_t now = currentHealthEpochMs();
    const int64_t observedEpoch = now >= 1700000000000LL &&
        age <= static_cast<uint64_t>(now - 1700000000000LL) ? now - static_cast<int64_t>(age) : 0;
    if (!DeviceHealth::observe(candidate, fault, active, bootSessionId, observedMs, observedEpoch))
    {
        healthObservationBlocked = true;
        Serial.println("[HEALTH] Transition queue full/invalid; sensing paused until it can be recorded.");
        return false;
    }
    if (candidate.crc == healthJournal.crc) return !healthJournalDirty;
    // Retain an uncommitted observation in RAM, but never transmit/continue
    // sensing until the NVS write/read-back succeeds. No silent drop on error.
    healthJournal = candidate;
    healthJournalDirty = !persistHealthJournal(candidate);
    if (healthJournalDirty)
        Serial.println("[HEALTH] Observation not durable yet; retrying storage, sensing paused.");
    return !healthJournalDirty;
}

void serviceSensors()
{
    healthObservationBlocked = false;
    if (!healthJournalUsable || healthJournalDirty) return;
    if (healthJournal.count == DeviceHealth::JOURNAL_CAPACITY)
    {
        healthObservationBlocked = true;
        return;
    }
    if (!CONTINUOUS_VIBRATION_ENABLED && !vibrationBusy.load() && adxlRetry.due(millis()))
    {
        const bool initialized = initADXL345();
        adxlInitFailed = !initialized;
        adxlRetry.attempted(millis(), initialized);
    }
    if (!CONTINUOUS_VIBRATION_ENABLED && adxlRetry.attempts())
        observeSensorFault(DeviceHealth::Fault::ADXL_INIT, adxlInitFailed, healthUptimeMs());
    SensorObservation observation;
    // Peek until the observation is durably recorded. A failed write or full
    // journal cannot discard a short-lived audio/vibration failure or recovery
    // between main loops. The continuous FIFO owner uses this same queue.
    while (xQueuePeek(sensorObservations, &observation, 0) == pdTRUE)
    {
        if (!observeSensorFault(observation.fault, observation.active, observation.observedMs)) break;
        xQueueReceive(sensorObservations, &observation, 0);
    }
}

void queueAudioFault(DeviceHealth::Fault fault, uint64_t observedMs)
{
    const SensorObservation observation{fault, true, observedMs};
    // Bounded queue with backpressure, not a lossy mailbox. This task owns no
    // mutex or live I2S read while waiting; the main task keeps servicing HTTP.
    xQueueSend(sensorObservations, &observation, portMAX_DELAY);
}

void serviceDeviceHealth()
{
    if (!healthJournalUsable) return;
    static uint32_t lastStorageRetry = 0;
    if (healthJournalDirty)
    {
        if (millis() - lastStorageRetry >= 5000U)
        {
            lastStorageRetry = millis();
            healthJournalDirty = !persistHealthJournal(healthJournal);
        }
        if (healthJournalDirty) return;
    }
    if (WiFi.status() != WL_CONNECTED || !timeReady || !*DEVICE_HEALTH_URL ||
        !*DEVICE_HEALTH_TOKEN || strcmp(DEVICE_HEALTH_TOKEN, "YOUR_DEVICE_HEALTH_TOKEN") == 0) return;
    DeviceHealth::Metrics metrics;
    metrics.rssiDbm = std::max(-120, std::min(0, static_cast<int>(WiFi.RSSI())));
    metrics.rebootCount = std::min<uint32_t>(bootSessionId - 1U, 2147483647U);
    metrics.queuedRecords = queueCount;
    metrics.queueCapacity = QUEUE_CAPACITY;
    metrics.firmwareVersion = FIRMWARE_VERSION;
    if (!healthSchedule.due(millis(), DeviceHealth::metricsChanged(lastHealthMetrics, metrics),
                            healthJournal.count != 0)) return;
    const int64_t epochMs = currentHealthEpochMs();
    const uint64_t reportMonotonic = healthUptimeMs();
    TelemetryPacket oldest;
    BinaryTelemetryRecord raw;
    uint64_t ordinal = 0;
    bool queued = !queueIsEmpty();
    const bool headKnown = queued &&
        readOldestPersistent(oldest, ordinal, raw) == QueueReadResult::READY;
    queued = !queueIsEmpty(); // Reading may safely skip proven corruption.
    const auto order = DeviceHealth::dispatchOrder(
        healthJournal, queued, headKnown,
        static_cast<int64_t>(oldest.epochSeconds) * 1000LL,
        bootSessionId, reportMonotonic, epochMs);
    const bool includesFaults = order.transition || order.snapshot;
    // Current metrics need not wait for an old backlog, but they must not
    // advance analysis boundaries or acknowledge a deferred fault transition.
    const std::string body = DeviceHealth::payload(
        healthJournal, metrics, bootSessionId, reportMonotonic, epochMs, includesFaults);
    bool accepted = false;
    if (!body.empty())
    {
        WiFiClientSecure secureClient;
        BackendHttp http;
        secureClient.setHandshakeTimeout(3);
        http.setConnectTimeout(1500);
        http.setTimeout(1500);
        http.setFollowRedirects(HTTPC_DISABLE_FOLLOW_REDIRECTS);
        if (beginBackendHttp(http, secureClient, DEVICE_HEALTH_URL))
        {
            http.addHeader("Content-Type", "application/json");
            http.addHeader("Authorization", String("Bearer ") + DEVICE_HEALTH_TOKEN);
            const int status = http.POST(reinterpret_cast<uint8_t*>(const_cast<char*>(body.data())), body.size());
            const String response = status == 200 ? http.getString() : String();
            accepted = DeviceHealth::accepted(status, response.c_str(), DEVICE_ID, epochMs);
            Serial.printf("[HEALTH] HTTP %d, confirmed=%s, pending=%u\n", status,
                          accepted ? "yes" : "no", static_cast<unsigned>(healthJournal.count));
            http.end();
        }
    }
    if (accepted && order.transition && healthJournal.count)
    {
        DeviceHealth::Journal candidate = healthJournal;
        DeviceHealth::acknowledgeHead(candidate, bootSessionId, reportMonotonic, epochMs);
        // A failed ACK-marker write leaves the old head for an idempotent retry.
        // Never send its successor until this removal is durable.
        if (persistHealthJournal(candidate)) healthJournal = candidate;
        else accepted = false;
    }
    healthSchedule.completed(millis(), accepted);
    if (accepted) lastHealthMetrics = metrics;
}

void setup()
{
    BackendHttp::gate = xSemaphoreCreateMutex();
    if (!BackendHttp::gate) { while (true) { delay(1000); } }
    Serial.begin(
        115200
    );

    delay(
        1000
    );

    Serial.println();

    Serial.println(
        "=============================================================="
    );

    Serial.println(
        " MotorDiagnosis Edge Node"
    );

    Serial.printf(
        " Firmware Version : %s\n",
        FIRMWARE_VERSION
    );

    Serial.println(
        " Fix : I/O-safe ring + session timestamps + strict ACK + TLS"
    );

    Serial.println(
        "=============================================================="
    );

    if (
        !preferences.begin(
            "telemetry",
            false
        )
    )
    {
        Serial.println(
            "[FATAL] NVS Preferences unavailable. Refusing unsafe sequence operation."
        );

        while (true)
        {
            delay(1000);
        }
    }

    initializeRemoteConfiguration();

    telemetrySequence =
        preferences.getUInt(
            "sequence",
            0
        );

    sequencePersistenceReady =
        true;

    loadTimeAnchorHistory();

    if (
        !initializeBootSession()
    )
    {
        Serial.println(
            "[FATAL] Durable boot-session allocation failed."
        );

        while (true)
        {
            delay(1000);
        }
    }

    if (
        !initPersistentStorage()
    )
    {
        Serial.println(
            "[FATAL] LittleFS failed."
        );

        while (true)
        {
            delay(1000);
        }
    }

    if (
        !restoreQueueFromFlash()
    )
    {
        Serial.println(
            "[FATAL] Ring recovery encountered an I/O failure; refusing to overwrite existing backlog."
        );

        while (true)
        {
            delay(1000);
        }
    }

    initializeDeviceHealth();
    initializeCommunicationQuality();

    pinMode(
        ADXL_CS,
        OUTPUT
    );

    digitalWrite(
        ADXL_CS,
        HIGH
    );

    adxlSPI.begin(
        ADXL_SCK,
        ADXL_MISO,
        ADXL_MOSI,
        ADXL_CS
    );

    vibrationResultMutex =
        xSemaphoreCreateMutex();

    audioRingMutex =
        xSemaphoreCreateMutex();

    sensorObservations = xQueueCreate(32, sizeof(SensorObservation));

    if (
        vibrationResultMutex ==
            nullptr ||
        audioRingMutex ==
            nullptr || sensorObservations == nullptr
    )
    {
        Serial.println(
            "[FATAL] Mutex creation failed."
        );

        while (true)
        {
            delay(1000);
        }
    }

    const BaseType_t vibrationCreated = CONTINUOUS_VIBRATION_ENABLED ? pdPASS : xTaskCreatePinnedToCore(
        vibrationTask,
        "VibrationTask",
        8192,
        nullptr,
        2,
        &vibrationTaskHandle,
        0
    );

    // P5:
    // Continuous audio task never waits for per-cycle notification.
    // It continuously drains I2S so stale DMA samples do not accumulate.
    const BaseType_t audioCreated = xTaskCreatePinnedToCore(
        audioCaptureTask,
        "AudioCaptureTask",
        8192,
        nullptr,
        3,
        &audioCaptureTaskHandle,
        1
    );

    if (vibrationCreated != pdPASS || audioCreated != pdPASS)
    {
        Serial.println("[FATAL] Sensor task allocation failed; no unsafe acquisition.");
        while (true) { delay(1000); }
    }
#if CONTINUOUS_VIBRATION_ENABLED
    if (!ContinuousVibration::start()) {
        Serial.println("[FATAL] Continuous window task/buffer allocation failed.");
        while (true) { delay(1000); }
    }
#endif

    Serial.println(
        "[OK] Vibration Task -> Core 0"
    );

    Serial.println(
        "[OK] Continuous Audio Task -> Core 1"
    );

    // P4 non-blocking Wi-Fi startup.
    lastNetworkRetry =
        millis() -
        NETWORK_RETRY_MS;

    startWiFiReconnect();

    // Small startup observation window only.
    // This is intentionally NOT the old 10-second blocking reconnect.
    uint32_t startupWindow =
        millis();

    while (
        millis() -
            startupWindow <
            500
    )
    {
        serviceWiFi();

        if (
            WiFi.status() ==
            WL_CONNECTED
        )
        {
            break;
        }

        delay(10);
    }

    if (
        WiFi.status() ==
        WL_CONNECTED
    )
    {
        if (
            syncTime()
        )
        {
            persistCurrentSessionTimeAnchor();
        }
    }
    else
    {
        Serial.println(
            "[WiFi] Startup continues offline."
        );

        Serial.println(
            "[TIME] No UTC yet. Sensor acquisition will continue and unresolved records will be stored locally."
        );
    }

    Serial.printf(
        "[SYSTEM] Restored Queue : %u / %u\n",
        static_cast<unsigned int>(
            queueCount
        ),
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        )
    );

    Serial.println(
        "[SYSTEM] Pipeline ready."
    );
}

// =====================================================
// Loop
// =====================================================

void loop()
{
    // =================================================
    // P4: Wi-Fi service never waits for connection.
    // =================================================

    serviceWiFi();

    static uint32_t lastTimeSyncAttempt = 0;
    static bool timeSyncAttempted = false;
    // Recover absolute time with bounded attempts, including sensor-fault mode.
    if (
        WiFi.status() ==
            WL_CONNECTED &&
        !timeReady && (!timeSyncAttempted || millis() - lastTimeSyncAttempt >= 30000U)
    )
    {
        timeSyncAttempted = true;
        lastTimeSyncAttempt = millis();
        if (
            syncTime()
        )
        {
            persistCurrentSessionTimeAnchor();
        }
    }

    // If time was already valid (for example after NTP sync in setup) but
    // the anchor write previously failed, retry it. Old-session records only
    // use an exact matching session anchor from the durable history.
    if (
        timeReady
    )
    {
        persistCurrentSessionTimeAnchor();
    }

    serviceCommunicationQuality();
    serviceRemoteConfiguration();
    serviceEdgeAnalysis();
    serviceSensors();
    serviceDeviceHealth(); // Metrics only while an older telemetry head exists.

    // Replay only a bounded slice of old data before each fresh
    // measurement. This prevents a full 24-hour backlog from starving
    // synchronized sensor acquisition while preserving FIFO order.
    if (
        WiFi.status() ==
            WL_CONNECTED &&
        !queueIsEmpty()
    )
    {
#if ADAPTIVE_TRANSMISSION_ENABLED
        ContinuousVibration::AdaptiveRuntime::replaySummariesIfDue();
#else
        replayQueueBatch();
#endif
    }

    // A missing absolute clock no longer blocks sensing. createPacket()
    // will mark the fresh record as unresolved and the binary ring will
    // preserve it until a durable UTC anchor can be established.

    // =================================================
    // P5: one common 0.64 s vibration/acoustic window
    // =================================================

    if ((!CONTINUOUS_VIBRATION_ENABLED && !adxlRetry.ready()) || !audioReady.load() || !healthJournalUsable ||
        healthJournalDirty || healthObservationBlocked)
    {
        delay(200); // Wi-Fi, health reporting and bounded replay remain alive.
        return;
    }

    // Telemetry UTC has whole-second precision, health has milliseconds.
    // Start a new window only after the acknowledged boundary's second, rather
    // than creating a fresh point that the backend would reject as <= boundary.
    if (timeReady && currentHealthEpochMs() / 1000LL * 1000LL <=
        DeviceHealth::acknowledgedBoundary(healthJournal))
    {
        delay(200);
        return;
    }

    VibrationFeatures vib;
    AcousticFeatures audio;
    const uint32_t acquisitionAudioGeneration = audioErrorGeneration.load();

    if (
        !acquireSynchronizedFeatures(
            vib,
            audio
        )
    )
    {
        Serial.println(
            "[SENSOR] Synchronized acquisition failed."
        );
        serviceSensors();
        serviceDeviceHealth();

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    if (
        !isfinite(
            vib.totalRms
        ) ||
        !isfinite(
            vib.peakHz
        ) ||
        !isfinite(
            audio.rmsRaw
        ) ||
        !isfinite(
            audio.peakHz
        )
    )
    {
        Serial.println(
            "[SENSOR] Invalid NaN/Inf measurement."
        );
        observeSensorFault(DeviceHealth::Fault::INVALID_FEATURES, true, healthUptimeMs());
        serviceDeviceHealth();

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    // A successful synchronized, finite window is the recovery evidence;
    // successful driver installation alone cannot clear runtime sensor faults.
    if (!audioReady.load() || acquisitionAudioGeneration != audioErrorGeneration.load())
    {
        serviceSensors();
        serviceDeviceHealth();
        delay(MEASUREMENT_INTERVAL_MS);
        return;
    }
    for (size_t code = 0; code < DeviceHealth::FAULT_COUNT; ++code)
    {
        const auto fault = static_cast<DeviceHealth::Fault>(code);
#if CONTINUOUS_VIBRATION_ENABLED
        // A cached summary must not clear a newer FIFO failure. Recovery is
        // queued by the capture owner after a complete window, in FIFO order.
        if (ContinuousVibration::CaptureHealth::owns(fault)) continue;
#endif
        observeSensorFault(fault, false, healthUptimeMs());
    }
    // Send these observations on the next service pass. An HTTP health request
    // here would shift the fresh packet timestamp after the measured window.
    if (healthJournalDirty || healthObservationBlocked)
    {
        delay(MEASUREMENT_INTERVAL_MS);
        return;
    }

    TelemetryPacket packet;

    if (
        !createPacket(
            vib,
            audio,
            packet
        )
    )
    {
        Serial.println(
            "[PACKET] Creation failed."
        );

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    Serial.printf(
        "[PACKET] Created Sequence %lu%s\n",
        static_cast<unsigned long>(
            packet.sequence
        ),
        packet.timestampResolved
            ? ""
            : " (UTC unresolved)"
    );
    preserveAnalysis(packet);
#if ADAPTIVE_TRANSMISSION_ENABLED
    // Keep vibration/acoustic summary measurements durable too. The ordered
    // replay dispatcher batches the upload period, without delaying health.
    if (!enqueuePersistent(packet)) Serial.println("[ADAPTIVE] Summary persistence failed");
    delay(MEASUREMENT_INTERVAL_MS);
    return;
#endif

    // True cold boot without UTC:
    // even if the station is associated with Wi-Fi but NTP/backend time is
    // unavailable, never attempt an HTTP POST with an invented timestamp.
    // Persist immediately and continue measuring.
    if (
        !packet.timestampResolved
    )
    {
        Serial.printf(
            "[OFFLINE-COLD-BOOT] Storing Sequence %lu locally without UTC; acquisition continues.\n",
            static_cast<unsigned long>(
                packet.sequence
            )
        );

        if (
            !enqueuePersistent(
                packet
            )
        )
        {
            Serial.println(
                "[CRITICAL] Cold-boot telemetry persistence failed."
            );
        }

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    // Existing backlog and pending health transitions always win the ordered
    // dispatcher. Do not bypass a deferred health boundary via direct POST.
    //
    // replayQueueBatch() consumed only a bounded number of oldest
    // records at the start of this loop. If backlog remains, this fresh
    // measurement is appended to the tail instead of bypassing older
    // telemetry. Therefore catch-up and acquisition are interleaved
    // without reordering packets.
    if (
        !queueIsEmpty() || healthJournal.count != 0
    )
    {
        if (
            !enqueuePersistent(
                packet
            )
        )
        {
            Serial.println(
                "[CRITICAL] Telemetry persistence failed."
            );
        }

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    // P4:
    // Wi-Fi offline after clock sync -> store immediately.
    if (
        WiFi.status() !=
        WL_CONNECTED
    )
    {
        Serial.printf(
            "[OFFLINE] Wi-Fi unavailable. Storing Sequence %lu locally.\n",
            static_cast<unsigned long>(
                packet.sequence
            )
        );

        if (
            !enqueuePersistent(
                packet
            )
        )
        {
            Serial.println(
                "[CRITICAL] Telemetry persistence failed."
            );
        }

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    PostOutcome outcome =
        postPacket(
            packet
        );

    if (
        outcome.result ==
        PostResult::SUCCESS
    )
    {
        Serial.printf(
            "[ACK] Sequence %lu completed.\n",
            static_cast<unsigned long>(
                packet.sequence
            )
        );

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    if (
        outcome.result ==
        PostResult::PERMANENT_PACKET_REJECT
    )
    {
        Serial.printf(
            "[REJECT] Sequence %lu permanently rejected (HTTP %d).\n",
            static_cast<unsigned long>(
                packet.sequence
            ),
            outcome.statusCode
        );

        // Power-cut safety invariant:
        // A freshly rejected packet has no ring source yet. Persist and
        // read-back verify it in the ring BEFORE touching the replaceable
        // isolation slot. If power fails while temp->final replacement is
        // in progress, reboot recovery still owns the ring copy.
        const bool ringDurable =
            enqueuePersistent(
                packet
            );

        if (
            !FirmwareLogic::shouldAttemptRejectedArchiveAfterDurableRingWrite(
                ringDurable
            )
        )
        {
            Serial.println(
                "[CRITICAL] Rejected packet could not be made durable in the ring; isolation replacement was not attempted."
            );

            delay(
                MEASUREMENT_INTERVAL_MS
            );

            return;
        }

        // This direct-send path is entered only when the backlog was empty,
        // so the newly persisted packet is now the ring head.
        const uint64_t durableOrdinal =
            ringHeadOrdinal;

        const bool archived =
            saveImmediateRejectedPacket(
                packet,
                outcome
            );

        if (
            !FirmwareLogic::shouldConsumeRejectedRingAfterArchive(
                ringDurable,
                archived
            )
        )
        {
            Serial.println(
                "[REJECT] Isolation replacement incomplete; durable ring source remains queued for reboot/retry."
            );

            delay(
                MEASUREMENT_INTERVAL_MS
            );

            return;
        }

        if (
            !consumeHeadOrdinal(
                durableOrdinal,
                false,
                packet.sequence,
                true
            )
        )
        {
            // Archive is already durable. Leaving an extra ring copy is safe
            // and recovery may replay/isolate it again idempotently.
            Serial.println(
                "[REJECT] Archive is durable but ring consume did not complete; duplicate recovery is safe."
            );
        }

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    if (
        outcome.result ==
        PostResult::CONFIGURATION_ERROR
    )
    {
        // Preserve packet in normal queue because this failure affects
        // the endpoint/auth/configuration rather than this one packet.
        Serial.printf(
            "[HTTP] Configuration/protocol error (HTTP %d). Preserving Sequence %lu for later retry.\n",
            outcome.statusCode,
            static_cast<unsigned long>(
                packet.sequence
            )
        );

        if (
            !enqueuePersistent(
                packet
            )
        )
        {
            Serial.println(
                "[CRITICAL] Telemetry persistence failed."
            );
        }

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    // Retryable network/server error.
    Serial.printf(
        "[OFFLINE] Sequence %lu retryable transmission failure.\n",
        static_cast<unsigned long>(
            packet.sequence
        )
    );

    if (
        !enqueuePersistent(
            packet
        )
    )
    {
        Serial.println(
            "[CRITICAL] Telemetry persistence failed."
        );
    }

    delay(
        MEASUREMENT_INTERVAL_MS
    );
}
