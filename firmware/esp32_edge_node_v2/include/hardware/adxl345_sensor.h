#pragma once

#include <cstdint>

#include "sampling/vibration_window.h"

enum class SampleReadStatus : uint8_t {
    sample,
    empty,
    fifo_overrun,
    sensor_error,
};

class Adxl345Sensor {
public:
    bool begin();
    bool stopMeasurement();
    bool startMeasurement();
    SampleReadStatus readSample(AxisSample& out);

    uint8_t deviceId() const { return deviceId_; }

private:
    bool readRegisters(uint8_t address, uint8_t* data, uint8_t length);
    bool writeRegister(uint8_t address, uint8_t value);
    bool resetFifo();

    bool initialized_ = false;
    uint8_t deviceId_ = 0;
};
