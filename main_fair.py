"""
Polar ECG Viewer - Fair/Demo Edition
Dark-themed, recording-free version for public demo booths.
Shows live ECG trace + Tachogram (BPM over time).
"""

import sys
import asyncio
import time
from typing import Optional, List
from collections import deque
from threading import Thread

import numpy as np
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QCheckBox, QGroupBox, QGraphicsDropShadowEffect
)
from PyQt6.QtCore import QTimer, pyqtSignal, QObject, Qt
from PyQt6.QtGui import QFont, QColor, QPalette, QKeySequence, QShortcut
import pyqtgraph as pg

from polar_backend import PolarH10Handler, ECGSample, RPeak

try:
    from config import (
        DISPLAY_WINDOW_SECONDS, ECG_BUFFER_SECONDS, BPM_HISTORY_POINTS,
        BPM_AVERAGE_WINDOW_SECONDS, BPM_Y_AXIS_PADDING, SHOW_RPEAKS_DEFAULT,
        ENABLE_SMOOTH_SCROLLING, SCROLL_UPDATE_INTERVAL_MS,
        SMOOTH_SAMPLE_INSERTION, SAMPLES_PER_FRAME,
    )
except ImportError:
    DISPLAY_WINDOW_SECONDS = 20
    ECG_BUFFER_SECONDS = 30
    BPM_HISTORY_POINTS = 500
    BPM_AVERAGE_WINDOW_SECONDS = 30
    BPM_Y_AXIS_PADDING = 10
    SHOW_RPEAKS_DEFAULT = False
    ENABLE_SMOOTH_SCROLLING = True
    SCROLL_UPDATE_INTERVAL_MS = 30
    SMOOTH_SAMPLE_INSERTION = True
    SAMPLES_PER_FRAME = 5

# ---- Neon dark-theme palette (override freely) ----
BG_COLOR = '#0d1117'
PANEL_BG = '#161b22'
TEXT_COLOR = '#c9d1d9'
ACCENT = '#58a6ff'
COLOR_ECG_LINE = '#00E5FF'      # cyan
COLOR_RPEAK_MARKER = '#FF4081'  # pink
COLOR_BPM_LINE = '#FFAB40'      # orange
COLOR_AVERAGE_LINE = '#69F0AE'  # green

# App title shown at the top - customize for your event
FAIR_TITLE = "🫀  Live ECG & Tachogram  —  Physiology Demo"


DARK_STYLESHEET = f"""
QMainWindow, QWidget {{
    background-color: {BG_COLOR};
    color: {TEXT_COLOR};
    font-size: 11pt;
}}
QGroupBox {{
    background-color: {PANEL_BG};
    border: 1px solid #30363d;
    border-radius: 10px;
    margin-top: 14px;
    font-weight: bold;
    color: {ACCENT};
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 2px 6px;
}}
QPushButton {{
    background-color: #21262d;
    color: {TEXT_COLOR};
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 8px 14px;
    font-weight: bold;
}}
QPushButton:hover {{
    background-color: #30363d;
    border: 1px solid {ACCENT};
}}
QPushButton:disabled {{
    color: #6e7681;
    border: 1px solid #21262d;
}}
QComboBox {{
    background-color: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 6px;
}}
QCheckBox {{
    spacing: 8px;
}}
QStatusBar {{
    background-color: {PANEL_BG};
    color: {TEXT_COLOR};
}}
QLabel#TitleLabel {{
    font-size: 22pt;
    font-weight: bold;
    color: {ACCENT};
    padding: 10px;
}}
QLabel#BpmLabel {{
    font-size: 40pt;
    font-weight: 900;
    color: {COLOR_BPM_LINE};
}}
"""


class SignalBridge(QObject):
    """Qt signals for thread-safe communication"""
    ecg_data = pyqtSignal(list)
    rpeak_detected = pyqtSignal(object)
    connection_status = pyqtSignal(str)
    devices_found = pyqtSignal(list)


class PolarECGViewer(QMainWindow):
    """Main application window for the fair/demo ECG viewer"""

    def __init__(self):
        super().__init__()

        ecg_buffer_size = int(130 * ECG_BUFFER_SECONDS)

        # Data buffers
        self.ecg_times = deque(maxlen=ecg_buffer_size)
        self.ecg_voltages = deque(maxlen=ecg_buffer_size)
        self.rpeak_times = deque(maxlen=100)
        self.rpeak_voltages = deque(maxlen=100)
        self.bpm_times = deque(maxlen=BPM_HISTORY_POINTS)
        self.bpm_values = deque(maxlen=BPM_HISTORY_POINTS)

        self.pending_samples = deque()
        self.enable_smooth_scrolling = ENABLE_SMOOTH_SCROLLING
        self.smooth_sample_insertion = SMOOTH_SAMPLE_INSERTION

        self.streaming_start_time = None
        self.is_streaming_active = False
        self.last_sample_time = 0.0

        self.timestamp_mapping = {}

        self.bpm_window_seconds = BPM_AVERAGE_WINDOW_SECONDS
        self.average_lines = []

        self.show_rpeaks = SHOW_RPEAKS_DEFAULT

        self.signals = SignalBridge()
        self.polar_handler: Optional[PolarH10Handler] = None
        self.asyncio_thread: Optional[Thread] = None
        self.event_loop: Optional[asyncio.AbstractEventLoop] = None

        self.init_ui()

        if self.enable_smooth_scrolling:
            self.scroll_timer = QTimer()
            self.scroll_timer.timeout.connect(self.update_time_based_scroll)
            self.scroll_timer.start(SCROLL_UPDATE_INTERVAL_MS)

        self.signals.ecg_data.connect(self.update_ecg_plot)
        self.signals.rpeak_detected.connect(self.update_rpeak)
        self.signals.connection_status.connect(self.update_status)
        self.signals.devices_found.connect(self.update_device_list)

        self.start_asyncio_thread()

        self.bpm_plot.setXLink(self.ecg_plot)

        # F11 toggles fullscreen - handy for a booth/kiosk display
        QShortcut(QKeySequence("F11"), self, activated=self.toggle_fullscreen)

    # ------------------------------------------------------------------ UI

    def init_ui(self):
        self.setWindowTitle("Live ECG Monitor - Demo")
        self.setGeometry(80, 60, 1400, 900)

        pg.setConfigOption('background', PANEL_BG)
        pg.setConfigOption('foreground', TEXT_COLOR)
        pg.setConfigOptions(antialias=True)

        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        layout = QVBoxLayout(central_widget)
        layout.setSpacing(12)

        # Big banner title
        title_label = QLabel(FAIR_TITLE)
        title_label.setObjectName("TitleLabel")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title_label)

        # Control panel
        control_panel = self.create_control_panel()
        layout.addWidget(control_panel)

        # ECG Plot
        ecg_group = QGroupBox("ECG Signal — Real-time")
        ecg_layout = QVBoxLayout()

        ecg_controls = QHBoxLayout()
        self.rpeak_toggle = QCheckBox("Show R-peaks")
        self.rpeak_toggle.setChecked(SHOW_RPEAKS_DEFAULT)
        self.rpeak_toggle.stateChanged.connect(self.toggle_rpeak_visibility)
        ecg_controls.addWidget(self.rpeak_toggle)
        ecg_controls.addStretch()
        ecg_layout.addLayout(ecg_controls)

        self.ecg_plot = pg.PlotWidget()
        self.ecg_plot.setLabel('left', 'Voltage', units='µV')
        self.ecg_plot.setLabel('bottom', 'Time', units='s')
        self.ecg_plot.showGrid(x=True, y=True, alpha=0.25)
        self.ecg_plot.enableAutoRange(axis='y', enable=True)
        self.ecg_plot.setAutoVisible(y=True)

        self.ecg_curve = self.ecg_plot.plot(pen=pg.mkPen(color=COLOR_ECG_LINE, width=2))

        self.rpeak_scatter = pg.ScatterPlotItem(
            size=10,
            brush=pg.mkBrush(COLOR_RPEAK_MARKER + 'CC'),
            symbol='o'
        )
        self.rpeak_scatter.setVisible(SHOW_RPEAKS_DEFAULT)
        self.ecg_plot.addItem(self.rpeak_scatter)

        ecg_layout.addWidget(self.ecg_plot)
        ecg_group.setLayout(ecg_layout)
        layout.addWidget(ecg_group, stretch=3)

        # Tachogram (BPM) Plot
        bpm_group = QGroupBox("Tachogram — Heart Rate Over Time")
        bpm_layout = QVBoxLayout()

        bpm_controls = QHBoxLayout()

        self.add_avg_btn = QPushButton(f"Mark Average ({BPM_AVERAGE_WINDOW_SECONDS}s)")
        self.add_avg_btn.clicked.connect(self.add_average_line)
        self.add_avg_btn.setEnabled(False)
        bpm_controls.addWidget(self.add_avg_btn)

        self.clear_avg_btn = QPushButton("Clear Markers")
        self.clear_avg_btn.clicked.connect(self.clear_average_lines)
        self.clear_avg_btn.setEnabled(False)
        bpm_controls.addWidget(self.clear_avg_btn)

        bpm_controls.addStretch()

        self.bpm_label = QLabel("-- BPM")
        self.bpm_label.setObjectName("BpmLabel")
        glow = QGraphicsDropShadowEffect()
        glow.setBlurRadius(25)
        glow.setColor(QColor(COLOR_BPM_LINE))
        glow.setOffset(0, 0)
        self.bpm_label.setGraphicsEffect(glow)
        bpm_controls.addWidget(self.bpm_label)

        bpm_layout.addLayout(bpm_controls)

        self.bpm_plot = pg.PlotWidget()
        self.bpm_plot.setLabel('left', 'BPM')
        self.bpm_plot.setLabel('bottom', 'Time', units='s')
        self.bpm_plot.showGrid(x=True, y=True, alpha=0.25)
        self.bpm_plot.setYRange(40, 180)

        self.bpm_curve = self.bpm_plot.plot(
            pen=pg.mkPen(color=COLOR_BPM_LINE, width=3),
            symbol='o',
            symbolSize=6,
            symbolBrush=pg.mkBrush(COLOR_BPM_LINE + 'AA')
        )

        bpm_layout.addWidget(self.bpm_plot)
        bpm_group.setLayout(bpm_layout)
        layout.addWidget(bpm_group, stretch=2)

        self.statusBar().showMessage("Ready — scan for a Polar device to begin")

    def create_control_panel(self) -> QWidget:
        panel = QGroupBox("Device")
        layout = QHBoxLayout()

        layout.addWidget(QLabel("Device:"))
        self.device_combo = QComboBox()
        self.device_combo.addItem("No devices found")
        layout.addWidget(self.device_combo)

        self.scan_btn = QPushButton("Scan Devices")
        self.scan_btn.clicked.connect(self.scan_devices)
        layout.addWidget(self.scan_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.clicked.connect(self.connect_device)
        self.connect_btn.setEnabled(False)
        layout.addWidget(self.connect_btn)

        self.stream_btn = QPushButton("Start Streaming")
        self.stream_btn.clicked.connect(self.toggle_streaming)
        self.stream_btn.setEnabled(False)
        layout.addWidget(self.stream_btn)

        layout.addStretch()

        panel.setLayout(layout)
        return panel

    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    # ------------------------------------------------------------- Device

    def scan_devices(self):
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning...")
        self.statusBar().showMessage("Scanning for Polar devices...")

        asyncio.run_coroutine_threadsafe(
            self._scan_devices_async(),
            self.event_loop
        )

    async def _scan_devices_async(self):
        handler = PolarH10Handler(None, None)
        devices = await handler.scan_devices(timeout=5.0)

        if devices:
            self.signals.devices_found.emit(devices)
            self.signals.connection_status.emit(f"Found {len(devices)} device(s)")
        else:
            self.signals.devices_found.emit([])
            self.signals.connection_status.emit("No Polar devices found")

    def update_device_list(self, devices: List[str]):
        self.device_combo.clear()
        if devices:
            self.device_combo.addItems(devices)
            self.connect_btn.setEnabled(True)
        else:
            self.device_combo.addItem("No devices found")
            self.connect_btn.setEnabled(False)

        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("Scan Devices")

    def connect_device(self):
        device_name = self.device_combo.currentText()

        self.connect_btn.setEnabled(False)
        self.connect_btn.setText("Connecting...")
        self.statusBar().showMessage(f"Connecting to {device_name}...")

        self.polar_handler = PolarH10Handler(
            ecg_callback=self._ecg_callback,
            rpeak_callback=self._rpeak_callback,
            sample_rate=130
        )

        asyncio.run_coroutine_threadsafe(
            self._connect_async(device_name),
            self.event_loop
        )

    async def _connect_async(self, device_name: str):
        success = await self.polar_handler.connect(device_name)

        if success:
            self.signals.connection_status.emit(f"Connected to {device_name}")
        else:
            self.signals.connection_status.emit(f"Failed to connect to {device_name}")

    def update_status(self, message: str):
        self.statusBar().showMessage(message)

        if "Connected" in message:
            self.connect_btn.setText("Connected")
            self.stream_btn.setEnabled(True)
        elif "Failed" in message:
            self.connect_btn.setEnabled(True)
            self.connect_btn.setText("Connect")

    def toggle_streaming(self):
        if not self.polar_handler:
            return

        if not self.polar_handler.is_streaming:
            self.stream_btn.setEnabled(False)
            self.stream_btn.setText("Starting...")

            asyncio.run_coroutine_threadsafe(
                self._start_streaming_async(),
                self.event_loop
            )
        else:
            asyncio.run_coroutine_threadsafe(
                self.polar_handler.stop_streaming(),
                self.event_loop
            )
            self.stream_btn.setText("Start Streaming")
            self.statusBar().showMessage("Streaming stopped")

    async def _start_streaming_async(self):
        success = await self.polar_handler.start_streaming()

        if success:
            self.signals.connection_status.emit("Streaming ECG data...")
            self.stream_btn.setText("Stop Streaming")
            self.stream_btn.setEnabled(True)
        else:
            self.signals.connection_status.emit("Failed to start streaming")
            self.stream_btn.setEnabled(True)

    # --------------------------------------------------------------- Data

    def _ecg_callback(self, samples: List[ECGSample]):
        self.signals.ecg_data.emit(samples)

    def _rpeak_callback(self, rpeak: RPeak):
        self.signals.rpeak_detected.emit(rpeak)

    def update_ecg_plot(self, samples: List[ECGSample]):
        current_real_time = time.time()

        if not self.is_streaming_active:
            self.is_streaming_active = True
            self.streaming_start_time = current_real_time
            self.last_sample_time = 0.0

        relative_time = current_real_time - self.streaming_start_time
        time_per_sample = 1.0 / 130.0

        if self.last_sample_time < relative_time - 2.0:
            self.last_sample_time = relative_time - 1.0
            self.pending_samples.clear()

        adjusted_samples = []
        for i, sample in enumerate(samples):
            new_timestamp = self.last_sample_time + (i * time_per_sample)
            self.timestamp_mapping[sample.timestamp] = new_timestamp

            adjusted_sample = ECGSample(
                timestamp=new_timestamp,
                voltage=sample.voltage,
                system_receive_time=sample.system_receive_time
            )
            adjusted_samples.append(adjusted_sample)

        if samples:
            self.last_sample_time = adjusted_samples[-1].timestamp + time_per_sample

        if len(self.timestamp_mapping) > 5000:
            sorted_keys = sorted(self.timestamp_mapping.keys())
            for key in sorted_keys[:1000]:
                del self.timestamp_mapping[key]

        if self.smooth_sample_insertion:
            self.pending_samples.extend(adjusted_samples)
        else:
            for sample in adjusted_samples:
                self.ecg_times.append(sample.timestamp)
                self.ecg_voltages.append(sample.voltage)
            self.ecg_curve.setData(list(self.ecg_times), list(self.ecg_voltages))

    def update_time_based_scroll(self):
        if not self.is_streaming_active:
            return

        current_time = time.time() - self.streaming_start_time if self.streaming_start_time else 0

        if self.smooth_sample_insertion and self.pending_samples:
            pending_count = len(self.pending_samples)

            if pending_count > 500:
                samples_to_add = min(50, pending_count)
            elif pending_count > 200:
                samples_to_add = min(20, pending_count)
            elif pending_count > 100:
                samples_to_add = min(10, pending_count)
            else:
                samples_to_add = min(SAMPLES_PER_FRAME, pending_count)

            for _ in range(samples_to_add):
                sample = self.pending_samples.popleft()
                self.ecg_times.append(sample.timestamp)
                self.ecg_voltages.append(sample.voltage)

        if len(self.ecg_times) > 0:
            self.ecg_curve.setData(list(self.ecg_times), list(self.ecg_voltages))

        if len(self.rpeak_times) > 0 and len(self.ecg_times) > 0:
            updated_voltages = []
            updated_times = []

            for rpeak_time, rpeak_voltage in zip(self.rpeak_times, self.rpeak_voltages):
                best_voltage = rpeak_voltage
                min_diff = float('inf')

                for ecg_time, ecg_voltage in zip(self.ecg_times, self.ecg_voltages):
                    time_diff = abs(ecg_time - rpeak_time)
                    if time_diff < min_diff and time_diff < 0.02:
                        min_diff = time_diff
                        best_voltage = ecg_voltage

                updated_times.append(rpeak_time)
                updated_voltages.append(best_voltage)

            self.rpeak_voltages.clear()
            self.rpeak_voltages.extend(updated_voltages)

            if self.show_rpeaks:
                self.rpeak_scatter.setData(updated_times, updated_voltages)

        if len(self.bpm_values) > 5 and len(self.bpm_times) > 5:
            x_min = current_time - DISPLAY_WINDOW_SECONDS
            x_max = current_time

            visible_bpms = [
                bpm for bpm_time, bpm in zip(self.bpm_times, self.bpm_values)
                if x_min <= bpm_time <= x_max
            ]

            if len(visible_bpms) > 2:
                min_bpm = min(visible_bpms)
                max_bpm = max(visible_bpms)
                self.bpm_plot.setYRange(
                    min_bpm - BPM_Y_AXIS_PADDING,
                    max_bpm + BPM_Y_AXIS_PADDING,
                    padding=0
                )

        if current_time > 1.0:
            x_min = current_time - DISPLAY_WINDOW_SECONDS
            x_max = current_time
            self.ecg_plot.setXRange(x_min, x_max, padding=0)

    def update_rpeak(self, rpeak: RPeak):
        polar_timestamp = rpeak.timestamp

        our_timestamp = None
        if polar_timestamp in self.timestamp_mapping:
            our_timestamp = self.timestamp_mapping[polar_timestamp]
        else:
            min_diff = float('inf')
            for polar_ts, our_ts in self.timestamp_mapping.items():
                diff = abs(polar_ts - polar_timestamp)
                if diff < min_diff:
                    min_diff = diff
                    our_timestamp = our_ts
                    if diff < 0.001:
                        break

        if our_timestamp is None:
            return

        self.rpeak_times.append(our_timestamp)
        self.rpeak_voltages.append(0)

        if rpeak.bpm is not None:
            self.bpm_times.append(our_timestamp)
            self.bpm_values.append(rpeak.bpm)

            self.bpm_curve.setData(list(self.bpm_times), list(self.bpm_values))
            self.bpm_label.setText(f"{rpeak.bpm:.0f} BPM")

            if len(self.bpm_times) >= 10:
                self.add_avg_btn.setEnabled(True)

    def add_average_line(self):
        if len(self.bpm_times) < 2:
            self.statusBar().showMessage("Not enough BPM data yet")
            return

        current_time = self.bpm_times[-1]
        cutoff_time = current_time - self.bpm_window_seconds

        recent_bpms = [
            bpm for time_, bpm in zip(self.bpm_times, self.bpm_values)
            if time_ >= cutoff_time
        ]

        if len(recent_bpms) < 2:
            self.statusBar().showMessage("Not enough recent data")
            return

        avg_bpm = np.mean(recent_bpms)

        avg_line = pg.InfiniteLine(
            angle=0,
            movable=False,
            pen=pg.mkPen(color=COLOR_AVERAGE_LINE, width=2, style=Qt.PenStyle.DashLine),
            label=f'{avg_bpm:.1f} BPM',
            labelOpts={'position': 0.95, 'color': COLOR_AVERAGE_LINE}
        )
        avg_line.setValue(avg_bpm)
        self.bpm_plot.addItem(avg_line)
        self.average_lines.append(avg_line)

        self.statusBar().showMessage(f"Average: {avg_bpm:.1f} BPM (last {self.bpm_window_seconds}s)")
        self.clear_avg_btn.setEnabled(True)

    def clear_average_lines(self):
        for line in self.average_lines:
            self.bpm_plot.removeItem(line)
        self.average_lines.clear()
        self.clear_avg_btn.setEnabled(False)
        self.statusBar().showMessage("Cleared average markers")

    def toggle_rpeak_visibility(self, state):
        self.show_rpeaks = bool(state)
        self.rpeak_scatter.setVisible(self.show_rpeaks)

    # -------------------------------------------------------------- Async

    def start_asyncio_thread(self):
        def run_loop():
            self.event_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.event_loop)
            self.event_loop.run_forever()

        self.asyncio_thread = Thread(target=run_loop, daemon=True)
        self.asyncio_thread.start()

        timeout = 1.0
        start = time.time()
        while self.event_loop is None and (time.time() - start) < timeout:
            time.sleep(0.01)

        if self.event_loop is None:
            raise RuntimeError("Failed to start asyncio event loop")

    def closeEvent(self, event):
        if hasattr(self, 'scroll_timer'):
            self.scroll_timer.stop()

        if self.polar_handler:
            if self.event_loop and self.event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    self.polar_handler.disconnect(),
                    self.event_loop
                )

        if self.event_loop:
            self.event_loop.call_soon_threadsafe(self.event_loop.stop)

        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setStyleSheet(DARK_STYLESHEET)

    # Dark palette so native widgets (dialogs, tooltips) match too
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(BG_COLOR))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_COLOR))
    palette.setColor(QPalette.ColorRole.Base, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.Text, QColor(TEXT_COLOR))
    palette.setColor(QPalette.ColorRole.Button, QColor(PANEL_BG))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_COLOR))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
    app.setPalette(palette)

    window = PolarECGViewer()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()