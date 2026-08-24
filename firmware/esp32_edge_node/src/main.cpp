/*
 * MotorDiagnosis Edge Node
 *
 * Firmware Version : v1.2-beta.2
 * Test             : Forced NTP Failure + Backend Time Fallback
 *
 * Hardware:
 * - ESP32-S3-WROOM-1
 * - ADXL345 (SPI)
 * - INMP441 (I2S)
 *
 * Features:
 * - Concurrent ADXL345 + INMP441 acquisition
 * - Vibration acceleration RMS [g]
 * - Vibration FFT peak [Hz]
 * - Acoustic raw PCM RMS
 * - Acoustic FFT peak [Hz]
 * - Wi-Fi telemetry
 * - Persistent sequence
 * - 256 packet LittleFS offline queue
 * - Reboot recovery / FIFO replay
 * - NTP time synchronization
 * - Backend /api/health time fallback
 *
 * Unit Policy:
 * vibrationRmsRaw = acceleration RMS [g]
 * vibrationRmsMmS = null
 * acousticRmsRaw  = raw PCM RMS
 * acousticDb      = null
 */
#include "secrets.h"
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

// =====================================================
// Firmware
// =====================================================

constexpr char FIRMWARE_VERSION[] =
    "v1.2-beta.2";

// =====================================================
// TEST CONFIG
// =====================================================

// true:
// NTP를 일부러 실패시켜 Backend fallback을 검증
//
// false:
// 실제 운용. NTP → Backend fallback 순서
constexpr bool TEST_FORCE_NTP_FAIL = false;

// =====================================================
// Wi-Fi
// =====================================================

const char* WIFI_SSID =
    WIFI_SSID_VALUE;

const char* WIFI_PASSWORD =
    WIFI_PASSWORD_VALUE;

// =====================================================
// Backend
// =====================================================

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

constexpr uint16_t VIB_SAMPLES =
    512;

constexpr double VIB_SAMPLE_RATE =
    800.0;

constexpr uint32_t VIB_PERIOD_US =
    static_cast<uint32_t>(
        1000000.0 / VIB_SAMPLE_RATE
    );

constexpr double G_PER_LSB =
    0.0039;

constexpr uint16_t AUDIO_SAMPLES =
    2048;

constexpr double AUDIO_SAMPLE_RATE =
    16000.0;

// =====================================================
// Offline Buffer
// =====================================================

constexpr size_t QUEUE_CAPACITY =
    256;

constexpr char QUEUE_DIR[] =
    "/telemetry_queue";

constexpr uint32_t MEASUREMENT_INTERVAL_MS =
    3000;

constexpr uint32_t NETWORK_RETRY_MS =
    5000;

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
// Buffers
// =====================================================

double vibX[VIB_SAMPLES];
double vibY[VIB_SAMPLES];
double vibZ[VIB_SAMPLES];

double vibFFTReal[VIB_SAMPLES];
double vibFFTImag[VIB_SAMPLES];

int32_t audioRaw[AUDIO_SAMPLES];

double audioFFTReal[AUDIO_SAMPLES];
double audioFFTImag[AUDIO_SAMPLES];

// =====================================================
// FFT
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
    AUDIO_SAMPLES,
    AUDIO_SAMPLE_RATE
);

// =====================================================
// Features
// =====================================================

struct VibrationFeatures
{
    double rmsX;
    double rmsY;
    double rmsZ;

    double totalRms;
    double peakHz;

    char fftAxis;
};

struct AcousticFeatures
{
    double rmsRaw;
    double peakHz;

    int32_t peakToPeak;
};

struct TelemetryPacket
{
    uint32_t sequence = 0;
    String payload;
};

// =====================================================
// Persistent Queue Index
// =====================================================

uint32_t queueSequences[
    QUEUE_CAPACITY
];

size_t queueCount =
    0;

// =====================================================
// Shared Results
// =====================================================

VibrationFeatures vibrationResult;
AcousticFeatures acousticResult;

SemaphoreHandle_t resultMutex;

TaskHandle_t vibrationTaskHandle =
    nullptr;

TaskHandle_t acousticTaskHandle =
    nullptr;

volatile bool vibrationFinished =
    false;

volatile bool acousticFinished =
    false;

// =====================================================
// Persistent State
// =====================================================

Preferences preferences;

uint32_t telemetrySequence =
    0;

bool timeReady =
    false;

uint32_t lastNetworkRetry =
    0;

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

    adxlWrite(
        REG_DATA_FORMAT,
        0x0B
    );

    adxlWrite(
        REG_BW_RATE,
        0x0D
    );

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

    config.dma_buf_len =
        256;

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
// Vibration
// =====================================================

VibrationFeatures acquireVibration()
{
    VibrationFeatures result = {};

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
// Acoustic
// =====================================================

AcousticFeatures acquireAudio()
{
    AcousticFeatures result = {};

    size_t totalBytes =
        0;

    uint8_t* buffer =
        reinterpret_cast<uint8_t*>(
            audioRaw
        );

    while (
        totalBytes <
        sizeof(audioRaw)
    )
    {
        size_t bytesRead =
            0;

        esp_err_t status =
            i2s_read(
                I2S_PORT,
                buffer +
                    totalBytes,
                sizeof(audioRaw) -
                    totalBytes,
                &bytesRead,
                portMAX_DELAY
            );

        if (
            status != ESP_OK
        )
        {
            Serial.printf(
                "[AUDIO] I2S error: %d\n",
                status
            );

            return result;
        }

        totalBytes +=
            bytesRead;
    }

    double mean =
        0.0;

    int32_t minValue =
        INT32_MAX;

    int32_t maxValue =
        INT32_MIN;

    for (
        uint16_t i = 0;
        i < AUDIO_SAMPLES;
        i++
    )
    {
        int32_t sample =
            audioRaw[i] >> 8;

        mean +=
            sample;

        if (
            sample < minValue
        )
        {
            minValue =
                sample;
        }

        if (
            sample > maxValue
        )
        {
            maxValue =
                sample;
        }
    }

    mean /=
        AUDIO_SAMPLES;

    double sumSquares =
        0.0;

    for (
        uint16_t i = 0;
        i < AUDIO_SAMPLES;
        i++
    )
    {
        double sample =
            static_cast<double>(
                audioRaw[i] >> 8
            );

        double centered =
            sample -
            mean;

        sumSquares +=
            centered *
            centered;

        audioFFTReal[i] =
            centered;

        audioFFTImag[i] =
            0.0;
    }

    result.rmsRaw =
        sqrt(
            sumSquares /
            AUDIO_SAMPLES
        );

    result.peakToPeak =
        maxValue -
        minValue;

    audioFFT.windowing(
        FFTWindow::Hamming,
        FFTDirection::Forward
    );

    audioFFT.compute(
        FFTDirection::Forward
    );

    audioFFT.complexToMagnitude();

    result.peakHz =
        audioFFT.majorPeak();

    return result;
}

// =====================================================
// Tasks
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
            resultMutex,
            portMAX_DELAY
        );

        vibrationResult =
            local;

        vibrationFinished =
            true;

        xSemaphoreGive(
            resultMutex
        );
    }
}

void acousticTask(
    void* parameter
)
{
    while (true)
    {
        ulTaskNotifyTake(
            pdTRUE,
            portMAX_DELAY
        );

        AcousticFeatures local =
            acquireAudio();

        xSemaphoreTake(
            resultMutex,
            portMAX_DELAY
        );

        acousticResult =
            local;

        acousticFinished =
            true;

        xSemaphoreGive(
            resultMutex
        );
    }
}

// =====================================================
// Wi-Fi
// =====================================================

bool connectWiFi()
{
    if (
        WiFi.status() ==
        WL_CONNECTED
    )
    {
        return true;
    }

    Serial.println();

    Serial.printf(
        "[WiFi] Connecting to %s\n",
        WIFI_SSID
    );

    WiFi.mode(
        WIFI_STA
    );

    WiFi.begin(
        WIFI_SSID,
        WIFI_PASSWORD
    );

    uint32_t started =
        millis();

    while (
        WiFi.status() !=
            WL_CONNECTED &&
        millis() -
            started <
            10000
    )
    {
        Serial.print(".");
        delay(500);
    }

    Serial.println();

    if (
        WiFi.status() !=
        WL_CONNECTED
    )
    {
        Serial.println(
            "[WiFi] Connection unavailable."
        );

        return false;
    }

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

    return true;
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
    // -------------------------------------------------
    // TEST MODE
    // -------------------------------------------------

    if (
        TEST_FORCE_NTP_FAIL
    )
    {
        Serial.println(
            "[TEST] Forced NTP failure enabled."
        );

        Serial.println(
            "[TIME] Skipping NTP."
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
            "[ERROR] Backend time fallback failed."
        );

        return false;
    }

    // -------------------------------------------------
    // NORMAL MODE
    // -------------------------------------------------

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

    // NTP success
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

    // NTP failure
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

uint32_t allocateSequence()
{
    telemetrySequence++;

    preferences.putUInt(
        "sequence",
        telemetrySequence
    );

    return telemetrySequence;
}

// =====================================================
// JSON
// =====================================================

String createTelemetryPayload(
    uint32_t sequence,
    const String& timestamp,
    const VibrationFeatures& vib,
    const AcousticFeatures& audio
)
{
    String json;

    json.reserve(
        768
    );

    json += "{";

    json += "\"timestamp\":\"";
    json += timestamp;
    json += "\",";

    json += "\"sequence\":";
    json += String(sequence);
    json += ",";

    json += "\"siteId\":\"";
    json += SITE_ID;
    json += "\",";

    json += "\"assetId\":\"";
    json += ASSET_ID;
    json += "\",";

    json += "\"deviceId\":\"";
    json += DEVICE_ID;
    json += "\",";

    json += "\"rpm\":null,";

    json += "\"vibrationRmsRaw\":";
    json += String(
        vib.totalRms,
        6
    );
    json += ",";

    json += "\"vibrationRmsMmS\":null,";

    json += "\"vibrationPeakHz\":";
    json += String(
        vib.peakHz,
        2
    );
    json += ",";

    json += "\"acousticRmsRaw\":";
    json += String(
        audio.rmsRaw,
        2
    );
    json += ",";

    json += "\"acousticDb\":null,";

    json += "\"acousticPeakHz\":";
    json += String(
        audio.peakHz,
        2
    );
    json += ",";

    json += "\"scenarioLabel\":\"normal\",";
    json += "\"knownVibrationLabel\":null,";
    json += "\"knownAcousticLabel\":null,";

    json += "\"source\":\"esp32-s3\",";
    json += "\"isSynthetic\":false,";

    json +=
        "\"vibrationUnitNote\":\"ADXL345 acceleration RMS in g\",";

    json +=
        "\"acousticUnitNote\":\"INMP441 raw PCM RMS, uncalibrated\"";

    json += "}";

    return json;
}

// =====================================================
// LittleFS Paths
// =====================================================

String packetFilePath(
    uint32_t sequence
)
{
    char path[64];

    snprintf(
        path,
        sizeof(path),
        "%s/%010lu.json",
        QUEUE_DIR,
        static_cast<unsigned long>(
            sequence
        )
    );

    return String(
        path
    );
}

// =====================================================
// Flash Write
// =====================================================

bool savePacketToFlash(
    const TelemetryPacket& packet
)
{
    String path =
        packetFilePath(
            packet.sequence
        );

    File file =
        LittleFS.open(
            path,
            FILE_WRITE
        );

    if (
        !file
    )
    {
        Serial.println(
            "[FLASH] File create failed."
        );

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
        LittleFS.remove(
            path
        );

        return false;
    }

    Serial.printf(
        "[FLASH] Saved Sequence %lu\n",
        static_cast<unsigned long>(
            packet.sequence
        )
    );

    return true;
}

// =====================================================
// Flash Read
// =====================================================

bool readPacketFromFlash(
    uint32_t sequence,
    TelemetryPacket& packet
)
{
    String path =
        packetFilePath(
            sequence
        );

    File file =
        LittleFS.open(
            path,
            FILE_READ
        );

    if (
        !file
    )
    {
        return false;
    }

    String payload =
        file.readString();

    file.close();

    if (
        payload.length() ==
        0
    )
    {
        return false;
    }

    packet.sequence =
        sequence;

    packet.payload =
        payload;

    return true;
}

// =====================================================
// Flash Delete
// =====================================================

bool deletePacketFromFlash(
    uint32_t sequence
)
{
    String path =
        packetFilePath(
            sequence
        );

    if (
        !LittleFS.exists(
            path
        )
    )
    {
        return true;
    }

    bool removed =
        LittleFS.remove(
            path
        );

    if (
        removed
    )
    {
        Serial.printf(
            "[FLASH] Deleted Sequence %lu\n",
            static_cast<unsigned long>(
                sequence
            )
        );
    }

    return removed;
}

// =====================================================
// Queue
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

void sortQueueSequences()
{
    for (
        size_t i = 1;
        i < queueCount;
        i++
    )
    {
        uint32_t key =
            queueSequences[i];

        int j =
            static_cast<int>(
                i
            ) - 1;

        while (
            j >= 0 &&
            queueSequences[j] >
                key
        )
        {
            queueSequences[
                j + 1
            ] =
                queueSequences[j];

            j--;
        }

        queueSequences[
            j + 1
        ] =
            key;
    }
}

bool addSequenceToQueue(
    uint32_t sequence
)
{
    if (
        queueIsFull()
    )
    {
        return false;
    }

    queueSequences[
        queueCount
    ] =
        sequence;

    queueCount++;

    sortQueueSequences();

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
        Serial.println(
            "[BUFFER] QUEUE FULL."
        );

        return false;
    }

    if (
        !savePacketToFlash(
            packet
        )
    )
    {
        return false;
    }

    if (
        !addSequenceToQueue(
            packet.sequence
        )
    )
    {
        LittleFS.remove(
            packetFilePath(
                packet.sequence
            )
        );

        return false;
    }

    Serial.printf(
        "[BUFFER] Queue : %u / %u\n",
        static_cast<unsigned int>(
            queueCount
        ),
        static_cast<unsigned int>(
            QUEUE_CAPACITY
        )
    );

    return true;
}

bool removeOldestPersistent()
{
    if (
        queueIsEmpty()
    )
    {
        return false;
    }

    uint32_t sequence =
        queueSequences[0];

    if (
        !deletePacketFromFlash(
            sequence
        )
    )
    {
        return false;
    }

    for (
        size_t i = 1;
        i < queueCount;
        i++
    )
    {
        queueSequences[
            i - 1
        ] =
            queueSequences[i];
    }

    queueCount--;

    return true;
}

// =====================================================
// Restore Queue
// =====================================================

void restoreQueueFromFlash()
{
    queueCount =
        0;

    File dir =
        LittleFS.open(
            QUEUE_DIR
        );

    if (
        !dir ||
        !dir.isDirectory()
    )
    {
        return;
    }

    File file =
        dir.openNextFile();

    while (
        file
    )
    {
        if (
            !file.isDirectory()
        )
        {
            String name =
                String(
                    file.name()
                );

            int slash =
                name.lastIndexOf('/');

            String fileName =
                slash >= 0
                ? name.substring(
                    slash + 1
                )
                : name;

            int dot =
                fileName.indexOf('.');

            String seqText =
                dot > 0
                ? fileName.substring(
                    0,
                    dot
                )
                : fileName;

            uint32_t sequence =
                static_cast<uint32_t>(
                    strtoul(
                        seqText.c_str(),
                        nullptr,
                        10
                    )
                );

            if (
                sequence > 0 &&
                queueCount <
                    QUEUE_CAPACITY
            )
            {
                queueSequences[
                    queueCount
                ] =
                    sequence;

                queueCount++;

                if (
                    sequence >
                    telemetrySequence
                )
                {
                    telemetrySequence =
                        sequence;
                }

                Serial.printf(
                    "[RECOVERY] Found Sequence %lu\n",
                    static_cast<unsigned long>(
                        sequence
                    )
                );
            }
        }

        file.close();

        file =
            dir.openNextFile();
    }

    dir.close();

    sortQueueSequences();

    preferences.putUInt(
        "sequence",
        telemetrySequence
    );

    Serial.printf(
        "[RECOVERY] Restored %u packet(s).\n",
        static_cast<unsigned int>(
            queueCount
        )
    );
}

// =====================================================
// HTTP POST
// =====================================================

bool postPacket(
    const TelemetryPacket& packet
)
{
    if (
        WiFi.status() !=
        WL_CONNECTED
    )
    {
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
            INGEST_URL
        )
    )
    {
        return false;
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

    Serial.printf(
        "HTTP     : %d\n",
        statusCode
    );

    if (
        statusCode >
        0
    )
    {
        Serial.println(
            http.getString()
        );
    }

    http.end();

    return (
        statusCode == 200 ||
        statusCode == 201
    );
}

// =====================================================
// Replay
// =====================================================

void flushQueue()
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
    Serial.println(
        "[BUFFER] Persistent Replay Started"
    );

    while (
        !queueIsEmpty()
    )
    {
        uint32_t sequence =
            queueSequences[0];

        TelemetryPacket packet;

        if (
            !readPacketFromFlash(
                sequence,
                packet
            )
        )
        {
            return;
        }

        Serial.printf(
            "[BUFFER] Replaying Sequence %lu\n",
            static_cast<unsigned long>(
                sequence
            )
        );

        if (
            !postPacket(
                packet
            )
        )
        {
            Serial.printf(
                "[BUFFER] Replay stopped at Sequence %lu\n",
                static_cast<unsigned long>(
                    sequence
                )
            );

            return;
        }

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
            return;
        }

        delay(
            100
        );
    }

    Serial.println(
        "[BUFFER] Persistent Replay Completed."
    );
}

// =====================================================
// Sensor Cycle
// =====================================================

void acquireSensorFeatures(
    VibrationFeatures& vib,
    AcousticFeatures& audio
)
{
    xSemaphoreTake(
        resultMutex,
        portMAX_DELAY
    );

    vibrationFinished =
        false;

    acousticFinished =
        false;

    xSemaphoreGive(
        resultMutex
    );

    Serial.println();
    Serial.println(
        "[SYNC] Starting sensor acquisition..."
    );

    uint32_t started =
        millis();

    xTaskNotifyGive(
        vibrationTaskHandle
    );

    xTaskNotifyGive(
        acousticTaskHandle
    );

    while (true)
    {
        bool vibDone;
        bool audioDone;

        xSemaphoreTake(
            resultMutex,
            portMAX_DELAY
        );

        vibDone =
            vibrationFinished;

        audioDone =
            acousticFinished;

        xSemaphoreGive(
            resultMutex
        );

        if (
            vibDone &&
            audioDone
        )
        {
            break;
        }

        delay(1);
    }

    uint32_t elapsed =
        millis() -
        started;

    xSemaphoreTake(
        resultMutex,
        portMAX_DELAY
    );

    vib =
        vibrationResult;

    audio =
        acousticResult;

    xSemaphoreGive(
        resultMutex
    );

    Serial.println();
    Serial.println(
        "========== EDGE FEATURES =========="
    );

    Serial.printf(
        "Acquisition       : %lu ms\n",
        static_cast<unsigned long>(
            elapsed
        )
    );

    Serial.printf(
        "vibrationRmsRaw   : %.6f g\n",
        vib.totalRms
    );

    Serial.printf(
        "vibrationPeakHz   : %.2f Hz\n",
        vib.peakHz
    );

    Serial.printf(
        "acousticRmsRaw    : %.2f\n",
        audio.rmsRaw
    );

    Serial.printf(
        "acousticPeakHz    : %.2f Hz\n",
        audio.peakHz
    );

    Serial.println(
        "==================================="
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
    if (
        !timeReady
    )
    {
        return false;
    }

    String timestamp =
        getTimestamp();

    if (
        timestamp.length() ==
        0
    )
    {
        timeReady =
            false;

        return false;
    }

    packet.sequence =
        allocateSequence();

    packet.payload =
        createTelemetryPayload(
            packet.sequence,
            timestamp,
            vib,
            audio
        );

    return true;
}

// =====================================================
// LittleFS Init
// =====================================================

bool initPersistentStorage()
{
    Serial.println(
        "[FLASH] Mounting LittleFS..."
    );

    if (
        !LittleFS.begin(
            false
        )
    )
    {
        Serial.println(
            "[FLASH] Mount failed. Trying format..."
        );

        if (
            !LittleFS.begin(
                true
            )
        )
        {
            return false;
        }
    }

    if (
        !LittleFS.exists(
            QUEUE_DIR
        )
    )
    {
        if (
            !LittleFS.mkdir(
                QUEUE_DIR
            )
        )
        {
            return false;
        }
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
        "=============================================="
    );

    Serial.println(
        " MotorDiagnosis Edge Node"
    );

    Serial.printf(
        " Firmware Version : %s\n",
        FIRMWARE_VERSION
    );

    Serial.println(
        " Test : Forced NTP Failure + Backend Fallback"
    );

    Serial.println(
        "=============================================="
    );

    preferences.begin(
        "telemetry",
        false
    );

    telemetrySequence =
        preferences.getUInt(
            "sequence",
            0
        );

    Serial.printf(
        "[SYSTEM] Last Sequence : %lu\n",
        static_cast<unsigned long>(
            telemetrySequence
        )
    );

    if (
        !initPersistentStorage()
    )
    {
        Serial.println(
            "[FATAL] LittleFS failed."
        );

        while (true)
            delay(1000);
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
            delay(1000);
    }

    Serial.println(
        "[OK] ADXL345 initialized."
    );

    if (
        !initINMP441()
    )
    {
        Serial.println(
            "[FATAL] INMP441 failed."
        );

        while (true)
            delay(1000);
    }

    Serial.println(
        "[OK] INMP441 initialized."
    );

    resultMutex =
        xSemaphoreCreateMutex();

    if (
        resultMutex ==
        nullptr
    )
    {
        Serial.println(
            "[FATAL] Mutex failed."
        );

        while (true)
            delay(1000);
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

    xTaskCreatePinnedToCore(
        acousticTask,
        "AcousticTask",
        8192,
        nullptr,
        2,
        &acousticTaskHandle,
        1
    );

    Serial.println(
        "[OK] Vibration Task -> Core 0"
    );

    Serial.println(
        "[OK] Acoustic Task  -> Core 1"
    );

    if (
        connectWiFi()
    )
    {
        syncTime();
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
    // Wi-Fi recovery
    if (
        WiFi.status() !=
        WL_CONNECTED
    )
    {
        if (
            millis() -
                lastNetworkRetry >=
            NETWORK_RETRY_MS
        )
        {
            lastNetworkRetry =
                millis();

            if (
                connectWiFi() &&
                !timeReady
            )
            {
                syncTime();
            }
        }
    }

    // Time recovery
    if (
        WiFi.status() ==
            WL_CONNECTED &&
        !timeReady
    )
    {
        syncTime();
    }

    // Replay first
    if (
        WiFi.status() ==
            WL_CONNECTED &&
        !queueIsEmpty()
    )
    {
        flushQueue();
    }

    // No clock
    if (
        !timeReady
    )
    {
        Serial.println(
            "[TIME] No valid clock."
        );

        Serial.println(
            "[TIME] New telemetry acquisition paused."
        );

        delay(3000);

        return;
    }

    // Queue full protection
    if (
        queueIsFull()
    )
    {
        Serial.println(
            "[BUFFER] QUEUE FULL."
        );

        Serial.println(
            "[BUFFER] New acquisition paused."
        );

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    // New measurement
    VibrationFeatures vib;
    AcousticFeatures audio;

    acquireSensorFeatures(
        vib,
        audio
    );

    // Basic validation
    if (
        !isfinite(vib.totalRms) ||
        !isfinite(vib.peakHz) ||
        !isfinite(audio.rmsRaw) ||
        !isfinite(audio.peakHz)
    )
    {
        Serial.println(
            "[SENSOR] Invalid measurement."
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
        "[PACKET] Created Sequence %lu\n",
        static_cast<unsigned long>(
            packet.sequence
        )
    );

    // Existing backlog
    if (
        !queueIsEmpty()
    )
    {
        Serial.println(
            "[BUFFER] Older telemetry pending."
        );

        enqueuePersistent(
            packet
        );

        delay(
            MEASUREMENT_INTERVAL_MS
        );

        return;
    }

    // Immediate POST
    if (
        postPacket(
            packet
        )
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

    // POST failure
    Serial.printf(
        "[OFFLINE] Sequence %lu failed.\n",
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