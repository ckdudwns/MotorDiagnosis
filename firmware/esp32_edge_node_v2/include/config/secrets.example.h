#pragma once

#define ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE false
#define WIFI_SSID_VALUE "YOUR_WIFI_SSID"
#define WIFI_PASSWORD_VALUE "YOUR_WIFI_PASSWORD"

#define INGEST_URL_VALUE \
    "https://motordiagnosis-api.duckdns.org/api/devices/DEV-01-MOT-02/periodic-snapshots"
#define HEALTH_URL_VALUE "https://example.invalid/api/health"
#define DEVICE_HEALTH_URL_VALUE \
    "https://example.invalid/api/devices/YOUR_DEVICE_ID/health"
#define DEVICE_HEALTH_TOKEN_VALUE "YOUR_DEVICE_HEALTH_TOKEN"

#define BACKEND_CA_CERT_VALUE \
    "-----BEGIN CERTIFICATE-----\n" \
    "YOUR_BACKEND_CA_CERTIFICATE\n" \
    "-----END CERTIFICATE-----\n"

#define INGEST_TOKEN_VALUE "YOUR_INGEST_TOKEN"
