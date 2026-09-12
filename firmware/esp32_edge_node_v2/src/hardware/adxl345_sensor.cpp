#include "hardware/adxl345_sensor.h"

#include <Arduino.h>
#include <SPI.h>

#include "config/app_config.h"

namespace {

constexpr uint8_t kRegisterDeviceId = 0x00;
constexpr uint8_t kRegisterBandwidthRate = 0x2C;
constexpr uint8_t kRegisterPowerControl = 0x2D;
constexpr uint8_t kRegisterDataFormat = 0x31;
constexpr uint8_t kRegisterDataX0 = 0x32;
constexpr uint8_t kRegisterFifoControl = 0x38;
constexpr uint8_t kRegisterFifoStatus = 0x39;

constexpr uint8_t kExpectedDeviceId = 0xE5;
constexpr uint8_t kDataRate800Hz = 0x0D;
constexpr uint8_t kFullResolution16G = 0x0B;
constexpr uint8_t kMeasureMode = 0x08;
constexpr uint8_t kFifoStreamMode = 0x80;
constexpr uint8_t kFifoWatermark = 31;
constexpr uint8_t kReadCommand = 0x80;
constexpr uint8_t kMultiByteCommand = 0x40;
constexpr uint8_t kMaxFifoEntries = 32;

SPISettings sensorSpiSettings(5000000, MSBFIRST, SPI_MODE3);

}  // namespace

bool Adxl345Sensor::begin() {
    pinMode(app::kAdxl345CsPin, OUTPUT);
    digitalWrite(app::kAdxl345CsPin, HIGH);
    SPI.begin(app::kAdxl345SckPin, app::kAdxl345MisoPin,
              app::kAdxl345MosiPin, app::kAdxl345CsPin);

    uint8_t id = 0;
    if (!readRegisters(kRegisterDeviceId, &id, 1) || id != kExpectedDeviceId) {
        initialized_ = false;
        return false;
    }

    if (!writeRegister(kRegisterPowerControl, 0x00) ||
        !writeRegister(kRegisterBandwidthRate, kDataRate800Hz) ||
        !writeRegister(kRegisterDataFormat, kFullResolution16G) ||
        !resetFifo()) {
        initialized_ = false;
        return false;
    }

    deviceId_ = id;
    initialized_ = true;
    return true;
}

bool Adxl345Sensor::stopMeasurement() {
    if (!initialized_) {
        return false;
    }
    return writeRegister(kRegisterPowerControl, 0x00);
}

bool Adxl345Sensor::startMeasurement() {
    if (!initialized_) {
        return false;
    }
    return resetFifo() &&
           writeRegister(kRegisterPowerControl, kMeasureMode);
}

SampleReadStatus Adxl345Sensor::readSample(AxisSample& out) {
    if (!initialized_) {
        return SampleReadStatus::sensor_error;
    }

    uint8_t fifoStatus = 0;
    if (!readRegisters(kRegisterFifoStatus, &fifoStatus, 1)) {
        return SampleReadStatus::sensor_error;
    }

    const uint8_t entries = fifoStatus & 0x3F;
    if (entries == 0) {
        return SampleReadStatus::empty;
    }
    if (entries >= kMaxFifoEntries) {
        resetFifo();
        return SampleReadStatus::fifo_overrun;
    }

    uint8_t bytes[6] = {};
    if (!readRegisters(kRegisterDataX0, bytes, sizeof(bytes))) {
        return SampleReadStatus::sensor_error;
    }

    out.x = static_cast<int16_t>(static_cast<uint16_t>(bytes[0]) |
                                 (static_cast<uint16_t>(bytes[1]) << 8));
    out.y = static_cast<int16_t>(static_cast<uint16_t>(bytes[2]) |
                                 (static_cast<uint16_t>(bytes[3]) << 8));
    out.z = static_cast<int16_t>(static_cast<uint16_t>(bytes[4]) |
                                 (static_cast<uint16_t>(bytes[5]) << 8));
    return SampleReadStatus::sample;
}

bool Adxl345Sensor::readRegisters(uint8_t address, uint8_t* data,
                                  uint8_t length) {
    if (data == nullptr || length == 0) {
        return false;
    }

    SPI.beginTransaction(sensorSpiSettings);
    digitalWrite(app::kAdxl345CsPin, LOW);
    SPI.transfer(address | kReadCommand |
                 (length > 1 ? kMultiByteCommand : 0));
    for (uint8_t i = 0; i < length; ++i) {
        data[i] = SPI.transfer(0x00);
    }
    digitalWrite(app::kAdxl345CsPin, HIGH);
    SPI.endTransaction();
    return true;
}

bool Adxl345Sensor::writeRegister(uint8_t address, uint8_t value) {
    SPI.beginTransaction(sensorSpiSettings);
    digitalWrite(app::kAdxl345CsPin, LOW);
    SPI.transfer(address & 0x3F);
    SPI.transfer(value);
    digitalWrite(app::kAdxl345CsPin, HIGH);
    SPI.endTransaction();
    return true;
}

bool Adxl345Sensor::resetFifo() {
    return writeRegister(kRegisterFifoControl, 0x00) &&
           writeRegister(kRegisterFifoControl,
                         kFifoStreamMode | kFifoWatermark);
}
