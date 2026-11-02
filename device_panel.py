"""
Device panel: everything for ONE connected Polar H10
(connection, streaming, live plots, BPM, recording).

The main window shows one panel per device, so devices never share state.
The panel has no start/stop buttons of its own: the main window's shared
controls call its public methods (start_streaming, start_recording, ...).
"""

import csv
import os
import time
from collections import deque
from typing import Any, Callable, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import QGroupBox, QHBoxLayout, QLabel, QLineEdit, QVBoxLayout

from config import (
    BPM_AVERAGE_WINDOW_SECONDS, BPM_HISTORY_POINTS, BPM_Y_AXIS_PADDING,
    COLOR_AVERAGE_LINE, COLOR_BPM_LINE, COLOR_ECG_LINE, COLOR_RPEAK_MARKER,
    DISPLAY_WINDOW_SECONDS, ECG_BUFFER_SECONDS, ECG_SAMPLE_RATE,
    ENABLE_SMOOTH_SCROLLING, SAMPLES_PER_FRAME, SCROLL_UPDATE_INTERVAL_MS,
    SHOW_RPEAKS_DEFAULT, SMOOTH_SAMPLE_INSERTION,
)
from polar_backend import BleLoop, ECGSample, PolarH10Handler, RPeak

CSV_HEADER = [
    'participant_id', 'sample_datetime', 'packet_received_datetime',
    'polar_timestamp_s', 'ecg_voltage_uv', 'device_name',
]

SAMPLE_INTERVAL = 1.0 / ECG_SAMPLE_RATE
MAX_BUFFER_SAMPLES = int(ECG_SAMPLE_RATE * ECG_BUFFER_SECONDS)
RPEAK_MATCH_TOLERANCE_S = 0.02   # R-peak marker must sit on an ECG sample within 20 ms
# Queued insertion is drained by the frame timer, so it needs scrolling enabled
SMOOTH_INSERTION = SMOOTH_SAMPLE_INSERTION and ENABLE_SMOOTH_SCROLLING
MAX_LAG_S = 2.0                  # If the ECG timeline falls further behind real time, jump ahead


class QtBleBridge(QObject):
    """Runs coroutines on the BLE loop and reports back on the Qt (GUI) thread."""

    _finished = pyqtSignal(object, object)  # (on_done, future)

    def __init__(self, ble_loop: BleLoop):
        super().__init__()
        self._ble_loop = ble_loop
        self._finished.connect(self._invoke)

    def run(self, coro, on_done: Callable[[Optional[BaseException], Any], None]):
        """Run coro; afterwards call on_done(error, result) on the GUI thread."""
        future = self._ble_loop.submit(coro)
        future.add_done_callback(lambda f: self._finished.emit(on_done, f))

    def _invoke(self, on_done, future):
        try:
            result, error = future.result(), None
        except BaseException as e:  # includes CancelledError
            result, error = None, e
        on_done(error, result)


class DevicePanel(QGroupBox):
    """One Polar H10: status, participant ID, ECG plot, BPM plot and recording."""

    ecg_data = pyqtSignal(list)           # List[ECGSample] (from the BLE thread)
    rpeak_detected = pyqtSignal(object)   # RPeak (from the BLE thread)
    closed = pyqtSignal(object, str)      # (panel, message) - connection failed or disconnected
    connected = pyqtSignal(str)           # device name, once the connection is established
    state_changed = pyqtSignal()          # connection/streaming/recording state changed

    def __init__(self, device_name: str, ble: QtBleBridge, parent=None):
        super().__init__(device_name, parent)
        self.device_name = device_name
        self._ble = ble
        self.handler = PolarH10Handler(
            ecg_callback=self.ecg_data.emit,
            rpeak_callback=self.rpeak_detected.emit,
            sample_rate=ECG_SAMPLE_RATE,
        )

        # Connection state
        self._connected = False
        self._streaming = False
        self._busy = True  # An async operation is in flight (connecting at start)

        # Displayed data
        self._ecg_times = deque(maxlen=MAX_BUFFER_SAMPLES)
        self._ecg_voltages = deque(maxlen=MAX_BUFFER_SAMPLES)
        self._pending = deque()  # (time, voltage) waiting for smooth insertion
        self._rpeak_times = deque(maxlen=100)
        self._bpm_times = deque(maxlen=BPM_HISTORY_POINTS)
        self._bpm_values = deque(maxlen=BPM_HISTORY_POINTS)

        # Timeline: the ECG time axis starts at the first packet and is
        # advanced sample by sample (not taken from the BLE packet timing).
        self._t0: Optional[float] = None   # wall-clock time of the first packet
        self._next_sample_time = 0.0       # time assigned to the next incoming sample
        self._time_map = {}                # handler timestamp -> plot timestamp (for R-peaks)

        # Plot decorations
        self._average_lines = []

        # Display options
        self._show_rpeaks = SHOW_RPEAKS_DEFAULT

        # Recording
        self._recording = False
        self._rec_file = None
        self._rec_writer = None
        self._participant_id = ""

        self._init_ui()

        self.ecg_data.connect(self._on_ecg_data)
        self.rpeak_detected.connect(self._on_rpeak)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        if ENABLE_SMOOTH_SCROLLING:
            self._timer.start(SCROLL_UPDATE_INTERVAL_MS)

        self._set_status("Connecting...")
        self._update_controls()
        self._ble.run(self.handler.connect(device_name), self._on_connected)

    # ------------------------------------------------------------------ UI

    def _init_ui(self):
        layout = QVBoxLayout(self)

        # Header row: participant, status, current BPM
        row = QHBoxLayout()
        row.addWidget(QLabel("Participant ID:"))
        self.participant_input = QLineEdit()
        self.participant_input.setPlaceholderText("e.g., P001")
        self.participant_input.setMaximumWidth(100)
        row.addWidget(self.participant_input)
        self.status_label = QLabel()
        row.addWidget(self.status_label)
        row.addStretch()
        self.bpm_label = QLabel("BPM: --")
        self.bpm_label.setStyleSheet("font-size: 14pt; font-weight: bold;")
        row.addWidget(self.bpm_label)
        layout.addLayout(row)

        # ECG plot
        self.ecg_plot = pg.PlotWidget()
        self.ecg_plot.setLabel('left', 'ECG', units='µV')
        self.ecg_plot.setLabel('bottom', 'Time', units='s')
        self.ecg_plot.showGrid(x=True, y=True, alpha=0.3)
        self.ecg_plot.enableAutoRange(axis='y', enable=True)
        self.ecg_plot.setAutoVisible(y=True)
        self.ecg_curve = self.ecg_plot.plot(pen=pg.mkPen(color=COLOR_ECG_LINE, width=2))
        self.rpeak_scatter = pg.ScatterPlotItem(
            size=10, brush=pg.mkBrush(COLOR_RPEAK_MARKER + 'CC'), symbol='o')
        self.rpeak_scatter.setVisible(self._show_rpeaks)
        self.ecg_plot.addItem(self.rpeak_scatter)
        layout.addWidget(self.ecg_plot, stretch=3)

        # BPM plot (X axis follows the ECG plot)
        self.bpm_plot = pg.PlotWidget()
        self.bpm_plot.setLabel('left', 'Heart rate', units='BPM')
        self.bpm_plot.setLabel('bottom', 'Time', units='s')
        self.bpm_plot.showGrid(x=True, y=True, alpha=0.3)
        self.bpm_plot.setYRange(40, 180)
        self.bpm_curve = self.bpm_plot.plot(
            pen=pg.mkPen(color=COLOR_BPM_LINE, width=2),
            symbol='o', symbolSize=5, symbolBrush=pg.mkBrush(COLOR_BPM_LINE + 'AA'))
        self.bpm_plot.setXLink(self.ecg_plot)
        layout.addWidget(self.bpm_plot, stretch=2)

    def _set_status(self, text: str):
        self.status_label.setText(text)

    def _update_controls(self):
        """Lock the participant ID while recording and tell the main window to refresh"""
        self.participant_input.setEnabled(not self._recording)
        self.state_changed.emit()

    # State used by the main window's shared controls
    @property
    def ready(self) -> bool:
        """Connected and not in the middle of an operation"""
        return self._connected and not self._busy

    @property
    def streaming(self) -> bool:
        return self._streaming

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def participant_id(self) -> str:
        return self.participant_input.text().strip()

    @property
    def can_add_average(self) -> bool:
        return len(self._bpm_times) >= 10

    @property
    def has_average_lines(self) -> bool:
        return bool(self._average_lines)

    # ----------------------------------------------- connection / streaming

    def _on_connected(self, error: Optional[BaseException], connected: bool):
        if error is not None or not connected:
            if error is not None:
                reason = str(error) or type(error).__name__
            else:
                reason = "device not found (is it worn and awake?)"
            self.closed.emit(self, f"Could not connect to {self.device_name}: {reason}")
            return
        self._connected = True
        self._busy = False
        self._set_status("Connected")
        self._update_controls()
        self.connected.emit(self.device_name)

    def start_streaming(self):
        if not self.ready or self._streaming:
            return
        self._busy = True
        self._set_status("Starting...")
        self._ble.run(self.handler.start_streaming(), self._on_stream_started)
        self._update_controls()

    def stop_streaming(self):
        if not self.ready or not self._streaming:
            return
        self._busy = True
        self._set_status("Stopping...")
        self._ble.run(self.handler.stop_streaming(), self._on_stream_stopped)
        self._update_controls()

    def _on_stream_started(self, error: Optional[BaseException], _result):
        self._busy = False
        self._streaming = error is None
        self._set_status("Streaming" if self._streaming else f"Failed to start streaming: {error}")
        self._update_controls()

    def _on_stream_stopped(self, error: Optional[BaseException], _result):
        self._busy = False
        self._streaming = False
        self.stop_recording()
        self._set_status("Connected" if error is None else f"Error while stopping: {error}")
        self._update_controls()

    def disconnect_device(self):
        """Stop everything and disconnect; the panel emits `closed` when done."""
        if not self.ready:
            return
        self._busy = True
        self.stop_recording()
        self._set_status("Disconnecting...")
        self._update_controls()
        self._ble.run(self.handler.disconnect(), lambda *_: self.closed.emit(self, f"Disconnected {self.device_name}"))

    def shutdown(self):
        """Release local resources (timer, recording file). BLE is closed by the caller."""
        self._timer.stop()
        self.stop_recording()

    # -------------------------------------------------------------- data

    def _on_ecg_data(self, samples: List[ECGSample]):
        """New ECG packet: record it, give it plot timestamps, queue it for display"""
        if not samples:
            return
        if self._recording:
            self._write_samples(samples)

        now = time.time()
        if self._t0 is None:
            self._t0 = now
        elapsed = now - self._t0

        # Fell far behind real time (e.g. after a pause): jump ahead so the
        # signal stays aligned with the scrolling time axis.
        if self._next_sample_time < elapsed - MAX_LAG_S:
            self._next_sample_time = elapsed - 1.0
            self._pending.clear()

        new_points = []
        for i, sample in enumerate(samples):
            t = self._next_sample_time + i * SAMPLE_INTERVAL
            self._time_map[sample.timestamp] = t   # lets R-peaks find their plot position
            new_points.append((t, sample.voltage))
        self._next_sample_time = new_points[-1][0] + SAMPLE_INTERVAL

        while len(self._time_map) > MAX_BUFFER_SAMPLES:  # drop oldest (dicts keep insertion order)
            del self._time_map[next(iter(self._time_map))]

        if SMOOTH_INSERTION:
            self._pending.extend(new_points)
        else:
            self._append_points(new_points)
        if not ENABLE_SMOOTH_SCROLLING:
            self._refresh()

    def _append_points(self, points):
        for t, v in points:
            self._ecg_times.append(t)
            self._ecg_voltages.append(v)

    def _drain_pending(self):
        """Move queued samples into the display buffer; faster when the queue is long"""
        n = len(self._pending)
        if n > 500:
            count = 50
        elif n > 200:
            count = 20
        elif n > 100:
            count = 10
        else:
            count = SAMPLES_PER_FRAME
        self._append_points([self._pending.popleft() for _ in range(min(count, n))])

    def _refresh(self):
        """Per-frame update: insert samples, redraw, scroll the time axis"""
        if self._t0 is None:
            return
        now = time.time() - self._t0

        self._drain_pending()
        if self._ecg_times:
            times = np.fromiter(self._ecg_times, dtype=float, count=len(self._ecg_times))
            volts = np.fromiter(self._ecg_voltages, dtype=float, count=len(self._ecg_voltages))
            self.ecg_curve.setData(times, volts)
            if self._show_rpeaks:
                self._update_rpeak_markers(times, volts)

        self._update_bpm_range(now)

        if now > 1.0:
            self.ecg_plot.setXRange(now - DISPLAY_WINDOW_SECONDS, now, padding=0)

    def _update_rpeak_markers(self, times: np.ndarray, volts: np.ndarray):
        """Place R-peak dots on the nearest ECG sample (skipping peaks without data yet)"""
        if not self._rpeak_times or len(times) < 2:
            self.rpeak_scatter.setData([], [])
            return
        peaks = np.fromiter(self._rpeak_times, dtype=float, count=len(self._rpeak_times))
        right = np.clip(np.searchsorted(times, peaks), 1, len(times) - 1)
        nearest = np.where(peaks - times[right - 1] <= times[right] - peaks, right - 1, right)
        matched = np.abs(times[nearest] - peaks) < RPEAK_MATCH_TOLERANCE_S
        self.rpeak_scatter.setData(peaks[matched], volts[nearest[matched]])

    def _update_bpm_range(self, now: float):
        """Fit the BPM Y axis to the values currently visible"""
        if len(self._bpm_values) <= 5:
            return
        times = np.fromiter(self._bpm_times, dtype=float, count=len(self._bpm_times))
        values = np.fromiter(self._bpm_values, dtype=float, count=len(self._bpm_values))
        visible = values[times >= now - DISPLAY_WINDOW_SECONDS]
        if len(visible) > 2:
            self.bpm_plot.setYRange(visible.min() - BPM_Y_AXIS_PADDING,
                                    visible.max() + BPM_Y_AXIS_PADDING, padding=0)

    def _plot_time(self, handler_timestamp: float) -> Optional[float]:
        """Convert a handler timestamp to a position on the plot's time axis"""
        if handler_timestamp in self._time_map:
            return self._time_map[handler_timestamp]
        if not self._time_map:
            return None
        nearest = min(self._time_map, key=lambda ts: abs(ts - handler_timestamp))
        return self._time_map[nearest]

    def _on_rpeak(self, rpeak: RPeak):
        t = self._plot_time(rpeak.timestamp)
        if t is None:
            return
        self._rpeak_times.append(t)
        if rpeak.bpm is not None:
            self._bpm_times.append(t)
            self._bpm_values.append(rpeak.bpm)
            self.bpm_curve.setData(list(self._bpm_times), list(self._bpm_values))
            self.bpm_label.setText(f"BPM: {rpeak.bpm:.1f}")
            self._update_controls()

    # ---------------------------------------------------- display options

    def set_show_rpeaks(self, show: bool):
        self._show_rpeaks = show
        self.rpeak_scatter.setVisible(show)

    def add_average_line(self):
        """Add a static line at the mean BPM of the last BPM_AVERAGE_WINDOW_SECONDS"""
        if len(self._bpm_times) < 2:
            return
        cutoff = self._bpm_times[-1] - BPM_AVERAGE_WINDOW_SECONDS
        recent = [bpm for t, bpm in zip(self._bpm_times, self._bpm_values) if t >= cutoff]
        if len(recent) < 2:
            return
        average = float(np.mean(recent))
        line = pg.InfiniteLine(
            pos=average, angle=0, movable=False,
            pen=pg.mkPen(color=COLOR_AVERAGE_LINE, width=2, style=Qt.PenStyle.DashLine),
            label=f'{average:.1f} BPM',
            labelOpts={'position': 0.95, 'color': COLOR_AVERAGE_LINE})
        self.bpm_plot.addItem(line)
        self._average_lines.append(line)
        self._update_controls()

    def clear_average_lines(self):
        for line in self._average_lines:
            self.bpm_plot.removeItem(line)
        self._average_lines.clear()
        self._update_controls()

    def _add_recording_marker(self, text: str, color: str):
        """Dashed vertical line on both plots at the current time"""
        if self._t0 is None:
            return
        now = time.time() - self._t0
        for plot in (self.ecg_plot, self.bpm_plot):
            plot.addItem(pg.InfiniteLine(
                pos=now, angle=90,
                pen=pg.mkPen(color=color, width=2, style=Qt.PenStyle.DashLine),
                label=text,
                labelOpts={'position': 0.95, 'color': color, 'fill': (255, 255, 255, 150)}))

    # --------------------------------------------------------- recording

    def start_recording(self, folder: str, stamp: str) -> Optional[str]:
        """Record to <folder>/ECG_<participant>_<stamp>.csv. Returns an error message or None."""
        participant_id = self.participant_id
        if not participant_id:
            return "Participant ID is missing"
        path = os.path.join(folder, f"ECG_{participant_id}_{stamp}.csv")
        try:
            self._rec_file = open(path, 'w', newline='', encoding='utf-8')
            self._rec_writer = csv.writer(self._rec_file)
            self._rec_writer.writerow(CSV_HEADER)
            self._rec_file.flush()
        except OSError as e:
            self._close_recording_file()
            return f"Cannot write {path}: {e}"
        self._participant_id = participant_id
        self._recording = True
        self._add_recording_marker("Recording started", '#4CAF50')
        self._set_status("🔴 Recording")
        self._update_controls()
        return None

    def stop_recording(self):
        """Stop recording (no-op if not recording)"""
        if not self._recording:
            return
        self._recording = False
        self._close_recording_file()
        self._add_recording_marker("Recording stopped", '#f44336')
        self._set_status("Streaming" if self._streaming else "Connected")
        self._update_controls()

    def _close_recording_file(self):
        if self._rec_file:
            self._rec_file.close()
        self._rec_file = None
        self._rec_writer = None

    def _write_samples(self, samples: List[ECGSample]):
        packet_time = samples[-1].system_receive_time.isoformat()
        try:
            for s in samples:
                self._rec_writer.writerow([
                    self._participant_id,
                    s.system_receive_time.isoformat(),
                    packet_time,
                    f"{s.timestamp:.6f}",
                    f"{s.voltage:.2f}",
                    self.device_name,
                ])
            self._rec_file.flush()
        except OSError as e:
            self.stop_recording()
            self._set_status(f"Recording stopped - write error: {e}")
            self._update_controls()
