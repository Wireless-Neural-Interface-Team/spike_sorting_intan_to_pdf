#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Qt GUI to run the full Intan -> sorting -> PDF pipeline.

This script lets a user fill all required inputs from a form instead of
editing the main Python script manually.

High-level behavior:
  - Collect runtime paths and processing parameters from the user.
  - Run the full pipeline in a separate process (multiprocessing).
  - Allows immediate stop via process.terminate() (threads cannot be killed).
  - Keep all Qt UI updates on the main thread (thread-safe).
  - Show progress and errors in a log panel.
"""

import os
import json
import time
import copy
import subprocess
import sys
from datetime import datetime
import ctypes
import threading
import multiprocessing
from queue import Empty
from collections import defaultdict

# Constantes réutilisables
JSON_FILTER = "JSON files (*.json);;All files (*.*)"

from gui_components import (
    ChannelsLoaderWorker,
    EmbeddedMEAEditor,
    MEA_EDITOR_AVAILABLE,
    save_electrodes_to_file,
)
from gui_pipeline_runner import run_pipeline_in_process, is_file_in_use_error

from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QCheckBox,
    QRadioButton,
    QButtonGroup,
    QComboBox,
    QGroupBox,
    QTextEdit,
    QProgressBar,
    QFileDialog,
    QMessageBox,
    QMenu,
    QSizePolicy,
    QDoubleSpinBox,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QScrollArea,
    QFrame,
)
from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QAction

from protocol_class import (
    apply_preprocessing_filter_to_dict,
    default_protocol_params,
    get_preprocessing_filter_freqs,
)

try:
    from spikeinterface.sorters import available_sorters
    SORTERS_AVAILABLE = True
except ImportError:
    SORTERS_AVAILABLE = False
    available_sorters = lambda: ["tridesclous2"]


class PipelineGUI(QMainWindow):
    # Signals for thread-safe GUI updates (emitted from worker, handled on main thread)
    log_signal = Signal(str)
    progress_signal = Signal(bool)
    pipeline_done_signal = Signal(bool, object)  # (success, payload)

    def __init__(self):
        super().__init__()
        self.setWindowTitle("SpikeSorting Pipeline Launcher")
        self.resize(960, 680)
        self._session_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_last_session.json")

        # Form field values (we use QLineEdit.text() etc. directly, no StringVar)
        self._trigger_widgets = []
        self._channels_load_thread = None
        self._channels_load_worker = None
        self._channels_debounce_timer = None
        self._mea_editor_window = None
        self._probe_temp_path = None
        self._last_probe_from_mea_editor = False
        self._mea_editor_sync_timer = None
        self._probe_path = ""
        self._stop_requested = False
        self._has_successful_pipeline = False
        self._last_success_results_path = None
        # Pipeline runs in a subprocess for immediate stop capability
        self._pipeline_process = None  # multiprocessing.Process instance
        self._log_queue = None  # multiprocessing.Queue for log messages from child
        self._queue_reader_thread = None  # Thread that reads queue and updates GUI
        self.log_signal.connect(self._log_impl)
        self.progress_signal.connect(self._progress_impl)
        self.pipeline_done_signal.connect(self._on_pipeline_done)
        self._build_ui()
        self._load_last_session()

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        main_layout = QVBoxLayout(central)
        main_layout.setContentsMargins(12, 12, 12, 12)

        # Top bar: File menu
        top_bar = QHBoxLayout()
        self._file_btn = QPushButton("File")
        self._file_btn.setFixedWidth(120)
        file_menu = QMenu(self)
        save_action = QAction("Save settings", self)
        save_action.triggered.connect(self._save_settings_as)
        load_action = QAction("Load settings", self)
        load_action.triggered.connect(self._load_config_from_file)
        file_menu.addAction(save_action)
        file_menu.addAction(load_action)
        self._file_btn.setMenu(file_menu)
        top_bar.addWidget(self._file_btn)
        top_bar.addStretch()
        main_layout.addLayout(top_bar)

        # Main content: 2 columns
        content = QHBoxLayout()
        content.setSpacing(12)

        # Left column: folder, probe, sorter, protocol
        left_widget = QWidget()
        left_layout = QGridLayout(left_widget)
        left_layout.setColumnStretch(1, 1)

        r = 0
        left_layout.addWidget(QLabel("Intan files folder path"), r, 0)
        self.folder_edit = QLineEdit()
        self.folder_edit.setMinimumWidth(400)
        self.folder_edit.editingFinished.connect(self._schedule_refresh_channels)
        left_layout.addWidget(self.folder_edit, r, 1)
        self._folder_btn = QPushButton("Browse")
        self._folder_btn.clicked.connect(self._on_folder_browse)
        left_layout.addWidget(self._folder_btn, r, 2)
        r += 1

        left_layout.addWidget(QLabel("Channels in file"), r, 0)
        self.channels_display = QTableWidget()
        self.channels_display.setMaximumWidth(280)
        self.channels_display.setMinimumHeight(280)
        self.channels_display.setMaximumHeight(400)
        self.channels_display.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.channels_display.verticalHeader().setVisible(False)
        left_layout.addWidget(self.channels_display, r, 1, 1, 2)
        r += 1

        left_layout.addWidget(QLabel("Probe"), r, 0)
        self.probe_name_display = QLineEdit()
        self.probe_name_display.setReadOnly(True)
        self.probe_name_display.setPlaceholderText("—")
        self.probe_name_display.setToolTip(
            "Probe loaded via MEA Editor. The pipeline always uses the version displayed in the editor (including modifications)."
        )
        left_layout.addWidget(self.probe_name_display, r, 1)
        self._probe_edit_btn = QPushButton("Load / Edit probe")
        self._probe_edit_btn.clicked.connect(self._open_mea_editor)
        self._probe_edit_btn.setEnabled(MEA_EDITOR_AVAILABLE)
        left_layout.addWidget(self._probe_edit_btn, r, 2)
        r += 1

        left_layout.addWidget(QLabel("Sorter"), r, 0)
        self.sorter_combo = QComboBox()
        self.sorter_combo.setMinimumWidth(180)
        self._populate_sorter_combo()
        self.sorter_combo.currentTextChanged.connect(self._on_sorter_changed)
        left_layout.addWidget(self.sorter_combo, r, 1)
        r += 1

        content.addWidget(left_widget)

        # Right column: Trigger section (independent from left table)
        trigger_group = QGroupBox("Trigger")
        trigger_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        trigger_layout = QGridLayout(trigger_group)
        trigger_layout.setColumnStretch(1, 1)

        t = 0
        self.use_trigger_cb = QCheckBox("Use trigger detection")
        self.use_trigger_cb.setChecked(True)
        self.use_trigger_cb.toggled.connect(self._toggle_trigger_fields_state)
        trigger_layout.addWidget(self.use_trigger_cb, t, 0, 1, 2)
        t += 1

        trigger_type_row = QWidget()
        trigger_type_row_layout = QHBoxLayout(trigger_type_row)
        trigger_type_row_layout.setContentsMargins(0, 0, 0, 0)
        trigger_type_row_layout.setSpacing(8)
        trigger_type_row_layout.addWidget(QLabel("Trigger type:"))
        self.trigger_type_group = QButtonGroup()
        self.rb_led = QRadioButton("LED")
        self.rb_electric = QRadioButton("Electric")
        self.rb_electric.setChecked(True)
        self.trigger_type_group.addButton(self.rb_led)
        self.trigger_type_group.addButton(self.rb_electric)
        self.rb_led.toggled.connect(self._on_trigger_type_change)
        self.rb_electric.toggled.connect(self._on_trigger_type_change)
        trigger_type_row_layout.addWidget(self.rb_led)
        trigger_type_row_layout.addWidget(self.rb_electric)
        trigger_layout.addWidget(trigger_type_row, t, 0, 1, 2)
        self._trigger_widgets.extend([self.rb_led, self.rb_electric])
        t += 1

        trigger_layout.addWidget(QLabel("Trigger threshold"), t, 0)
        self.trigger_threshold_edit = QLineEdit()
        self.trigger_threshold_edit.setText("37000")
        self.trigger_threshold_edit.setMaximumWidth(150)
        trigger_layout.addWidget(self.trigger_threshold_edit, t, 1)
        self._trigger_widgets.append(self.trigger_threshold_edit)
        t += 1

        trigger_layout.addWidget(QLabel("Trigger polarity"), t, 0)
        self.polarity_combo = QComboBox()
        self.polarity_combo.addItems(["Rising Edge", "Falling Edge"])
        self.polarity_combo.setCurrentText("Falling Edge")
        self.polarity_combo.setMaximumWidth(150)
        trigger_layout.addWidget(self.polarity_combo, t, 1)
        self._trigger_widgets.append(self.polarity_combo)
        t += 1

        trigger_layout.addWidget(QLabel("Minimum elapsed time between triggers (s)"), t, 0)
        self.trigger_interval_edit = QLineEdit()
        self.trigger_interval_edit.setText("5.1")
        self.trigger_interval_edit.setMaximumWidth(150)
        trigger_layout.addWidget(self.trigger_interval_edit, t, 1)
        self._trigger_widgets.append(self.trigger_interval_edit)
        t += 1

        trigger_layout.addWidget(QLabel("Trigger channel index"), t, 0)
        self.trigger_channel_edit = QLineEdit()
        self.trigger_channel_edit.setText("0")
        self.trigger_channel_edit.setMaximumWidth(150)
        trigger_layout.addWidget(self.trigger_channel_edit, t, 1)
        self._trigger_widgets.append(self.trigger_channel_edit)

        # Protocol section: Preprocessing and Postprocessing separated
        protocol_container = QWidget()
        protocol_container_layout = QVBoxLayout(protocol_container)
        protocol_container_layout.setContentsMargins(0, 0, 0, 0)
        protocol_content = QWidget()
        protocol_main = QVBoxLayout(protocol_content)

        default = default_protocol_params(400, 5000)
        self._protocol_params = copy.deepcopy(default)

        # --- Preprocessing group ---
        preprocessing_group = QGroupBox("Preprocessing")
        preprocessing_group.setToolTip("Applied to the recording before spike sorting")
        preprocessing_layout = QGridLayout(preprocessing_group)
        preprocessing_layout.setColumnStretch(1, 1)

        prep = 0
        preprocessing_layout.addWidget(QLabel("Filter type"), prep, 0)
        self.protocol_filter_type = QComboBox()
        for text, data in (
            ("Bandpass", "bandpass"),
            ("Highpass", "highpass"),
            ("Lowpass (Gaussian)", "lowpass"),
        ):
            self.protocol_filter_type.addItem(text, data)
        self.protocol_filter_type.setMaximumWidth(200)
        self.protocol_filter_type.currentIndexChanged.connect(self._on_preprocessing_filter_type_changed)
        preprocessing_layout.addWidget(self.protocol_filter_type, prep, 1)
        prep += 1

        self._prep_freq_min_label = QLabel("Bandpass freq min (Hz)")
        preprocessing_layout.addWidget(self._prep_freq_min_label, prep, 0)
        self.protocol_freq_min = QDoubleSpinBox()
        self.protocol_freq_min.setRange(1, 20000)
        self.protocol_freq_min.setValue(400)
        self.protocol_freq_min.setDecimals(0)
        self.protocol_freq_min.setMaximumWidth(120)
        self.protocol_freq_min.valueChanged.connect(self._update_protocol_from_form)
        preprocessing_layout.addWidget(self.protocol_freq_min, prep, 1)
        prep += 1

        self._prep_freq_max_label = QLabel("Bandpass freq max (Hz)")
        preprocessing_layout.addWidget(self._prep_freq_max_label, prep, 0)
        self.protocol_freq_max = QDoubleSpinBox()
        self.protocol_freq_max.setRange(1, 20000)
        self.protocol_freq_max.setValue(5000)
        self.protocol_freq_max.setDecimals(0)
        self.protocol_freq_max.setMaximumWidth(120)
        self.protocol_freq_max.valueChanged.connect(self._update_protocol_from_form)
        preprocessing_layout.addWidget(self.protocol_freq_max, prep, 1)

        self._sync_preprocessing_filter_widgets()
        protocol_main.addWidget(preprocessing_group)

        # --- Postprocessing group ---
        postprocessing_group = QGroupBox("Postprocessing")
        postprocessing_group.setToolTip("Used by the SortingAnalyzer for analysis and visualization")
        postprocessing_layout = QGridLayout(postprocessing_group)
        postprocessing_layout.setColumnStretch(1, 1)

        post = 0
        postprocessing_layout.addWidget(QLabel("Unit locations method"), post, 0)
        self.protocol_unit_locations_method = QComboBox()
        self.protocol_unit_locations_method.addItems(["center_of_mass", "monopolar_triangulation"])
        self.protocol_unit_locations_method.setMaximumWidth(180)
        self.protocol_unit_locations_method.currentTextChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_unit_locations_method, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Random spikes max_spikes_per_unit"), post, 0)
        self.protocol_random_spikes_max = QSpinBox()
        self.protocol_random_spikes_max.setRange(1, 10000)
        self.protocol_random_spikes_max.setValue(1000)
        self.protocol_random_spikes_max.setMaximumWidth(120)
        self.protocol_random_spikes_max.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_random_spikes_max, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Waveforms ms_before"), post, 0)
        self.protocol_waveforms_ms_before = QDoubleSpinBox()
        self.protocol_waveforms_ms_before.setRange(0.1, 10)
        self.protocol_waveforms_ms_before.setValue(1.0)
        self.protocol_waveforms_ms_before.setSingleStep(0.1)
        self.protocol_waveforms_ms_before.setMaximumWidth(120)
        self.protocol_waveforms_ms_before.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_waveforms_ms_before, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Waveforms ms_after"), post, 0)
        self.protocol_waveforms_ms_after = QDoubleSpinBox()
        self.protocol_waveforms_ms_after.setRange(0.1, 10)
        self.protocol_waveforms_ms_after.setValue(2.0)
        self.protocol_waveforms_ms_after.setSingleStep(0.1)
        self.protocol_waveforms_ms_after.setMaximumWidth(120)
        self.protocol_waveforms_ms_after.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_waveforms_ms_after, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Correlograms window_ms"), post, 0)
        self.protocol_correlograms_window = QDoubleSpinBox()
        self.protocol_correlograms_window.setRange(1, 500)
        self.protocol_correlograms_window.setValue(50.0)
        self.protocol_correlograms_window.setMaximumWidth(120)
        self.protocol_correlograms_window.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_correlograms_window, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Correlograms bin_ms"), post, 0)
        self.protocol_correlograms_bin = QDoubleSpinBox()
        self.protocol_correlograms_bin.setRange(0.1, 10)
        self.protocol_correlograms_bin.setValue(1.0)
        self.protocol_correlograms_bin.setSingleStep(0.1)
        self.protocol_correlograms_bin.setMaximumWidth(120)
        self.protocol_correlograms_bin.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_correlograms_bin, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Spike amplitudes peak_sign"), post, 0)
        self.protocol_spike_amplitudes_peak = QComboBox()
        self.protocol_spike_amplitudes_peak.addItems(["neg", "pos"])
        self.protocol_spike_amplitudes_peak.setMaximumWidth(120)
        self.protocol_spike_amplitudes_peak.currentTextChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_spike_amplitudes_peak, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Template similarity method"), post, 0)
        self.protocol_template_similarity_method = QComboBox()
        self.protocol_template_similarity_method.addItems(["cosine_similarity"])
        self.protocol_template_similarity_method.setMaximumWidth(180)
        self.protocol_template_similarity_method.currentTextChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_template_similarity_method, post, 1)
        post += 1

        postprocessing_layout.addWidget(QLabel("Template metrics multi_channel"), post, 0)
        self.protocol_template_metrics_multi = QCheckBox("")
        self.protocol_template_metrics_multi.setChecked(False)
        self.protocol_template_metrics_multi.toggled.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_template_metrics_multi, post, 1)

        protocol_main.addWidget(postprocessing_group)

        # Protocol buttons (Load / Reset apply to full protocol)
        protocol_btns = QHBoxLayout()
        self._protocol_load_btn = QPushButton("Load protocol")
        self._protocol_load_btn.clicked.connect(self._load_protocol_from_file)
        self._protocol_reset_btn = QPushButton("Reset to defaults")
        self._protocol_reset_btn.clicked.connect(self._reset_protocol_defaults)
        protocol_btns.addWidget(self._protocol_load_btn)
        protocol_btns.addWidget(self._protocol_reset_btn)
        protocol_btns.addStretch()
        protocol_main.addLayout(protocol_btns)

        protocol_container_layout.addWidget(protocol_content)
        content.addWidget(protocol_container)
        content.addWidget(trigger_group)
        main_layout.addLayout(content)
        self._update_protocol_from_form()  # Sync initial form values to dict

        # Run / Stop controls
        controls = QHBoxLayout()
        self._run_button = QPushButton("Run Pipeline")
        self._run_button.setFixedWidth(150)
        self._run_button.clicked.connect(self._run_pipeline_async)
        controls.addWidget(self._run_button)
        self._stop_button = QPushButton("Stop Pipeline")
        self._stop_button.setFixedWidth(150)
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._request_stop)
        controls.addWidget(self._stop_button)
        self._clear_logs_btn = QPushButton("Clear Logs")
        self._clear_logs_btn.setFixedWidth(150)
        self._clear_logs_btn.clicked.connect(self._clear_logs)
        controls.addWidget(self._clear_logs_btn)
        self._launch_sigui_btn = QPushButton("Launch spikeinterface-gui")
        self._launch_sigui_btn.setFixedWidth(210)
        self._launch_sigui_btn.clicked.connect(self._prompt_and_launch_spikeinterface_gui)
        controls.addWidget(self._launch_sigui_btn)
        controls.addStretch()
        main_layout.addLayout(controls)

        # Logs
        main_layout.addWidget(QLabel("Logs"))
        self._progressbar = QProgressBar()
        self._progressbar.setRange(0, 0)  # indeterminate
        self._progressbar.setVisible(False)
        main_layout.addWidget(self._progressbar)

        self.logs = QTextEdit()

        # Widgets to disable when pipeline is running
        self._form_widgets = [
            self._file_btn, self.folder_edit, self._folder_btn,
            self.channels_display,
            self.probe_name_display, self._probe_edit_btn, self.sorter_combo,
            *self._get_protocol_form_widgets(),
            self._protocol_load_btn, self._protocol_reset_btn,
            self.use_trigger_cb, self.rb_led, self.rb_electric,
            self.trigger_threshold_edit, self.polarity_combo,
            self.trigger_interval_edit, self.trigger_channel_edit,
            self._clear_logs_btn,
        ]
        self.logs.setReadOnly(True)
        self.logs.setMinimumHeight(200)
        main_layout.addWidget(self.logs, 1)

        self._toggle_trigger_fields_state()

    def _toggle_trigger_fields_state(self):
        enabled = self.use_trigger_cb.isChecked()
        for w in self._trigger_widgets:
            w.setEnabled(enabled)

    def _populate_sorter_combo(self):
        """Fill sorter combo with available sorters from SpikeInterface."""
        self.sorter_combo.blockSignals(True)
        self.sorter_combo.clear()
        if SORTERS_AVAILABLE:
            sorters = sorted(available_sorters())
            self.sorter_combo.addItems(sorters)
        else:
            self.sorter_combo.addItem("tridesclous2")
        idx = self.sorter_combo.findText("tridesclous2")
        if idx >= 0:
            self.sorter_combo.setCurrentIndex(idx)
        self.sorter_combo.blockSignals(False)

    def _on_sorter_changed(self):
        """When sorter selection changes, persist session only."""
        self._save_last_session()

    def _on_trigger_type_change(self):
        preset = {"led": ("37000", "Falling Edge", "5.1"), "electric": ("39000", "Rising Edge", "5.1")}
        t = "led" if self.rb_led.isChecked() else "electric"
        if t in preset:
            thresh, polarity, interval = preset[t]
            self.trigger_threshold_edit.setText(thresh)
            self.polarity_combo.setCurrentText(polarity)
            self.trigger_interval_edit.setText(interval)
        self._save_last_session()

    def _polarity_to_edge(self, polarity_str):
        """Convert 'Rising Edge' / 'Falling Edge' to 1 / -1 for Trigger."""
        if polarity_str.strip() == "Rising Edge":
            return 1
        if polarity_str.strip() == "Falling Edge":
            return -1
        raise ValueError("trigger polarity must be 'Rising Edge' or 'Falling Edge'.")

    def _edge_to_polarity(self, edge):
        return "Rising Edge" if edge == 1 else "Falling Edge"

    def _collect_form_state(self):
        """Collect all form values for save/load settings."""
        state = {
            "folder_path": self.folder_edit.text(),
            "use_trigger": self.use_trigger_cb.isChecked(),
            "trigger_type": "led" if self.rb_led.isChecked() else "electric",
            "trigger_threshold": self.trigger_threshold_edit.text(),
            "trigger_polarity": self.polarity_combo.currentText(),
            "trigger_min_interval": self.trigger_interval_edit.text(),
            "trigger_channel_index": self.trigger_channel_edit.text(),
            "sorter_name": self.sorter_combo.currentText(),
            "my_probe_path": self._probe_path,
        }
        state["protocol_params"] = copy.deepcopy(self._protocol_params)
        return state

    def _apply_form_state(self, state):
        if not isinstance(state, dict):
            return
        self.folder_edit.setText(state.get("folder_path", self.folder_edit.text()))
        self.use_trigger_cb.setChecked(bool(state.get("use_trigger", True)))
        t = state.get("trigger_type", "electric")
        self.rb_led.setChecked(t == "led")
        self.rb_electric.setChecked(t == "electric")
        self.trigger_threshold_edit.setText(state.get("trigger_threshold", self.trigger_threshold_edit.text()))
        polarity = state.get("trigger_polarity") or state.get("trigger_edge")
        if polarity in ("-1", "1"):
            polarity = self._edge_to_polarity(int(polarity))
        if polarity in ("Rising Edge", "Falling Edge"):
            self.polarity_combo.setCurrentText(polarity)
        self.trigger_interval_edit.setText(state.get("trigger_min_interval", self.trigger_interval_edit.text()))
        self.trigger_channel_edit.setText(state.get("trigger_channel_index", self.trigger_channel_edit.text()))
        sorter_name = state.get("sorter_name", "tridesclous2")
        idx = self.sorter_combo.findText(sorter_name)
        if idx >= 0:
            self.sorter_combo.setCurrentIndex(idx)
        else:
            self.sorter_combo.setCurrentText(sorter_name)
        self._set_probe_path(state.get("my_probe_path", self._probe_path))
        self._on_probe_path_changed()
        protocol_params = state.get("protocol_params")
        if isinstance(protocol_params, dict):
            # Ignore any legacy/custom sorter params to keep sorter defaults untouched.
            sanitized_protocol = copy.deepcopy(protocol_params)
            sanitized_protocol.pop("sorter_params", None)
            self._protocol_params = sanitized_protocol
            self._apply_protocol_to_form(sanitized_protocol)
        if state.get("protocol_freq_min") is not None or state.get("protocol_freq_max") is not None:
            self.protocol_freq_min.setValue(float(state.get("protocol_freq_min", 400)))
            self.protocol_freq_max.setValue(float(state.get("protocol_freq_max", 5000)))
            self._update_protocol_from_form()
        self._refresh_intan_channels()

    def _load_last_session(self):
        if not os.path.isfile(self._session_file):
            return
        try:
            with open(self._session_file, "r", encoding="utf-8") as f:
                self._apply_form_state(json.load(f))
        except Exception:
            pass

    def _save_last_session(self):
        """Save settings to the default session file (auto-save on close, etc.)."""
        try:
            with open(self._session_file, "w", encoding="utf-8") as f:
                json.dump(self._collect_form_state(), f, indent=2, ensure_ascii=True)
        except Exception:
            pass

    def _save_settings_as(self):
        """Open a file dialog to choose where to save the settings."""
        path, _ = QFileDialog.getSaveFileName(self, "Save settings", "", JSON_FILTER)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._collect_form_state(), f, indent=2, ensure_ascii=True)
            self._save_last_session()  # Also update auto-restore file
            QMessageBox.information(self, "Settings saved", f"Settings saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def closeEvent(self, event):
        self._stop_mea_editor_sync_timer()
        if MEA_EDITOR_AVAILABLE and self._mea_editor_window is not None:
            self._mea_editor_window.close()
        self._save_last_session()
        event.accept()

    def _on_folder_browse(self):
        self._browse_path("folder", self.folder_edit)
        self._refresh_intan_channels()

    def _schedule_refresh_channels(self):
        """Debounce: delay channel load to avoid repeated loads while typing."""
        if self._channels_debounce_timer is not None:
            self._channels_debounce_timer.stop()
        self._channels_debounce_timer = QTimer(self)
        self._channels_debounce_timer.setSingleShot(True)
        self._channels_debounce_timer.timeout.connect(self._refresh_intan_channels)
        self._channels_debounce_timer.start(400)

    def _refresh_intan_channels(self):
        """Start background load of channel IDs (non-blocking)."""
        folder_path = self.folder_edit.text().strip()
        self._populate_channels_table([])
        if not folder_path or not os.path.isdir(folder_path):
            return
        self._populate_channels_table(None)  # show "Loading..."
        worker = ChannelsLoaderWorker(folder_path)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_channels_loaded)
        worker.finished.connect(thread.quit)
        thread.start()
        self._channels_load_thread = thread
        self._channels_load_worker = worker

    def _on_channels_loaded(self, folder_path, channel_ids):
        """Called when channel load completes (on main thread). Ignore stale results."""
        if folder_path != self.folder_edit.text().strip():
            return
        self._populate_channels_table(channel_ids)

    def _populate_channels_table(self, channel_ids):
        """Fill the channels table. channel_ids=None -> show 'Loading...', [] -> clear."""
        self.channels_display.setRowCount(0)
        self.channels_display.setColumnCount(0)
        if channel_ids is None:
            self.channels_display.setRowCount(1)
            self.channels_display.setColumnCount(1)
            item = QTableWidgetItem("Loading...")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.channels_display.setItem(0, 0, item)
            return
        if not channel_ids:
            return
        by_letter = defaultdict(list)
        for ch in channel_ids:
            s = str(ch)
            letter = s[0].upper() if s and s[0].isalpha() else "#"
            by_letter[letter].append(s)
        letters = sorted((k for k in by_letter if k != "#"), key=str) + (["#"] if "#" in by_letter else [])
        for k in letters:
            by_letter[k].sort(key=lambda x: (len(x), x))
        n_cols = len(letters)
        n_rows = max(len(by_letter[k]) for k in letters) if letters else 0
        self.channels_display.setColumnCount(n_cols)
        self.channels_display.setRowCount(n_rows)
        self.channels_display.setHorizontalHeaderLabels(letters)
        for col, letter in enumerate(letters):
            for row, ch_id in enumerate(by_letter[letter]):
                item = QTableWidgetItem(ch_id)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.channels_display.setItem(row, col, item)

    def _set_probe_path(self, path):
        """Store probe path and display only the filename."""
        self._probe_path = path.strip() if path else ""
        name = os.path.basename(self._probe_path) if self._probe_path else ""
        self.probe_name_display.setText(name)
        self._save_last_session()

    def _on_probe_path_changed(self):
        """Reload MEA editor if open and path changed."""
        if self._mea_editor_window and MEA_EDITOR_AVAILABLE:
            path = self._probe_path
            if path and os.path.isfile(path) and path != getattr(self._mea_editor_window, "_initial_path", None):
                try:
                    self._mea_editor_window._initial_path = path
                    self._mea_editor_window._load_array_from_file(path)
                    self._mea_editor_window.current_file_path = ""
                    self._mea_editor_window.is_dirty = False
                    self._mea_editor_window._update_title()
                except Exception:
                    pass

    def _open_mea_editor(self):
        """Open MEA editor window. Load probe via File > Open in the editor."""
        if not MEA_EDITOR_AVAILABLE:
            QMessageBox.information(
                self,
                "MEA Editor",
                "Install mea-editor to edit probes: pip install mea-editor",
            )
            return
        path = self._probe_path if (self._probe_path and os.path.isfile(self._probe_path)) else ""
        if self._mea_editor_window is None:
            self._mea_editor_window = EmbeddedMEAEditor(
                path,
                on_file_loaded=self._on_probe_file_loaded,
                on_close_callback=self._on_mea_editor_closed,
            )
        # Si la fenêtre existe déjà, on l'affiche sans recharger : garder la version modifiée
        self._mea_editor_window.show()
        self._mea_editor_window.raise_()
        self._mea_editor_window.activateWindow()
        self._start_mea_editor_sync_timer()

    def _on_probe_file_loaded(self, path):
        """Called when a probe file is loaded in MEA Editor (immediate update)."""
        if path and os.path.isfile(path):
            self._set_probe_path(path)

    def _on_mea_editor_closed(self, path):
        """Called when MEA Editor is closed, with the path of the loaded probe (if any)."""
        self._stop_mea_editor_sync_timer()
        if path and os.path.isfile(path):
            self._set_probe_path(path)

    def _start_mea_editor_sync_timer(self):
        """Start timer to sync probe display with MEA Editor state (modifications)."""
        self._stop_mea_editor_sync_timer()
        self._mea_editor_sync_timer = QTimer(self)
        self._mea_editor_sync_timer.timeout.connect(self._sync_probe_display_from_mea_editor)
        self._mea_editor_sync_timer.start(1500)

    def _stop_mea_editor_sync_timer(self):
        """Stop the MEA Editor sync timer."""
        if self._mea_editor_sync_timer:
            self._mea_editor_sync_timer.stop()
            self._mea_editor_sync_timer = None

    def _sync_probe_display_from_mea_editor(self):
        """Update probe display to reflect MEA Editor state (modified indicator)."""
        if not MEA_EDITOR_AVAILABLE or self._mea_editor_window is None:
            self._stop_mea_editor_sync_timer()
            return
        if not self._mea_editor_window.isVisible():
            self._stop_mea_editor_sync_timer()
            return
        path = getattr(self._mea_editor_window, "current_file_path", None) or self._mea_editor_window._initial_path
        base = os.path.basename(path) if path else ""
        if not base and list(self._mea_editor_window.electrodes.values()):
            base = "(unsaved probe)"
        dirty = getattr(self._mea_editor_window, "is_dirty", False)
        self.probe_name_display.setText(f"{base} *" if dirty and base else base)

    def _get_probe_path_for_pipeline(self, folder_path=None):
        """
        Return the probe file path to use for the pipeline.
        If MEA editor was opened and has content, ALWAYS export current state (all modifications)
        to a file in the recording folder so the subprocess can read it reliably.
        """
        if MEA_EDITOR_AVAILABLE and self._mea_editor_window is not None:
            electrodes = list(self._mea_editor_window.electrodes.values())
            if electrodes and folder_path and os.path.isdir(folder_path):
                try:
                    path = os.path.join(folder_path, "probe_pipeline_temp.json")
                    save_electrodes_to_file(path, electrodes, self._mea_editor_window.si_units)
                    self._probe_temp_path = path
                    self._last_probe_from_mea_editor = True
                    return path
                except Exception as exc:
                    raise ValueError(
                        f"Could not export probe from MEA Editor: {exc}\n"
                        "Fix errors in the MEA Editor (e.g. duplicate contact_ids) before running the pipeline."
                    ) from exc
        self._last_probe_from_mea_editor = False
        return self._probe_path

    def _browse_path(self, mode, target_edit, filter_ext=None):
        if mode == "folder":
            selected = QFileDialog.getExistingDirectory(self, "Select folder")
        else:
            filter_str = JSON_FILTER if filter_ext else "All files (*.*)"
            selected, _ = QFileDialog.getOpenFileName(self, "Select file", "", filter_str)
        if selected:
            target_edit.setText(selected)
            self._save_last_session()

    def _update_protocol_from_form(self):
        """Met à jour le dictionnaire protocol à chaque modification d'un champ."""
        p = self._protocol_params
        prep = p.setdefault("preprocessing", {})
        apply_preprocessing_filter_to_dict(
            prep,
            self.protocol_filter_type.currentData() or "bandpass",
            self.protocol_freq_min.value(),
            self.protocol_freq_max.value(),
        )
        p.setdefault("postprocessing", {}).setdefault("unit_locations", {})["method"] = self.protocol_unit_locations_method.currentText()
        p.setdefault("postprocessing", {}).setdefault("random_spikes", {})["max_spikes_per_unit"] = self.protocol_random_spikes_max.value()
        p.setdefault("postprocessing", {}).setdefault("waveforms", {})["ms_before"] = self.protocol_waveforms_ms_before.value()
        p.setdefault("postprocessing", {}).setdefault("waveforms", {})["ms_after"] = self.protocol_waveforms_ms_after.value()
        p.setdefault("postprocessing", {}).setdefault("correlograms", {})["window_ms"] = self.protocol_correlograms_window.value()
        p.setdefault("postprocessing", {}).setdefault("correlograms", {})["bin_ms"] = self.protocol_correlograms_bin.value()
        p.setdefault("postprocessing", {}).setdefault("spike_amplitudes", {})["peak_sign"] = self.protocol_spike_amplitudes_peak.currentText()
        p.setdefault("postprocessing", {}).setdefault("template_similarity", {})["method"] = self.protocol_template_similarity_method.currentText()
        p.setdefault("postprocessing", {}).setdefault("template_metrics", {})["include_multi_channel_metrics"] = self.protocol_template_metrics_multi.isChecked()
        self._save_last_session()

    def _get_protocol_form_widgets(self):
        """Liste des widgets protocol pour blockSignals."""
        return [
            self.protocol_filter_type,
            self.protocol_freq_min, self.protocol_freq_max, self.protocol_unit_locations_method,
            self.protocol_random_spikes_max, self.protocol_waveforms_ms_before, self.protocol_waveforms_ms_after,
            self.protocol_correlograms_window, self.protocol_correlograms_bin,
            self.protocol_spike_amplitudes_peak, self.protocol_template_similarity_method,
            self.protocol_template_metrics_multi,
        ]

    def _apply_protocol_to_form(self, params):
        """Remplit les champs à partir du dict protocol (bloque les signaux)."""
        widgets = self._get_protocol_form_widgets()
        for w in widgets:
            w.blockSignals(True)
        prep = params.get("preprocessing", {})
        ft = prep.get("filter_type", "bandpass")
        idx = self.protocol_filter_type.findData(ft)
        if idx >= 0:
            self.protocol_filter_type.setCurrentIndex(idx)
        else:
            self.protocol_filter_type.setCurrentIndex(0)
        self._sync_preprocessing_filter_widgets()
        _ft, fmin, fmax = get_preprocessing_filter_freqs(prep)
        self.protocol_freq_min.setValue(float(fmin))
        self.protocol_freq_max.setValue(float(fmax))
        ul = params.get("postprocessing", {}).get("unit_locations", {})
        method = ul.get("method", "center_of_mass") if isinstance(ul, dict) else "center_of_mass"
        idx = self.protocol_unit_locations_method.findText(method)
        if idx >= 0:
            self.protocol_unit_locations_method.setCurrentIndex(idx)
        rs = params.get("postprocessing", {}).get("random_spikes", {})
        self.protocol_random_spikes_max.setValue(int(rs.get("max_spikes_per_unit", 1000)))
        wf = params.get("postprocessing", {}).get("waveforms", {})
        self.protocol_waveforms_ms_before.setValue(float(wf.get("ms_before", 1.0)))
        self.protocol_waveforms_ms_after.setValue(float(wf.get("ms_after", 2.0)))
        cc = params.get("postprocessing", {}).get("correlograms", {})
        self.protocol_correlograms_window.setValue(float(cc.get("window_ms", 50.0)))
        self.protocol_correlograms_bin.setValue(float(cc.get("bin_ms", 1.0)))
        sa = params.get("postprocessing", {}).get("spike_amplitudes", {})
        idx = self.protocol_spike_amplitudes_peak.findText(sa.get("peak_sign", "neg"))
        if idx >= 0:
            self.protocol_spike_amplitudes_peak.setCurrentIndex(idx)
        ts = params.get("postprocessing", {}).get("template_similarity", {})
        idx = self.protocol_template_similarity_method.findText(ts.get("method", "cosine_similarity"))
        if idx >= 0:
            self.protocol_template_similarity_method.setCurrentIndex(idx)
        tm = params.get("postprocessing", {}).get("template_metrics", {})
        self.protocol_template_metrics_multi.setChecked(bool(tm.get("include_multi_channel_metrics", False)))
        for w in widgets:
            w.blockSignals(False)

    def _load_protocol_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select protocol file", "", JSON_FILTER)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                parsed = json.load(f)
            if not isinstance(parsed, dict):
                raise ValueError("File must contain a JSON object.")
            if "preprocessing" not in parsed or "postprocessing" not in parsed:
                raise ValueError("Protocol must contain 'preprocessing' and 'postprocessing' keys.")
            # Ignore any legacy/custom sorter params to keep sorter defaults untouched.
            parsed.pop("sorter_params", None)
            self._protocol_params = copy.deepcopy(parsed)
            self._apply_protocol_to_form(parsed)
            self._save_last_session()
            QMessageBox.information(self, "Protocol loaded", f"Protocol loaded from:\n{path}")
        except json.JSONDecodeError as e:
            QMessageBox.critical(self, "Invalid JSON", str(e))
        except (ValueError, TypeError) as e:
            QMessageBox.critical(self, "Invalid protocol", str(e))

    def _reset_protocol_defaults(self):
        default = default_protocol_params(400, 5000)
        self._protocol_params = copy.deepcopy(default)
        self._apply_protocol_to_form(default)
        self._save_last_session()

    def _clear_logs(self):
        self.logs.clear()

    def _on_preprocessing_filter_type_changed(self):
        self._sync_preprocessing_filter_widgets()
        self._update_protocol_from_form()

    def _sync_preprocessing_filter_widgets(self):
        ft = self.protocol_filter_type.currentData()
        if ft is None:
            ft = "bandpass"
        if ft == "bandpass":
            self._prep_freq_min_label.setText("Bandpass freq min (Hz)")
            self._prep_freq_max_label.setText("Bandpass freq max (Hz)")
            self._prep_freq_min_label.setVisible(True)
            self.protocol_freq_min.setVisible(True)
            self._prep_freq_max_label.setVisible(True)
            self.protocol_freq_max.setVisible(True)
        elif ft == "highpass":
            self._prep_freq_min_label.setText("Highpass cutoff (Hz)")
            self._prep_freq_min_label.setVisible(True)
            self.protocol_freq_min.setVisible(True)
            self._prep_freq_max_label.setVisible(False)
            self.protocol_freq_max.setVisible(False)
        else:
            self._prep_freq_max_label.setText("Lowpass cutoff (Hz)")
            self._prep_freq_min_label.setVisible(False)
            self.protocol_freq_min.setVisible(False)
            self._prep_freq_max_label.setVisible(True)
            self.protocol_freq_max.setVisible(True)

    def _get_spikeinterface_results_path(self, output_folder):
        """Resolve best results path for spikeinterface-gui from an output folder."""
        if not output_folder or not os.path.isdir(output_folder):
            return None
        sorter_name = self.sorter_combo.currentText().strip()
        analyzer_dir = os.path.join(output_folder, f"Analyzer_binary_pipeline_{sorter_name}")
        if os.path.isdir(analyzer_dir):
            return analyzer_dir
        sorting_dir = os.path.join(output_folder, f"Sorting_pipeline_{sorter_name}")
        if os.path.isdir(sorting_dir):
            return sorting_dir
        return output_folder

    def _prompt_and_launch_spikeinterface_gui(self):
        """Propose le dossier du dernier pipeline ou la sélection d'un dossier, puis lance sigui."""
        pipeline_path = self._last_success_results_path
        pipeline_ok = (
            self._has_successful_pipeline
            and pipeline_path
            and os.path.isdir(pipeline_path)
        )
        msg = QMessageBox(self)
        msg.setIcon(QMessageBox.Question)
        msg.setWindowTitle("SpikeInterface GUI")
        msg.setText("Quel dossier SortingAnalyzer ouvrir dans spikeinterface-gui ?")
        btn_pipeline = None
        if pipeline_ok:
            msg.setInformativeText(f"Dossier issu du dernier pipeline :\n{pipeline_path}")
            btn_pipeline = msg.addButton(
                "Utiliser le résultat du pipeline",
                QMessageBox.AcceptRole,
            )
        else:
            msg.setInformativeText(
                "Aucun résultat de pipeline disponible pour l'instant — "
                "choisissez un dossier sur le disque."
            )
        btn_browse = msg.addButton("Choisir un dossier…", QMessageBox.ActionRole)
        btn_cancel = msg.addButton("Annuler", QMessageBox.RejectRole)
        msg.setDefaultButton(btn_pipeline or btn_browse)
        msg.exec()
        clicked = msg.clickedButton()
        if clicked is None or clicked == btn_cancel:
            return
        if btn_pipeline is not None and clicked == btn_pipeline:
            self._launch_spikeinterface_gui(pipeline_path)
            return
        if clicked == btn_browse:
            start_dir = pipeline_path if pipeline_ok else self.folder_edit.text().strip() or ""
            folder = QFileDialog.getExistingDirectory(
                self,
                "Dossier SortingAnalyzer",
                start_dir,
            )
            if folder:
                self._launch_spikeinterface_gui(folder)

    def _launch_spikeinterface_gui(self, results_path=None):
        """Launch spikeinterface-gui in a separate process."""
        target_path = results_path or self._last_success_results_path
        if not target_path:
            self._show_info(
                "SpikeInterface GUI",
                "Aucun dossier à ouvrir.",
            )
            return
        scripts_dir = os.path.dirname(sys.executable)
        sigui_exe = os.path.join(
            scripts_dir, "sigui.exe" if sys.platform == "win32" else "sigui"
        )
        # pip installs the CLI as `sigui` only; there is no `python -m spikeinterface_gui`.
        py_sigui = (
            "from spikeinterface_gui.main import run_mainwindow_cli; "
            "import sys; "
            f"sys.argv = ['sigui', {repr(target_path)}]; "
            "run_mainwindow_cli()"
        )
        launch_attempts = []
        if os.path.isfile(sigui_exe):
            launch_attempts.append([sigui_exe, target_path])
        launch_attempts.append(["sigui", target_path])
        launch_attempts.append([sys.executable, "-c", py_sigui])
        errors = []
        for command in launch_attempts:
            try:
                subprocess.Popen(command)
                self._log(f"Started: {' '.join(command)}")
                return
            except FileNotFoundError as exc:
                errors.append(str(exc))
            except Exception as exc:
                errors.append(str(exc))
        self._show_error(
            "Unable to launch SpikeInterface GUI",
            "Could not start the GUI (console command: sigui).\n"
            "Install it in the current environment with:\n"
            "pip install spikeinterface-gui\n\n"
            f"Details:\n{errors[-1] if errors else 'Unknown launch error.'}",
        )

    def _set_run_button_state(self, enabled):
        """Enable Run when idle, enable Stop when pipeline is running."""
        self._run_button.setEnabled(enabled)
        self._stop_button.setEnabled(not enabled)

    def _set_form_enabled(self, enabled):
        """Enable/disable all form fields. Disabled when pipeline is running."""
        for w in self._form_widgets:
            w.setEnabled(enabled)
        self._launch_sigui_btn.setEnabled(enabled)
        if enabled:
            self._toggle_trigger_fields_state()  # Restore trigger fields state

    def _reset_pipeline_state(self):
        """Réactive le formulaire après arrêt ou fin du pipeline."""
        self._set_run_button_state(True)
        self._set_form_enabled(True)
        self._set_sorter_progress(False)

    def _request_stop(self):
        """Stop the pipeline immediately by terminating the subprocess."""
        if self._pipeline_process and self._pipeline_process.is_alive():
            self._log("Stopping pipeline immediately...")
            self._pipeline_process.terminate()
            self._pipeline_process.join(timeout=2.0)
            if self._pipeline_process.is_alive():
                self._pipeline_process.kill()
                self._pipeline_process.join(timeout=1.0)
            self._pipeline_process = None
            self._reset_pipeline_state()
            self._log("Pipeline stopped.")

    def _set_sorter_progress(self, running):
        self.progress_signal.emit(running)

    def _progress_impl(self, visible):
        self._progressbar.setVisible(visible)

    def _open_output_folder(self, folder_path):
        if os.path.isdir(folder_path):
            os.startfile(folder_path)
            time.sleep(0.25)
            user32 = ctypes.windll.user32
            hwnd = user32.FindWindowW("CabinetWClass", None)
            if hwnd:
                user32.ShowWindow(hwnd, 9)
                user32.SetForegroundWindow(hwnd)
        else:
            raise ValueError(f"Output folder not found: {folder_path}")

    def _log(self, message):
        """Append message to log panel (thread-safe via signal)."""
        self.log_signal.emit(message)

    def _log_impl(self, message):
        self.logs.append(message)
        scrollbar = self.logs.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _show_info(self, title, message):
        QMessageBox.information(self, title, message)

    def _show_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def _load_config_from_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Select settings file", "", JSON_FILTER)
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                state = json.load(f)
            state.pop("_description", None)
            self._apply_form_state(state)
            self._save_last_session()
            self._toggle_trigger_fields_state()
            QMessageBox.information(self, "Config loaded", f"Parameters loaded from:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Load failed", str(exc))

    def _on_pipeline_done(self, success: bool, payload):
        """Slot appelé sur le thread principal via pipeline_done_signal."""
        if success:
            self._on_pipeline_success(payload)
        else:
            if self._probe_temp_path:
                try:
                    os.unlink(self._probe_temp_path)
                except Exception:
                    pass
                self._probe_temp_path = None
            self._reset_pipeline_state()
            if payload == "file_in_use":
                err_msg = "Le fichier PDF est déjà ouvert.\n\nVeuillez fermer le fichier (par exemple dans un lecteur PDF) puis cliquer sur Run pour réessayer."
            else:
                err_msg = payload
                self._log(f"ERROR: {payload}")
            self._show_error("Erreur de génération PDF", err_msg)

    def _on_pipeline_success(self, folder_path):
        self._has_successful_pipeline = True
        self._last_success_results_path = self._get_spikeinterface_results_path(folder_path)
        self._reset_pipeline_state()
        QApplication.processEvents()
        self._log("Pipeline completed successfully.")
        self._log("Opening output folder...")
        try:
            self._open_output_folder(folder_path)
        except Exception:
            pass

    def _is_pdf_file_in_use(self, folder_path, sorter_name):
        """Check if the PDF output file exists and is open by another process."""
        pdf_path = os.path.join(folder_path, f"Summary_figures_sorting_{sorter_name}.pdf")
        if not os.path.isfile(pdf_path):
            return False
        try:
            with open(pdf_path, "ab") as _:
                pass
            return False
        except (PermissionError, OSError) as e:
            return is_file_in_use_error(e)

    def _run_pipeline_async(self):
        """Launch the pipeline in a subprocess. Logs are relayed via a queue."""
        self._save_last_session()
        self._set_run_button_state(False)
        params = self._collect_pipeline_params()
        if params is None:
            self._set_run_button_state(True)
            return
        # Vérifier si le PDF est déjà ouvert AVANT de lancer le sorting
        if self._is_pdf_file_in_use(params["output_folder"], params["sorter_name"]):
            self._set_run_button_state(True)
            QMessageBox.warning(
                self,
                "Fichier PDF ouvert",
                "Le fichier PDF de sortie est déjà ouvert par une autre application.\n\n"
                "Veuillez fermer le fichier (par exemple dans un lecteur PDF) avant de lancer le pipeline.",
            )
            return
        if self._last_probe_from_mea_editor:
            self._log("Probe used: current version from MEA Editor (including modifications).")
        self._set_form_enabled(False)  # Lock form while pipeline runs
        # Queue for inter-process communication (child -> parent)
        self._log_queue = multiprocessing.Queue()
        self._pipeline_process = multiprocessing.Process(
            target=run_pipeline_in_process,
            args=(params, self._log_queue),
            daemon=True,
        )
        self._pipeline_process.start()
        # Thread to read queue and emit Qt signals (GUI updates must be on main thread)
        self._queue_reader_thread = threading.Thread(target=self._queue_reader_loop, daemon=True)
        self._queue_reader_thread.start()

    def _collect_pipeline_params(self):
        """
        Collect and validate all params from GUI for the pipeline process.
        Returns a dict of params, or None if validation fails.
        """
        try:
            folder_path = self.folder_edit.text().strip()
            use_trigger = self.use_trigger_cb.isChecked()
            sorter_name = self.sorter_combo.currentText().strip()
            protocol_params = self._protocol_params
            prep = protocol_params.get("preprocessing", {})
            filter_type, min_freq, max_freq = get_preprocessing_filter_freqs(prep)
            if filter_type not in ("bandpass", "highpass", "lowpass"):
                filter_type = "bandpass"
            trigger_threshold = None
            trigger_edge = None
            trigger_min_interval = None
            trigger_channel_index = None
            if use_trigger:
                trigger_threshold = float(self.trigger_threshold_edit.text().strip())
                trigger_edge = self._polarity_to_edge(self.polarity_combo.currentText())
                trigger_min_interval = float(self.trigger_interval_edit.text().strip())
                trigger_channel_index = int(self.trigger_channel_edit.text().strip())

            # Validation
            if not folder_path or not os.path.isdir(folder_path):
                raise ValueError("folder_path is missing or does not exist.")
            # Dossier de sortie : date_heure_nom_sorter (tous les outputs y vont)
            output_folder_name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + "_" + sorter_name
            output_folder = os.path.join(folder_path, output_folder_name)
            os.makedirs(output_folder, exist_ok=True)
            my_probe_path = self._get_probe_path_for_pipeline(output_folder)
            if not my_probe_path or not os.path.isfile(my_probe_path):
                raise ValueError("Probe file is missing or does not exist. Load a probe via MEA Editor first.")
            if filter_type == "bandpass":
                if min_freq <= 0 or max_freq <= 0:
                    raise ValueError("protocol min_freq and max_freq must be > 0.")
                if min_freq >= max_freq:
                    raise ValueError("protocol min_freq must be < max_freq.")
            elif filter_type == "highpass":
                if min_freq <= 0:
                    raise ValueError("protocol highpass cutoff must be > 0.")
            elif filter_type == "lowpass":
                if max_freq <= 0:
                    raise ValueError("protocol lowpass cutoff must be > 0.")
            if use_trigger:
                if trigger_edge not in (-1, 1):
                    raise ValueError("trigger polarity must be 'Rising Edge' or 'Falling Edge'.")
                if trigger_channel_index < 0:
                    raise ValueError("trigger_channel_index must be >= 0.")

            # Build params dict for the subprocess (must be picklable)
            return {
                "folder_path": folder_path,
                "output_folder": output_folder,
                "use_trigger": use_trigger,
                "sorter_name": sorter_name,
                "my_probe_path": my_probe_path,
                "protocol_params": protocol_params,
                "min_freq": min_freq,
                "max_freq": max_freq,
                "trigger_threshold": trigger_threshold,
                "trigger_edge": trigger_edge,
                "trigger_min_interval": trigger_min_interval,
                "trigger_channel_index": trigger_channel_index,
                "trigger_type": "led" if self.rb_led.isChecked() else "electric",
            }
        except Exception as exc:
            self._log(f"Validation error: {exc}")
            return None

    def _queue_reader_loop(self):
        """
        Read messages from the pipeline process queue and update the GUI.
        Message types: ("log", msg), ("progress", bool), ("done", status, payload).
        Runs in a daemon thread; emits Qt signals for thread-safe GUI updates.
        """
        while True:
            try:
                item = self._log_queue.get(timeout=0.2)
            except Empty:
                # Process died without sending "done" (e.g. killed by user)
                if self._pipeline_process and not self._pipeline_process.is_alive():
                    break
                continue
            if item is None:
                break
            if isinstance(item, tuple):
                kind = item[0]
                if kind == "log":
                    self.log_signal.emit(item[1])
                elif kind == "progress":
                    self.progress_signal.emit(item[1])
                elif kind == "done":
                    _, status, payload = item
                    self._set_sorter_progress(False)
                    self.pipeline_done_signal.emit(status == "success", payload)
                    break
            else:
                self.log_signal.emit(str(item))
        self._pipeline_process = None


def run_app():
    """Create and run the Qt application."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    window = PipelineGUI()
    window.show()
    app.exec()


if __name__ == "__main__":
    run_app()
