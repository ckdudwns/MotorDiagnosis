#pragma once

// Copy this file to secrets.h for local development.
// Never commit real credentials or private CA material.

#define WIFI_SSID_VALUE "YOUR_WIFI_SSID"
#define WIFI_PASSWORD_VALUE "YOUR_WIFI_PASSWORD"

// Production/default policy: HTTPS only with certificate validation.
#define INGEST_URL_VALUE "https://backend.example.com/api/telemetry/ingest"
#define HEALTH_URL_VALUE "https://backend.example.com/api/health"
#define INGEST_TOKEN_VALUE "YOUR_TELEMETRY_TOKEN"

// PEM-encoded CA certificate that validates the backend TLS certificate.
// Keep this empty in the example so CI can compile without distributing
// environment-specific trust material. Real deployments must configure it.
#define BACKEND_CA_CERT_VALUE ""

// LOCAL DEVELOPMENT ONLY.
// Set true only in ignored secrets.h when talking to a trusted local HTTP
// backend. Production firmware must keep this false.
#define ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE false
