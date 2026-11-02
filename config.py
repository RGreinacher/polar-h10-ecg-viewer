"""
Configuration file for Polar ECG Viewer
"""

# ECG Streaming
ECG_SAMPLE_RATE = 130  # Hz (130 or 250)
ECG_RESOLUTION = 14    # bits

# Display Windows
DISPLAY_WINDOW_SECONDS = 10  # Time window to show in plots
ECG_BUFFER_SECONDS = 30      # Total buffer size (keep more than display)
BPM_HISTORY_POINTS = 500     # Number of BPM points to keep

# Smooth Animation (Time-based scrolling)
ENABLE_SMOOTH_SCROLLING = True   # Scroll plot continuously with real time
SCROLL_UPDATE_INTERVAL_MS = 30   # Update X-axis every N ms (30ms = ~33 FPS)
SMOOTH_SAMPLE_INSERTION = True   # Gradually insert samples over multiple frames
SAMPLES_PER_FRAME = 5            # Max samples to add per frame (for smoothness)

# R-Peak Detection
RPEAK_MIN_DISTANCE_MS = 300
RPEAK_HEIGHT_MULTIPLIER = 0.5
RPEAK_PROMINENCE_MULTIPLIER = 0.3
RPEAK_BUFFER_SECONDS = 2

# BPM Calculation
BPM_SMOOTHING_WINDOW = 2     # Average over N RR intervals for smoother BPM
BPM_AVERAGE_WINDOW_SECONDS = DISPLAY_WINDOW_SECONDS  # Window for "Add Average Line" button
BPM_Y_AXIS_PADDING = 10      # BPM padding above/below min/max (±10 BPM)

# HRV (heart rate variability), computed from RR intervals
HRV_WINDOW_SECONDS = 30      # RMSSD/SDNN use the RR intervals of the last N seconds
HRV_MIN_INTERVALS = 5        # Need at least this many valid RR intervals

# BPM Display
BPM_Y_RANGE = (40, 180)

# ECG Display
ECG_Y_RANGE = (-1000, 1000)

# UI Defaults
SHOW_RPEAKS_DEFAULT = False  # Show R-peak dots by default

# UI Colors (hex)
COLOR_ECG_LINE = '#2E86AB'
COLOR_RPEAK_MARKER = '#FF0000'
COLOR_BPM_LINE = '#A23B72'
COLOR_AVERAGE_LINE = '#F18F01'

# Device
DEVICE_SCAN_TIMEOUT = 5.0
DEVICE_CONNECTION_TIMEOUT = 10.0

# Debug
DEBUG_PEAK_DETECTION = False  # Print R-peak detection debug info