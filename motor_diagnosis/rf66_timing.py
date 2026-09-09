"""Shared RF66 start-to-start continuity limits, not sensor calibration."""

MIN_START_INTERVAL_US = 630000
MAX_START_INTERVAL_US = 670000


def continuous_interval(current_uptime_us, previous_uptime_us):
    interval = current_uptime_us - previous_uptime_us
    return MIN_START_INTERVAL_US <= interval <= MAX_START_INTERVAL_US


def interval_range_us():
    # A fresh list for each stored result/event; old evidence is never updated.
    return [MIN_START_INTERVAL_US, MAX_START_INTERVAL_US]
