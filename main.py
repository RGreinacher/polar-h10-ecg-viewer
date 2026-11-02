"""
Polar ECG Viewer - Main Application
Real-time ECG visualization with R-peak detection and BPM monitoring
for one or more Polar H10 devices at the same time.
"""

import asyncio
import os
import sys
from datetime import datetime
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QListWidget, QMainWindow, QPushButton, QSplitter,
    QVBoxLayout, QWidget,
)

from config import BPM_AVERAGE_WINDOW_SECONDS, DEVICE_SCAN_TIMEOUT, SHOW_RPEAKS_DEFAULT
from device_panel import DevicePanel, QtBleBridge
from polar_backend import BleLoop, PolarH10Handler

SHUTDOWN_TIMEOUT_S = 5.0

_DISABLED = "QPushButton:disabled { background-color: #BDBDBD; color: #757575; }"
STYLE_BLUE = "QPushButton { background-color: #2196F3; color: white; font-weight: bold; }" + _DISABLED
STYLE_GREEN = "QPushButton { background-color: #4CAF50; color: white; font-weight: bold; }" + _DISABLED
STYLE_RED = "QPushButton { background-color: #f44336; color: white; font-weight: bold; }" + _DISABLED


class PolarECGViewer(QMainWindow):
    """Main window: scan for devices, connect any number, one panel per device"""

    def __init__(self):
        super().__init__()
        self.ble_loop = BleLoop()
        self.ble = QtBleBridge(self.ble_loop)
        self.panels: Dict[str, DevicePanel] = {}  # device name -> panel
        self.save_folder: Optional[str] = None    # where all recordings are written
        self._init_ui()

    def _init_ui(self):
        self.setWindowTitle("Polar ECG Viewer - Teaching Demo")
        self.resize(1400, 900)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)

        # Device selection
        box = QGroupBox("Devices")
        row = QHBoxLayout(box)
        self.scan_btn = QPushButton("Scan Devices")
        self.scan_btn.clicked.connect(self.scan_devices)
        row.addWidget(self.scan_btn)
        self.device_list = QListWidget()
        self.device_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.device_list.setMaximumHeight(70)
        self.device_list.itemSelectionChanged.connect(self._update_connect_button)
        row.addWidget(self.device_list, stretch=1)
        self.connect_btn = QPushButton("Connect Selected")
        self.connect_btn.setEnabled(False)
        self.connect_btn.clicked.connect(self.connect_selected)
        row.addWidget(self.connect_btn)
        layout.addWidget(box)

        # Shared controls: every button acts on ALL connected devices
        box = QGroupBox("Controls (all devices)")
        row = QHBoxLayout(box)
        self.stream_btn = QPushButton("Start Streaming")
        self.stream_btn.clicked.connect(self.toggle_streaming)
        row.addWidget(self.stream_btn)
        self.folder_btn = QPushButton("Select Save Folder")
        self.folder_btn.clicked.connect(self.select_save_folder)
        row.addWidget(self.folder_btn)
        self.record_btn = QPushButton("Start Recording")
        self.record_btn.clicked.connect(self.toggle_recording)
        row.addWidget(self.record_btn)
        self.disconnect_btn = QPushButton("Disconnect All")
        self.disconnect_btn.clicked.connect(self.disconnect_all)
        row.addWidget(self.disconnect_btn)
        row.addSpacing(20)
        self.rpeak_toggle = QCheckBox("Show R-peaks")
        self.rpeak_toggle.setChecked(SHOW_RPEAKS_DEFAULT)
        self.rpeak_toggle.toggled.connect(self.set_show_rpeaks)
        row.addWidget(self.rpeak_toggle)
        self.add_avg_btn = QPushButton(f"Add Average Line ({BPM_AVERAGE_WINDOW_SECONDS}s)")
        self.add_avg_btn.clicked.connect(self.add_average_lines)
        row.addWidget(self.add_avg_btn)
        self.clear_avg_btn = QPushButton("Clear Lines")
        self.clear_avg_btn.clicked.connect(self.clear_average_lines)
        row.addWidget(self.clear_avg_btn)
        row.addStretch()
        layout.addWidget(box)

        # One panel per connected device, side by side
        self.placeholder = QLabel("No devices connected.\nScan, select one or more devices and click Connect.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        layout.addWidget(self.placeholder, stretch=1)
        layout.addWidget(self.splitter, stretch=1)
        self.splitter.hide()

        self.statusBar().showMessage("Ready - click 'Scan Devices' to start")
        self._update_controls()

    # ------------------------------------------------------------ scanning

    def scan_devices(self):
        self.scan_btn.setEnabled(False)
        self.scan_btn.setText("Scanning...")
        self.statusBar().showMessage("Scanning for Polar devices...")
        self.ble.run(PolarH10Handler.scan_devices(DEVICE_SCAN_TIMEOUT), self._on_scan_finished)

    def _on_scan_finished(self, error: Optional[BaseException], names: Optional[List[str]]):
        self.scan_btn.setEnabled(True)
        self.scan_btn.setText("Scan Devices")
        self.device_list.clear()
        if error is not None:
            self.statusBar().showMessage(f"Scan failed: {error} (is Bluetooth on?)")
            return
        new_names = [n for n in names if n not in self.panels]  # already connected ones are hidden
        self.device_list.addItems(new_names)
        if new_names:
            self.statusBar().showMessage(f"Found {len(new_names)} device(s) - select and connect")
        else:
            self.statusBar().showMessage("No new Polar devices found")

    def _update_connect_button(self):
        self.connect_btn.setEnabled(bool(self.device_list.selectedItems()))

    # ---------------------------------------------------------- connecting

    def connect_selected(self):
        for item in self.device_list.selectedItems():
            name = item.text()
            if name not in self.panels:
                self._add_panel(name)
            self.device_list.takeItem(self.device_list.row(item))
        self._update_connect_button()

    def _add_panel(self, name: str):
        panel = DevicePanel(name, self.ble)
        panel.closed.connect(self._on_panel_closed)
        panel.state_changed.connect(self._update_controls)
        panel.connected.connect(lambda n: self.statusBar().showMessage(f"Connected to {n}"))
        panel.set_show_rpeaks(self.rpeak_toggle.isChecked())
        self.panels[name] = panel
        self.splitter.addWidget(panel)
        self.placeholder.hide()
        self.splitter.show()
        self.statusBar().showMessage(f"Connecting to {name}...")
        self._update_controls()

    def _on_panel_closed(self, panel: DevicePanel, message: str):
        """Panel failed to connect or was disconnected: remove it"""
        panel.shutdown()
        self.panels.pop(panel.device_name, None)
        panel.hide()
        panel.setParent(None)
        panel.deleteLater()
        if not self.panels:
            self.splitter.hide()
            self.placeholder.show()
        self.statusBar().showMessage(message)
        self._update_controls()

    # ------------------------------------------------------ shared controls

    def _update_controls(self):
        """Enable/label the shared buttons from the state of all panels"""
        panels = list(self.panels.values())
        ready = [p for p in panels if p.ready]
        recording = any(p.recording for p in panels)

        all_streaming = bool(ready) and all(p.streaming for p in ready)
        self.stream_btn.setEnabled(bool(ready))
        self.stream_btn.setText("Stop Streaming" if all_streaming else "Start Streaming")
        self.stream_btn.setStyleSheet(STYLE_RED if all_streaming else STYLE_GREEN)

        self.folder_btn.setEnabled(not recording)
        self.folder_btn.setText(
            f"Folder: {os.path.basename(self.save_folder)} ✓" if self.save_folder else "Select Save Folder")
        self.folder_btn.setToolTip(self.save_folder or "")
        self.folder_btn.setStyleSheet(STYLE_GREEN if self.save_folder else STYLE_BLUE)

        can_record = bool(self.save_folder) and any(p.streaming for p in ready)
        self.record_btn.setEnabled(recording or can_record)
        self.record_btn.setText("Stop Recording" if recording else "Start Recording")
        self.record_btn.setStyleSheet(STYLE_RED if recording else STYLE_GREEN)

        self.disconnect_btn.setEnabled(bool(ready))
        self.add_avg_btn.setEnabled(any(p.can_add_average for p in panels))
        self.clear_avg_btn.setEnabled(any(p.has_average_lines for p in panels))

    def toggle_streaming(self):
        """Start streaming on all devices, or stop all if they are all streaming"""
        ready = [p for p in self.panels.values() if p.ready]
        if ready and all(p.streaming for p in ready):
            for p in ready:
                p.stop_streaming()
        else:
            for p in ready:
                p.start_streaming()

    def select_save_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select folder for ECG recordings", self.save_folder or "")
        if folder:
            self.save_folder = folder
            self.statusBar().showMessage(f"Recordings will be saved to: {folder}")
            self._update_controls()

    def toggle_recording(self):
        """Start recording on all streaming devices, or stop all recordings"""
        panels = list(self.panels.values())
        if any(p.recording for p in panels):
            for p in panels:
                p.stop_recording()
            self.statusBar().showMessage("Recording stopped")
            return

        targets = [p for p in panels if p.streaming]
        missing = [p.device_name for p in targets if not p.participant_id]
        if missing:
            self.statusBar().showMessage("Enter a Participant ID for: " + ", ".join(missing))
            return
        ids = [p.participant_id for p in targets]
        if len(set(ids)) != len(ids):
            self.statusBar().showMessage("Each device needs a different Participant ID")
            return

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for p in targets:
            error = p.start_recording(self.save_folder, stamp)
            if error:
                for started in targets:  # all or nothing
                    started.stop_recording()
                self.statusBar().showMessage(f"Recording not started: {error}")
                return
        self.statusBar().showMessage(f"Recording {len(targets)} device(s) to: {self.save_folder}")

    def disconnect_all(self):
        for p in [p for p in self.panels.values() if p.ready]:
            p.disconnect_device()

    def set_show_rpeaks(self, show: bool):
        for p in self.panels.values():
            p.set_show_rpeaks(show)

    def add_average_lines(self):
        for p in self.panels.values():
            if p.can_add_average:
                p.add_average_line()

    def clear_average_lines(self):
        for p in self.panels.values():
            p.clear_average_lines()

    # ------------------------------------------------------------ shutdown

    def closeEvent(self, event):
        panels = list(self.panels.values())
        for panel in panels:
            panel.shutdown()  # stops timers, closes recording files

        async def disconnect_all():
            await asyncio.gather(*(p.handler.disconnect() for p in panels), return_exceptions=True)

        try:
            self.ble_loop.run_sync(disconnect_all(), SHUTDOWN_TIMEOUT_S)
        except Exception as e:
            print(f"Warning: could not disconnect cleanly: {e}")
        self.ble_loop.stop()
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyle('Fusion')

    window = PolarECGViewer()
    window.show()

    sys.exit(app.exec())


if __name__ == '__main__':
    main()
