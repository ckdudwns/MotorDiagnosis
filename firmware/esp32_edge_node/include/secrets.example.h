#pragma once

// Copy this file to secrets.h for local development.
// Never commit real credentials or private CA material.

#define WIFI_SSID_VALUE "YOUR_WIFI_SSID"
#define WIFI_PASSWORD_VALUE "YOUR_WIFI_PASSWORD"

// Production/default policy: HTTPS only with certificate validation.
#define INGEST_URL_VALUE "https://backend.example.com/api/telemetry/ingest"
#define HEALTH_URL_VALUE "https://backend.example.com/api/health"
#define INGEST_TOKEN_VALUE "YOUR_TELEMETRY_TOKEN"
#define DEVICE_HEALTH_TOKEN_VALUE "YOUR_DEVICE_HEALTH_TOKEN"

// Provision a device-scoped machine credential using docs/production-ingest-auth.md.
// Its secret can fill INGEST_TOKEN_VALUE and DEVICE_HEALTH_TOKEN_VALUE for this
// same device only. Never use a factory-demo token or an operator session.
// A separately provisioned legacy health-only credential is also supported.
#define DEVICE_HEALTH_URL_VALUE "https://backend.example.com/api/devices/DEV-01-MOT-02/health"
#define DEVICE_HEALTH_TOKEN_VALUE "YOUR_DEVICE_HEALTH_TOKEN"

// Optional, separate per-device configuration channel. No admin/ingest token.
// Backend: DEVICE_CONFIG_TOKENS_JSON maps this device ID to a unique token.
// Kept disabled by default. Set the base URL ending in /configuration, with
// no query string or trailing slash, and a provisioned token in secrets.h.
#define DEVICE_CONFIG_URL_VALUE ""
#define DEVICE_CONFIG_TOKEN_VALUE ""

// Optional per-device observation channel. Provision a separate quality-only
// token through backend DEVICE_QUALITY_TOKENS_JSON. URL must end in
// /api/devices/{deviceId}/communication-quality. Disabled by default.
#define DEVICE_QUALITY_URL_VALUE ""
#define DEVICE_QUALITY_TOKEN_VALUE ""

// Opt-in analysis channel uses the existing scoped ingest credential and the
// same HTTPS origin as INGEST_URL_VALUE. Features are separate from telemetry;
// raw 0.64-second waveform windows require an operator request. Eight durable
// slots retain unacknowledged captures; no unbounded audio recording occurs.
#ifndef EDGE_ANALYSIS_ENABLED_VALUE
#define EDGE_ANALYSIS_ENABLED_VALUE false
#endif

// PEM-encoded CA certificate that validates the backend TLS certificate.
// Keep this empty in the example so CI can compile without distributing
// environment-specific trust material. Real deployments must configure it.
#define BACKEND_CA_CERT_VALUE ""

// LOCAL DEVELOPMENT ONLY.
// Set true only in ignored secrets.h when talking to a trusted local HTTP
// backend. Production firmware must keep this false.
#define ALLOW_INSECURE_HTTP_FOR_LOCAL_DEV_VALUE false
