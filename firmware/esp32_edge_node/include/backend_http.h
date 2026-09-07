#pragma once
#include <HTTPClient.h>
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

// One TLS connection at a time on the no-PSRAM board. Sensor tasks never wait.
class BackendHttp : public HTTPClient {
    bool held_ = false;
public:
    static SemaphoreHandle_t gate;
    bool acquire() {
        if (held_) return true;
        held_ = gate && xSemaphoreTake(gate, pdMS_TO_TICKS(250)) == pdTRUE;
        return held_;
    }
    void end() {
        setReuse(false);
        HTTPClient::end();
        // HTTPClient::disconnect skips stop() when the peer already closed.
        // Release TLS buffers even on that path before another task gets gate.
        if (_client) {_client->stop(); _client=nullptr;}
        if (held_) {held_=false; xSemaphoreGive(gate);}
    }
    ~BackendHttp() {end();}
};
