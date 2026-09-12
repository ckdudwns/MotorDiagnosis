"""Allowlisted deployment keys; no application/ML imports for maintenance."""
ENV_KEYS = tuple("PUMP_DUAL_" + key for key in (
    "EVENT_ARTIFACT", "EVENT_CHECKSUM", "FORECAST_ARTIFACT", "FORECAST_CHECKSUM",
    "STREAM", "DEVICE_ID", "SITE_ID", "ASSET_ID", "SENSOR_ID", "INPUT_MODE"))
INPUT_MODE = "experimental-adxl25"
