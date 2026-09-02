/*
 * MotorDiagnosis Edge Node
 *
 * Firmware Version : v1.2-beta.11
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
 * - The production ring stores 25,000 x 48-byte records (~1.20 MB),
 *   which exceeds the 24-hour target at the current acquisition cadence.
 * - Overflow policy is oldest-drop: acquisition continues and the newest
 *   retention window is preserved.
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
#include <Preferences.h>
#include <LittleFS.h>
#include <arduinoFFT.h>
#include "driver/i2s.h"

#include <time.h>
#include <sys/time.h>
#include <math.h>
#include <stddef.h>

#include "firmware_logic.h"

#if __has_include("secrets.h")
#include "secrets.h"
#else
#include "secrets.example.h"
#endif

// =====================================================
// Firmware
// =====================================================

constexpr char FIRMWARE_VERSION[] =
    "v1.2-beta.11.7";

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

// Ring holds 1.024 s of audio at 16 kHz.
// This is larger than the 0.64 s synchronized window and provides
// margin while the main task copies/analyzes a completed window.
constexpr size_t AUDIO_RING_CAPACITY =
    16384;

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
// Current offline cycle is approximately:
//   3.0 s measurement interval + ~0.99 s acquisition ~= 3.99 s
//
// 25,000 fixed 48-byte records occupy about 1.20 MB and provide
// roughly 27.7 h at the measured 3.99 s cadence. Even at a 3.64 s
// lower-bound cycle (3.0 s delay + 0.64 s common signal window),
// capacity is about 25.3 h.
//
// IMPORTANT:
// - The HTTP/API payload remains JSON and API-v1.3 compatible.
// - First-send and replay JSON use one canonical serializer fed only by
//   fields preserved in the binary ring, so ACK loss cannot change the
//   payload merely because RAM sensor values had higher precision.
// - Only the local offline storage representation is binary.
// - On overflow the OLDEST buffered record is discarded so acquisition
//   continues and the newest ~24 h window is retained.
//

constexpr size_t QUEUE_CAPACITY =
    25000;

constexpr char RING_FILE[] =
    "/telemetry_ring.bin";

constexpr char REJECTED_DIR[] =
    "/telemetry_rejected";

constexpr uint32_t RING_MAGIC =
    0x4D445231UL; // "MDR1"

constexpr uint16_t RING_SCHEMA_VERSION =
    1;

// Cold-boot timestamp handling:
//
// A device with no RTC cannot know absolute UTC during a true offline
// cold boot. We still acquire and persist the measurement immediately.
// Such records carry this flag and epochSeconds=0 until network time is
// available.
//
// At the first successful time sync, one durable NVS anchor maps ring
// ordinal -> UTC. Every unresolved record is then serialized from that
// same persisted anchor. This makes replay deterministic across reboots
// and avoids changing the JSON body for an already-used sequence.
//
// The measured offline production cadence is approximately 3.99 s.
constexpr uint16_t RING_FLAG_TIME_UNRESOLVED =
    0x0001U;

constexpr uint64_t UNRESOLVED_NOMINAL_PERIOD_MS =
    3990ULL;

constexpr uint8_t LITTLEFS_MOUNT_ATTEMPTS =
    3;

// Fast replay:
// Successful ACKs no longer rewrite + flush the 1.2 MB LittleFS ring
// for every single packet. Instead, consumed ordinals are tracked by a
// durable NVS watermark in batches.
//
// If power is lost before the next watermark commit, at most the uncommitted
// batch can replay once more. The backend already handles duplicate sequence
// IDs idempotently (HTTP 200 duplicate:true), so this is safe at-least-once
// delivery behavior.
constexpr size_t ACK_WATERMARK_BATCH_SIZE =
    32;

// Backlog replay is deliberately bounded per loop iteration.
//
// Previous behavior drained the entire persistent queue before taking
// another sensor measurement. A 24-hour backlog could therefore block
// fresh acquisition for hours.
//
// With this limit, each loop:
//   1. replays at most four oldest records,
//   2. returns to synchronized sensor acquisition,
//   3. appends the fresh packet to the tail if backlog still exists.
//
// FIFO order is preserved, while new measurements continue throughout
// catch-up.
constexpr size_t REPLAY_MAX_RECORDS_PER_LOOP =
    4;

constexpr uint32_t MEASUREMENT_INTERVAL_MS =
    3000;

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

double audioFFTReal[
    AUDIO_FFT_SAMPLES
];

double audioFFTImag[
    AUDIO_FFT_SAMPLES
];

double audioSpectrumAverage[
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

ArduinoFFT<double> vibFFT(
    vibFFTReal,
    vibFFTImag,
    VIB_SAMPLES,
    VIB_SAMPLE_RATE
);

ArduinoFFT<double> audioFFT(
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

    // Resolved packet:
    //   timestampResolved=true, epochSeconds=absolute UTC.
    //
    // Offline cold-boot packet:
    //   timestampResolved=false, epochSeconds=0 until replay resolves
    //   it from the durable ordinal->UTC NVS anchor.
    uint64_t epochSeconds = 0;

    bool timestampResolved = false;

    float vibrationRmsRaw = 0.0f;
    float vibrationPeakHz = 0.0f;
    float acousticRmsRaw = 0.0f;
    float acousticPeakHz = 0.0f;

    String payload;
};

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

uint64_t newestUnresolvedOrdinal =
    0;

// Durable mapping for unresolved records:
//   timestamp_ms = anchor_epoch_ms
//                + (record.ordinal - anchor_ordinal) * 3990 ms
//
// The mapping is stored in NVS before any unresolved packet is replayed.
// It is intentionally kept after the queue drains; recovery ignores a
// stale anchor when there are no active unresolved records. This preserves
// deterministic duplicate replay if power fails before the ACK watermark
// itself becomes durable.
uint64_t unresolvedAnchorOrdinal =
    0;

uint64_t unresolvedAnchorEpochMs =
    0;

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

    return true;
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

void audioCaptureTask(
    void* parameter
)
{
    int32_t dmaSamples[
        AUDIO_DMA_READ_SAMPLES
    ];

    while (true)
    {
        size_t bytesRead =
            0;

        esp_err_t status =
            i2s_read(
                I2S_PORT,
                dmaSamples,
                sizeof(dmaSamples),
                &bytesRead,
                portMAX_DELAY
            );

        if (
            status != ESP_OK
        )
        {
            Serial.printf(
                "[AUDIO] Continuous I2S read error: %d\n",
                status
            );

            delay(10);
            continue;
        }

        size_t sampleCount =
            bytesRead /
            sizeof(int32_t);

        if (
            sampleCount >
            0
        )
        {
            appendAudioSamplesToRing(
                dmaSamples,
                sampleCount
            );
        }
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

void vibrationTask(
    void* parameter
)
{
    while (true)
    {
        ulTaskNotifyTake(
            pdTRUE,
            portMAX_DELAY
        );

        VibrationFeatures local =
            acquireVibration();

        xSemaphoreTake(
            vibrationResultMutex,
            portMAX_DELAY
        );

        vibrationResult =
            local;

        vibrationFinished =
            true;

        xSemaphoreGive(
            vibrationResultMutex
        );
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

            return false;
        }

        delay(1);
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
// Backend Time Fallback
// =====================================================

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

    HTTPClient http;

    http.setConnectTimeout(
        3000
    );

    http.setTimeout(
        3000
    );

    if (
        !http.begin(
            HEALTH_URL
        )
    )
    {
        Serial.println(
            "[TIME] Backend health connection failed."
        );

        return false;
    }

    int statusCode =
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

    String response =
        http.getString();

    http.end();

    const String key =
        "\"timestamp\":";

    int keyPosition =
        response.indexOf(
            key
        );

    if (
        keyPosition <
        0
    )
    {
        Serial.println(
            "[TIME] Backend timestamp not found."
        );

        return false;
    }

    int valueStart =
        keyPosition +
        key.length();

    while (
        valueStart <
            response.length() &&
        (
            response[valueStart] == ' ' ||
            response[valueStart] == '\t'
        )
    )
    {
        valueStart++;
    }

    int valueEnd =
        valueStart;

    while (
        valueEnd <
            response.length() &&
        (
            (
                response[valueEnd] >= '0' &&
                response[valueEnd] <= '9'
            ) ||
            response[valueEnd] == '.'
        )
    )
    {
        valueEnd++;
    }

    double serverTimestamp =
        response.substring(
            valueStart,
            valueEnd
        ).toDouble();

    if (
        serverTimestamp <
        1700000000.0
    )
    {
        Serial.println(
            "[TIME] Invalid backend timestamp."
        );

        return false;
    }

    time_t seconds =
        static_cast<time_t>(
            serverTimestamp
        );

    double fraction =
        serverTimestamp -
        static_cast<double>(
            seconds
        );

    struct timeval tv;

    tv.tv_sec =
        seconds;

    tv.tv_usec =
        static_cast<suseconds_t>(
            fraction *
            1000000.0
        );

    settimeofday(
        &tv,
        nullptr
    );

    timeReady =
        true;

    Serial.printf(
        "[OK] Backend time synchronized: %s\n",
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
// Rejected Storage Paths
// =====================================================

String rejectedPacketFilePath(
    uint32_t sequence
)
{
    char path[64];

    snprintf(
        path,
        sizeof(path),
        "%s/%010lu.json",
        REJECTED_DIR,
        static_cast<unsigned long>(
            sequence
        )
    );

    return String(path);
}

String rejectedMetaFilePath(
    uint32_t sequence
)
{
    char path[64];

    snprintf(
        path,
        sizeof(path),
        "%s/%010lu.meta",
        REJECTED_DIR,
        static_cast<unsigned long>(
            sequence
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

bool recordIsValid(
    const BinaryTelemetryRecord& record
)
{
    if (
        record.magic !=
            RING_MAGIC ||
        record.schemaVersion !=
            RING_SCHEMA_VERSION ||
        record.ordinal == 0 ||
        record.sequence == 0
    )
    {
        return false;
    }

    // Current schema recognizes only the cold-boot unresolved-time flag.
    if (
        (
            record.flags &
            static_cast<uint16_t>(
                ~RING_FLAG_TIME_UNRESOLVED
            )
        ) != 0
    )
    {
        return false;
    }

    if (
        recordHasUnresolvedTimestamp(
            record
        )
    )
    {
        // A true offline cold boot intentionally has no UTC value yet.
        if (
            record.epochSeconds !=
            0
        )
        {
            return false;
        }
    }
    else if (
        record.epochSeconds <
            1700000000ULL
    )
    {
        return false;
    }

    if (
        !isfinite(
            record.vibrationRmsRaw
        ) ||
        !isfinite(
            record.vibrationPeakHz
        ) ||
        !isfinite(
            record.acousticRmsRaw
        ) ||
        !isfinite(
            record.acousticPeakHz
        )
    )
    {
        return false;
    }

    return (
        record.crc32 ==
        calculateRecordCrc(
            record
        )
    );
}

// =====================================================
// Binary Ring File
// =====================================================

uint64_t ringFileSizeBytes()
{
    return (
        static_cast<uint64_t>(
            QUEUE_CAPACITY
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
        QUEUE_CAPACITY
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

bool readRingRecord(
    uint64_t ordinal,
    BinaryTelemetryRecord& record
)
{
    if (
        ordinal == 0
    )
    {
        return false;
    }

    File file =
        LittleFS.open(
            RING_FILE,
            "r"
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

    size_t bytes =
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
        return false;
    }

    return (
        record.ordinal ==
            ordinal &&
        recordIsValid(
            record
        )
    );
}

bool writeRingRecord(
    BinaryTelemetryRecord record
)
{
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
        ) &&
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
// Binary <-> HTTP Packet
// =====================================================

bool unresolvedTimeAnchorReady()
{
    return (
        unresolvedAnchorOrdinal >
            0 &&
        unresolvedAnchorEpochMs >=
            1700000000000ULL
    );
}

bool establishUnresolvedTimeAnchor()
{
    if (
        unresolvedTimestampCount ==
        0
    )
    {
        return true;
    }

    if (
        unresolvedTimeAnchorReady()
    )
    {
        return true;
    }

    if (
        !timeReady
    )
    {
        return false;
    }

    time_t now =
        time(nullptr);

    if (
        now <
        1700000000
    )
    {
        return false;
    }

    if (
        newestUnresolvedOrdinal ==
        0
    )
    {
        Serial.println(
            "[TIME] Cannot establish unresolved timestamp anchor: no active unresolved ordinal."
        );

        return false;
    }

    uint64_t candidateOrdinal =
        newestUnresolvedOrdinal;

    uint64_t candidateEpochMs =
        static_cast<uint64_t>(
            now
        ) *
        1000ULL;

    // Both anchor values must be durable before any unresolved record is
    // serialized or transmitted. This guarantees that an ACK-loss reboot
    // reconstructs the identical timestamp and therefore identical JSON.
    size_t ordinalWritten =
        preferences.putULong64(
            "uTimeOrd",
            candidateOrdinal
        );

    size_t epochWritten =
        preferences.putULong64(
            "uTimeMs",
            candidateEpochMs
        );

    if (
        ordinalWritten !=
            sizeof(uint64_t) ||
        epochWritten !=
            sizeof(uint64_t)
    )
    {
        Serial.println(
            "[TIME] Failed to persist unresolved timestamp anchor; replay will wait."
        );

        return false;
    }

    uint64_t verifiedOrdinal =
        preferences.getULong64(
            "uTimeOrd",
            0
        );

    uint64_t verifiedEpochMs =
        preferences.getULong64(
            "uTimeMs",
            0
        );

    if (
        verifiedOrdinal !=
            candidateOrdinal ||
        verifiedEpochMs !=
            candidateEpochMs
    )
    {
        Serial.println(
            "[TIME] Unresolved timestamp anchor read-back mismatch; replay will wait."
        );

        return false;
    }

    unresolvedAnchorOrdinal =
        candidateOrdinal;

    unresolvedAnchorEpochMs =
        candidateEpochMs;

    Serial.printf(
        "[TIME] Durable cold-boot timestamp anchor: ordinal=%llu epochMs=%llu.\n",
        static_cast<unsigned long long>(
            unresolvedAnchorOrdinal
        ),
        static_cast<unsigned long long>(
            unresolvedAnchorEpochMs
        )
    );

    return true;
}

bool resolveUnresolvedRecordEpoch(
    const BinaryTelemetryRecord& record,
    uint64_t& resolvedEpochSeconds
)
{
    resolvedEpochSeconds =
        0;

    if (
        !recordHasUnresolvedTimestamp(
            record
        )
    )
    {
        resolvedEpochSeconds =
            record.epochSeconds;

        return (
            resolvedEpochSeconds >=
            1700000000ULL
        );
    }

    if (
        !unresolvedTimeAnchorReady() ||
        unresolvedAnchorEpochMs >
            static_cast<uint64_t>(
                INT64_MAX
            )
    )
    {
        return false;
    }

    const int64_t resolvedEpochMs =
        FirmwareLogic::resolveTimestampMs(
            record.ordinal,
            unresolvedAnchorOrdinal,
            static_cast<int64_t>(
                unresolvedAnchorEpochMs
            ),
            static_cast<uint32_t>(
                UNRESOLVED_NOMINAL_PERIOD_MS
            )
        );

    if (
        resolvedEpochMs <
        1700000000000LL
    )
    {
        return false;
    }

    resolvedEpochSeconds =
        static_cast<uint64_t>(
            resolvedEpochMs /
            1000LL
        );

    return true;
}

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
            0;
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

bool recordToPacket(
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
        return false;
    }

    packet.sequence =
        record.sequence;

    if (
        !resolveUnresolvedRecordEpoch(
            record,
            packet.epochSeconds
        )
    )
    {
        if (
            recordHasUnresolvedTimestamp(
                record
            )
        )
        {
            Serial.printf(
                "[TIME] Sequence %lu is safely buffered but still waiting for a durable UTC anchor.\n",
                static_cast<unsigned long>(
                    record.sequence
                )
            );
        }

        return false;
    }

    packet.timestampResolved =
        true;

    packet.vibrationRmsRaw =
        record.vibrationRmsRaw;

    packet.vibrationPeakHz =
        record.vibrationPeakHz;

    packet.acousticRmsRaw =
        record.acousticRmsRaw;

    packet.acousticPeakHz =
        record.acousticPeakHz;

    packet.payload =
        createCanonicalTelemetryPayload(
            packet
        );

    if (
        packet.payload.length() ==
        0
    )
    {
        return false;
    }

    return true;
}

// =====================================================
// Ring Queue
// =====================================================

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

bool readOldestPersistent(
    TelemetryPacket& packet,
    uint64_t& ordinal
)
{
    while (
        !queueIsEmpty()
    )
    {
        ordinal =
            ringHeadOrdinal;

        BinaryTelemetryRecord record;

        if (
            readRingRecord(
                ordinal,
                record
            )
        )
        {
            return recordToPacket(
                record,
                packet
            );
        }

        Serial.printf(
            "[RECOVERY] Invalid/corrupt ring ordinal %llu discarded.\n",
            static_cast<unsigned long long>(
                ordinal
            )
        );

        // Do NOT perform the old 1.2 MB LittleFS flush here.
        // Persist the skipped position with the watermark instead.
        ringHeadOrdinal++;
        queueCount--;

        // Corruption is rare, so commit immediately to avoid repeatedly
        // rediscovering the same bad ordinal after reboot.
        noteConsumedOrdinal(
            ordinal,
            true
        );
    }

    return false;
}

bool removeOldestPersistent()
{
    if (
        queueIsEmpty()
    )
    {
        return false;
    }

    uint64_t ordinal =
        ringHeadOrdinal;

    BinaryTelemetryRecord record;

    uint32_t sequence =
        0;

    bool consumedUnresolvedTimestamp =
        false;

    if (
        readRingRecord(
            ordinal,
            record
        )
    )
    {
        sequence =
            record.sequence;

        consumedUnresolvedTimestamp =
            recordHasUnresolvedTimestamp(
                record
            );
    }

    // FAST PATH:
    // Do not zero/flush the 1.2 MB ring file per ACK.
    // Advance RAM FIFO state immediately and batch-persist a consumed
    // ordinal watermark to NVS.
    ringHeadOrdinal++;
    queueCount--;

    if (
        consumedUnresolvedTimestamp &&
        unresolvedTimestampCount >
            0
    )
    {
        unresolvedTimestampCount--;
    }

    noteConsumedOrdinal(
        ordinal,
        queueIsEmpty()
    );

    if (
        sequence >
        0
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

bool dropOldestForOverflow()
{
    if (
        queueIsEmpty()
    )
    {
        return true;
    }

    uint64_t ordinal =
        ringHeadOrdinal;

    BinaryTelemetryRecord oldest;

    uint32_t sequence =
        0;

    bool droppedUnresolvedTimestamp =
        false;

    if (
        readRingRecord(
            ordinal,
            oldest
        )
    )
    {
        sequence =
            oldest.sequence;

        droppedUnresolvedTimestamp =
            recordHasUnresolvedTimestamp(
                oldest
            );
    }

    // The next enqueue at full capacity naturally overwrites this oldest
    // physical ring slot. Avoid the old per-drop LittleFS invalidation flush.
    ringHeadOrdinal++;
    queueCount--;

    if (
        droppedUnresolvedTimestamp &&
        unresolvedTimestampCount >
            0
    )
    {
        unresolvedTimestampCount--;
    }

    noteConsumedOrdinal(
        ordinal,
        true
    );

    droppedOldestCount++;

    preferences.putULong64(
        "ringDropped",
        droppedOldestCount
    );

    Serial.printf(
        "[BUFFER] Capacity reached. Dropped oldest Sequence %lu. Total dropped: %llu\n",
        static_cast<unsigned long>(
            sequence
        ),
        static_cast<unsigned long long>(
            droppedOldestCount
        )
    );

    return true;
}

bool enqueuePersistent(
    const TelemetryPacket& packet
)
{
    if (
        queueIsFull()
    )
    {
        if (
            !dropOldestForOverflow()
        )
        {
            return false;
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

    if (
        !writeRingRecord(
            record
        )
    )
    {
        return false;
    }

    if (
        recordHasUnresolvedTimestamp(
            record
        )
    )
    {
        unresolvedTimestampCount++;

        if (
            ordinal >
            newestUnresolvedOrdinal
        )
        {
            newestUnresolvedOrdinal =
                ordinal;
        }
    }

    if (
        queueCount == 0
    )
    {
        ringHeadOrdinal =
            ordinal;
    }

    ringNextOrdinal =
        ordinal + 1;

    queueCount++;

    Serial.printf(
        "[BUFFER] Queue : %u / %u | approx %.1f h retained | dropped=%llu\n",
        static_cast<unsigned int>(
            queueCount
        ),
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        ),
        (
            static_cast<double>(
                queueCount
            ) *
            3.99
        ) /
            3600.0,
        static_cast<unsigned long long>(
            droppedOldestCount
        )
    );

    return true;
}

// =====================================================
// Ring Recovery
// =====================================================

void restoreQueueFromFlash()
{
    queueCount =
        0;

    ringHeadOrdinal =
        0;

    ringNextOrdinal =
        1;

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

    newestUnresolvedOrdinal =
        0;

    unresolvedAnchorOrdinal =
        0;

    unresolvedAnchorEpochMs =
        0;

    if (
        !ensureRingFile()
    )
    {
        Serial.println(
            "[RING] Ring file initialization failed."
        );

        return;
    }

    File file =
        LittleFS.open(
            RING_FILE,
            "r"
        );

    if (!file)
    {
        return;
    }

    uint64_t minimumActiveOrdinal =
        UINT64_MAX;

    uint64_t maximumActiveOrdinal =
        0;

    // Must include stale-but-valid consumed slots so a reboot never reuses
    // an ordinal lower than the durable watermark.
    uint64_t maximumOrdinalSeen =
        committedConsumedOrdinal;

    size_t activeValidCount =
        0;

    size_t ignoredConsumedCount =
        0;

    size_t corruptCount =
        0;

    for (
        size_t slot = 0;
        slot < QUEUE_CAPACITY;
        slot++
    )
    {
        BinaryTelemetryRecord record;

        size_t bytes =
            file.read(
                reinterpret_cast<uint8_t*>(
                    &record
                ),
                sizeof(record)
            );

        if (
            bytes !=
            sizeof(record)
        )
        {
            break;
        }

        // Empty / legacy-invalidated slot.
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
            record.ordinal >
            maximumOrdinalSeen
        )
        {
            maximumOrdinalSeen =
                record.ordinal;
        }

        if (
            record.sequence >
            telemetrySequence
        )
        {
            telemetrySequence =
                record.sequence;
        }

        // New fast-replay rule:
        // valid bytes can remain in the ring after ACK. The durable watermark
        // is authoritative for whether that ordinal is still queued.
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
                record.ordinal >
                newestUnresolvedOrdinal
            )
            {
                newestUnresolvedOrdinal =
                    record.ordinal;
            }
        }

        if (
            record.ordinal <
            minimumActiveOrdinal
        )
        {
            minimumActiveOrdinal =
                record.ordinal;
        }

        if (
            record.ordinal >
            maximumActiveOrdinal
        )
        {
            maximumActiveOrdinal =
                record.ordinal;
        }
    }

    file.close();

    // Never reuse an ordinal at or below the consumed watermark.
    ringNextOrdinal =
        FirmwareLogic::calculateNextRingOrdinal(
            maximumOrdinalSeen
        );

    if (
        activeValidCount >
        0
    )
    {
        ringHeadOrdinal =
            minimumActiveOrdinal;

        // Valid records should form one continuous FIFO interval.
        // Any missing/torn slot is skipped lazily by readOldestPersistent().
        queueCount =
            FirmwareLogic::calculateRecoveredQueueCount(
                minimumActiveOrdinal,
                maximumActiveOrdinal,
                QUEUE_CAPACITY,
                activeValidCount
            );
    }

    // The ring may contain a sequence newer than the NVS value (for
    // example, if a prior write was interrupted). Repair that floor before
    // any new sequence is ever allocated.
    if (
        !ensureCurrentSequenceFloorDurable()
    )
    {
        Serial.println(
            "[RECOVERY] Sequence floor is NOT durable. New packet creation will remain blocked until NVS persistence succeeds."
        );
    }

    if (
        unresolvedTimestampCount >
        0
    )
    {
        uint64_t storedAnchorOrdinal =
            preferences.getULong64(
                "uTimeOrd",
                0
            );

        uint64_t storedAnchorEpochMs =
            preferences.getULong64(
                "uTimeMs",
                0
            );

        if (
            storedAnchorOrdinal >
                0 &&
            storedAnchorEpochMs >=
                1700000000000ULL
        )
        {
            unresolvedAnchorOrdinal =
                storedAnchorOrdinal;

            unresolvedAnchorEpochMs =
                storedAnchorEpochMs;

            Serial.printf(
                "[RECOVERY] Restored cold-boot UTC anchor: ordinal=%llu epochMs=%llu.\n",
                static_cast<unsigned long long>(
                    unresolvedAnchorOrdinal
                ),
                static_cast<unsigned long long>(
                    unresolvedAnchorEpochMs
                )
            );
        }
    }

    Serial.printf(
        "[RECOVERY] Binary ring restored %u queued slot(s).\n",
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
        "[RECOVERY] Active cold-boot unresolved timestamps: %u.\n",
        static_cast<unsigned int>(
            unresolvedTimestampCount
        )
    );

    Serial.printf(
        "[RECOVERY] CRC-invalid/torn slots observed: %u.\n",
        static_cast<unsigned int>(
            corruptCount
        )
    );

    Serial.printf(
        "[RECOVERY] Ring capacity: %u records, %llu bytes (~25+ h minimum target).\n",
        static_cast<unsigned int>(
            QUEUE_CAPACITY
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
// LittleFS Quiet Existence Helper
// =====================================================

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

    int slash =
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

    String directoryPath =
        slash == 0
            ? "/"
            : path.substring(
                  0,
                  slash
              );

    String expectedName =
        path.substring(
            slash + 1
        );

    File directory =
        LittleFS.open(
            directoryPath
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
            String entryName =
                String(
                    entry.name()
                );

            int entrySlash =
                entryName.lastIndexOf('/');

            if (
                entrySlash >= 0
            )
            {
                entryName =
                    entryName.substring(
                        entrySlash + 1
                    );
            }

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

bool writeRejectedMeta(
    uint32_t sequence,
    const PostOutcome& outcome
)
{
    File metaFile =
        LittleFS.open(
            rejectedMetaFilePath(
                sequence
            ),
            FILE_WRITE
        );

    if (!metaFile)
    {
        return false;
    }

    metaFile.print(
        "sequence="
    );

    metaFile.println(
        sequence
    );

    metaFile.print(
        "httpStatus="
    );

    metaFile.println(
        outcome.statusCode
    );

    metaFile.print(
        "response="
    );

    metaFile.println(
        outcome.response
    );

    metaFile.print(
        "recordedAt="
    );

    String timestamp =
        getTimestamp();

    metaFile.println(
        timestamp.length() > 0
            ? timestamp
            : "time-unavailable"
    );

    metaFile.flush();
    metaFile.close();

    return true;
}

bool saveImmediateRejectedPacket(
    const TelemetryPacket& packet,
    const PostOutcome& outcome
)
{
    String path =
        rejectedPacketFilePath(
            packet.sequence
        );

    if (
        fileExistsQuiet(
            path
        )
    )
    {
        path +=
            "." +
            String(
                millis()
            ) +
            ".rejected";
    }

    File file =
        LittleFS.open(
            path,
            FILE_WRITE
        );

    if (!file)
    {
        return false;
    }

    size_t written =
        file.print(
            packet.payload
        );

    file.flush();
    file.close();

    if (
        written !=
        packet.payload.length()
    )
    {
        return false;
    }

    bool metaOk =
        writeRejectedMeta(
            packet.sequence,
            outcome
        );

    Serial.printf(
        "[REJECT] Preserved Sequence %lu (HTTP %d)\n",
        static_cast<unsigned long>(
            packet.sequence
        ),
        outcome.statusCode
    );

    return metaOk;
}

bool moveQueuedPacketToRejected(
    const TelemetryPacket& packet,
    const PostOutcome& outcome
)
{
    // Binary-ring packets are reconstructed into the exact API JSON payload
    // before preservation in the rejected archive.
    return saveImmediateRejectedPacket(
        packet,
        outcome
    );
}

// =====================================================
// HTTP POST
// =====================================================

PostOutcome postPacket(
    const TelemetryPacket& packet
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

    HTTPClient http;

    http.setConnectTimeout(
        3000
    );

    http.setTimeout(
        3000
    );

    if (
        !http.begin(
            INGEST_URL
        )
    )
    {
        outcome.result =
            PostResult::RETRYABLE;

        outcome.statusCode =
            -1;

        outcome.response =
            "http-begin-failed";

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

    int statusCode =
        http.POST(
            packet.payload
        );

    outcome.statusCode =
        statusCode;

    if (
        statusCode >
        0
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

    Serial.printf(
        "HTTP     : %d\n",
        statusCode
    );

    if (
        outcome.response.length() >
        0
    )
    {
        Serial.println(
            outcome.response
        );
    }

    http.end();

    outcome.result =
        classifyHttpOutcome(
            packet,
            statusCode,
            outcome.response
        );

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

        uint64_t ordinal =
            0;

        if (
            !readOldestPersistent(
                packet,
                ordinal
            )
        )
        {
            commitConsumedWatermark();
            return;
        }

        uint32_t sequence =
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
                "[BUFFER] ACK Sequence %lu\n",
                static_cast<unsigned long>(
                    sequence
                )
            );

            if (
                !removeOldestPersistent()
            )
            {
                commitConsumedWatermark();
                return;
            }

            consumedThisBatch++;

            // Keep a small pacing gap so catch-up does not hammer the
            // backend, but never drain the whole backlog in one loop.
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
                commitConsumedWatermark();
                return;
            }

            if (
                !removeOldestPersistent()
            )
            {
                commitConsumedWatermark();
                return;
            }

            consumedThisBatch++;

            Serial.println(
                "[BUFFER] Continuing with next queued packet."
            );
        }
    }

    // Do not force an NVS write at every small replay slice.
    // removeOldestPersistent() keeps the existing 32-record watermark
    // batching policy and force-commits automatically when the queue
    // becomes empty. If power is lost between commits, the backend's
    // idempotent duplicate handling safely absorbs the small replay window.
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

    // True offline cold boot:
    // no RTC means absolute UTC is physically unknowable right now.
    // Do NOT invent a timestamp and do NOT stop acquisition. Persist the
    // sensor values + durable sequence with the unresolved-time ring flag.
    // Once UTC becomes available, replay derives a deterministic timestamp
    // from the durable ordinal->UTC anchor.
    packet.timestampResolved =
        false;

    packet.epochSeconds =
        0;

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

void setup()
{
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
        " Fix : Durable Sequence + No Auto-Format + Offline Cold Boot"
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

    telemetrySequence =
        preferences.getUInt(
            "sequence",
            0
        );

    sequencePersistenceReady =
        true;

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

    restoreQueueFromFlash();

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

    if (
        !initADXL345()
    )
    {
        Serial.println(
            "[FATAL] ADXL345 failed."
        );

        while (true)
        {
            delay(1000);
        }
    }

    if (
        !initINMP441()
    )
    {
        Serial.println(
            "[FATAL] INMP441 failed."
        );

        while (true)
        {
            delay(1000);
        }
    }

    vibrationResultMutex =
        xSemaphoreCreateMutex();

    audioRingMutex =
        xSemaphoreCreateMutex();

    if (
        vibrationResultMutex ==
            nullptr ||
        audioRingMutex ==
            nullptr
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

    xTaskCreatePinnedToCore(
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
    xTaskCreatePinnedToCore(
        audioCaptureTask,
        "AudioCaptureTask",
        8192,
        nullptr,
        3,
        &audioCaptureTaskHandle,
        1
    );

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
        syncTime();
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

    // Recover absolute time when network becomes available.
    if (
        WiFi.status() ==
            WL_CONNECTED &&
        !timeReady
    )
    {
        syncTime();
    }

    // If cold-boot records were captured before UTC was known, establish
    // one durable ordinal->UTC mapping before any of those records can be
    // replayed. Failure is safe: data stays queued and acquisition continues.
    if (
        timeReady &&
        unresolvedTimestampCount >
            0 &&
        !unresolvedTimeAnchorReady()
    )
    {
        establishUnresolvedTimeAnchor();
    }

    // Replay only a bounded slice of old data before each fresh
    // measurement. This prevents a full 24-hour backlog from starving
    // synchronized sensor acquisition while preserving FIFO order.
    if (
        WiFi.status() ==
            WL_CONNECTED &&
        !queueIsEmpty()
    )
    {
        replayQueueBatch();
    }

    // A missing absolute clock no longer blocks sensing. createPacket()
    // will mark the fresh record as unresolved and the binary ring will
    // preserve it until a durable UTC anchor can be established.

    // =================================================
    // P5: one common 0.64 s vibration/acoustic window
    // =================================================

    VibrationFeatures vib;
    AcousticFeatures audio;

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

        delay(
            MEASUREMENT_INTERVAL_MS
        );

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

    // Existing backlog always wins FIFO.
    //
    // replayQueueBatch() consumed only a bounded number of oldest
    // records at the start of this loop. If backlog remains, this fresh
    // measurement is appended to the tail instead of bypassing older
    // telemetry. Therefore catch-up and acquisition are interleaved
    // without reordering packets.
    if (
        !queueIsEmpty()
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

        if (
            !saveImmediateRejectedPacket(
                packet,
                outcome
            )
        )
        {
            Serial.println(
                "[CRITICAL] Rejected telemetry preservation failed."
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
