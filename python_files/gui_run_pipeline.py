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
import ctypes
import threading
import traceback
import multiprocessing
from queue import Empty

# Constantes réutilisables
JSON_FILTER = "JSON files (*.json);;All files (*.*)"

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
    QScrollArea,
)
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction

from trigger_class import Trigger
from timestamps_class import TimestampsParameters
from sorter_class import Sorter
from protocol_class import default_protocol_params
from intan_class import IntanFile
from probe_class import Probe
from pipeline_class import Pipeline
from pdf_generator_class import PDFGenerator


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
        self._stop_requested = False
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
        left_layout.addWidget(self.folder_edit, r, 1)
        self._folder_btn = QPushButton("Browse")
        self._folder_btn.clicked.connect(lambda: self._browse_path("folder", self.folder_edit))
        left_layout.addWidget(self._folder_btn, r, 2)
        r += 1

        left_layout.addWidget(QLabel("Probe file path (.json)"), r, 0)
        self.probe_edit = QLineEdit()
        self.probe_edit.setText("C:/Spikesorting_utilities/MEA_RdLGN64.json")
        left_layout.addWidget(self.probe_edit, r, 1)
        self._probe_btn = QPushButton("Browse")
        self._probe_btn.clicked.connect(lambda: self._browse_path("file", self.probe_edit, "*.json"))
        left_layout.addWidget(self._probe_btn, r, 2)
        r += 1

        left_layout.addWidget(QLabel("Sorter name"), r, 0)
        self.sorter_edit = QLineEdit()
        self.sorter_edit.setText("tridesclous2")
        left_layout.addWidget(self.sorter_edit, r, 1)
        r += 1

        content.addWidget(left_widget)

        # Right column: Trigger section
        trigger_group = QGroupBox("Trigger")
        trigger_layout = QGridLayout(trigger_group)
        trigger_layout.setColumnStretch(1, 1)

        t = 0
        self.use_trigger_cb = QCheckBox("Use trigger detection")
        self.use_trigger_cb.setChecked(True)
        self.use_trigger_cb.toggled.connect(self._toggle_trigger_fields_state)
        trigger_layout.addWidget(self.use_trigger_cb, t, 0, 1, 2)
        t += 1

        trigger_layout.addWidget(QLabel("Trigger type:"), t, 0)
        trigger_type_widget = QWidget()
        trigger_type_layout = QHBoxLayout(trigger_type_widget)
        trigger_type_layout.setContentsMargins(0, 0, 0, 0)
        self.trigger_type_group = QButtonGroup()
        self.rb_led = QRadioButton("LED")
        self.rb_electric = QRadioButton("Electric")
        self.rb_electric.setChecked(True)
        self.trigger_type_group.addButton(self.rb_led)
        self.trigger_type_group.addButton(self.rb_electric)
        self.rb_led.toggled.connect(self._on_trigger_type_change)
        self.rb_electric.toggled.connect(self._on_trigger_type_change)
        trigger_type_layout.addWidget(self.rb_led)
        trigger_type_layout.addWidget(self.rb_electric)
        trigger_layout.addWidget(trigger_type_widget, t, 1)
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

        content.addWidget(trigger_group)
        main_layout.addLayout(content)

        # Protocol section: un champ par paramètre, chaque modification met à jour le dict
        protocol_scroll = QScrollArea()
        protocol_scroll.setWidgetResizable(True)
        protocol_scroll.setMaximumHeight(320)
        protocol_content = QWidget()
        protocol_main = QVBoxLayout(protocol_content)

        protocol_group = QGroupBox("Protocol")
        protocol_layout = QGridLayout(protocol_group)
        protocol_layout.setColumnStretch(1, 1)

        default = default_protocol_params(400, 5000)
        self._protocol_params = copy.deepcopy(default)

        p = 0
        protocol_layout.addWidget(QLabel("Bandpass freq min (Hz)"), p, 0)
        self.protocol_freq_min = QDoubleSpinBox()
        self.protocol_freq_min.setRange(1, 20000)
        self.protocol_freq_min.setValue(400)
        self.protocol_freq_min.setDecimals(0)
        self.protocol_freq_min.setMaximumWidth(120)
        self.protocol_freq_min.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_freq_min, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Bandpass freq max (Hz)"), p, 0)
        self.protocol_freq_max = QDoubleSpinBox()
        self.protocol_freq_max.setRange(1, 20000)
        self.protocol_freq_max.setValue(5000)
        self.protocol_freq_max.setDecimals(0)
        self.protocol_freq_max.setMaximumWidth(120)
        self.protocol_freq_max.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_freq_max, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Unit locations method"), p, 0)
        self.protocol_unit_locations_method = QComboBox()
        self.protocol_unit_locations_method.addItems(["center_of_mass", "monopolar_triangulation"])
        self.protocol_unit_locations_method.setMaximumWidth(180)
        self.protocol_unit_locations_method.currentTextChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_unit_locations_method, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Random spikes max_spikes_per_unit"), p, 0)
        self.protocol_random_spikes_max = QSpinBox()
        self.protocol_random_spikes_max.setRange(1, 10000)
        self.protocol_random_spikes_max.setValue(1000)
        self.protocol_random_spikes_max.setMaximumWidth(120)
        self.protocol_random_spikes_max.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_random_spikes_max, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Waveforms ms_before"), p, 0)
        self.protocol_waveforms_ms_before = QDoubleSpinBox()
        self.protocol_waveforms_ms_before.setRange(0.1, 10)
        self.protocol_waveforms_ms_before.setValue(1.0)
        self.protocol_waveforms_ms_before.setSingleStep(0.1)
        self.protocol_waveforms_ms_before.setMaximumWidth(120)
        self.protocol_waveforms_ms_before.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_waveforms_ms_before, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Waveforms ms_after"), p, 0)
        self.protocol_waveforms_ms_after = QDoubleSpinBox()
        self.protocol_waveforms_ms_after.setRange(0.1, 10)
        self.protocol_waveforms_ms_after.setValue(2.0)
        self.protocol_waveforms_ms_after.setSingleStep(0.1)
        self.protocol_waveforms_ms_after.setMaximumWidth(120)
        self.protocol_waveforms_ms_after.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_waveforms_ms_after, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Correlograms window_ms"), p, 0)
        self.protocol_correlograms_window = QDoubleSpinBox()
        self.protocol_correlograms_window.setRange(1, 500)
        self.protocol_correlograms_window.setValue(50.0)
        self.protocol_correlograms_window.setMaximumWidth(120)
        self.protocol_correlograms_window.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_correlograms_window, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Correlograms bin_ms"), p, 0)
        self.protocol_correlograms_bin = QDoubleSpinBox()
        self.protocol_correlograms_bin.setRange(0.1, 10)
        self.protocol_correlograms_bin.setValue(1.0)
        self.protocol_correlograms_bin.setSingleStep(0.1)
        self.protocol_correlograms_bin.setMaximumWidth(120)
        self.protocol_correlograms_bin.valueChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_correlograms_bin, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Spike amplitudes peak_sign"), p, 0)
        self.protocol_spike_amplitudes_peak = QComboBox()
        self.protocol_spike_amplitudes_peak.addItems(["neg", "pos"])
        self.protocol_spike_amplitudes_peak.setMaximumWidth(120)
        self.protocol_spike_amplitudes_peak.currentTextChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_spike_amplitudes_peak, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Template similarity method"), p, 0)
        self.protocol_template_similarity_method = QComboBox()
        self.protocol_template_similarity_method.addItems(["cosine_similarity"])
        self.protocol_template_similarity_method.setMaximumWidth(180)
        self.protocol_template_similarity_method.currentTextChanged.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_template_similarity_method, p, 1)
        p += 1

        protocol_layout.addWidget(QLabel("Template metrics multi_channel"), p, 0)
        self.protocol_template_metrics_multi = QCheckBox("")
        self.protocol_template_metrics_multi.setChecked(False)
        self.protocol_template_metrics_multi.toggled.connect(self._update_protocol_from_form)
        protocol_layout.addWidget(self.protocol_template_metrics_multi, p, 1)
        p += 1

        protocol_btns = QHBoxLayout()
        self._protocol_load_btn = QPushButton("Load protocol")
        self._protocol_load_btn.clicked.connect(self._load_protocol_from_file)
        self._protocol_reset_btn = QPushButton("Reset to defaults")
        self._protocol_reset_btn.clicked.connect(self._reset_protocol_defaults)
        protocol_btns.addWidget(self._protocol_load_btn)
        protocol_btns.addWidget(self._protocol_reset_btn)
        protocol_btns.addStretch()
        protocol_layout.addLayout(protocol_btns, p, 0, 1, 2)
        protocol_main.addWidget(protocol_group)
        protocol_scroll.setWidget(protocol_content)
        main_layout.addWidget(protocol_scroll)
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
            self.probe_edit, self._probe_btn, self.sorter_edit,
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
            "sorter_name": self.sorter_edit.text(),
            "my_probe_path": self.probe_edit.text(),
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
        self.sorter_edit.setText(state.get("sorter_name", self.sorter_edit.text()))
        self.probe_edit.setText(state.get("my_probe_path", self.probe_edit.text()))
        protocol_params = state.get("protocol_params")
        if isinstance(protocol_params, dict):
            self._protocol_params = copy.deepcopy(protocol_params)
            self._apply_protocol_to_form(protocol_params)
        elif state.get("protocol_freq_min") is not None or state.get("protocol_freq_max") is not None:
            self.protocol_freq_min.setValue(float(state.get("protocol_freq_min", 400)))
            self.protocol_freq_max.setValue(float(state.get("protocol_freq_max", 5000)))
            self._update_protocol_from_form()

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
        self._save_last_session()
        event.accept()

    def _browse_path(self, mode, target_edit, filter_ext=None):
        if mode == "folder":
            selected = QFileDialog.getExistingDirectory(self, "Select folder")
        else:
            filter_str = JSON_FILTER if filter_ext else "All files (*.*)"
            selected, _ = QFileDialog.getOpenFileName(self, "Select file", "", filter_str)
        if selected:
            target_edit.setText(selected)
            self._save_last_session()

    def _get_current_protocol_params(self):
        """Return the protocol dict (updated by _update_protocol_from_form on each field change)."""
        return self._protocol_params

    def _update_protocol_from_form(self):
        """Met à jour le dictionnaire protocol à chaque modification d'un champ."""
        p = self._protocol_params
        p.setdefault("preprocessing", {}).setdefault("bandpass_filter", {})["freq_min"] = self.protocol_freq_min.value()
        p.setdefault("preprocessing", {}).setdefault("bandpass_filter", {})["freq_max"] = self.protocol_freq_max.value()
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
        bp = params.get("preprocessing", {}).get("bandpass_filter", {})
        self.protocol_freq_min.setValue(float(bp.get("freq_min", 400)))
        self.protocol_freq_max.setValue(float(bp.get("freq_max", 5000)))
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

    def _set_run_button_state(self, enabled):
        """Enable Run when idle, enable Stop when pipeline is running."""
        self._run_button.setEnabled(enabled)
        self._stop_button.setEnabled(not enabled)

    def _set_form_enabled(self, enabled):
        """Enable/disable all form fields. Disabled when pipeline is running."""
        for w in self._form_widgets:
            w.setEnabled(enabled)
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

    def _save_protocol_to_output_folder(self, folder_path):
        protocol_path = os.path.join(folder_path, "protocol.json")
        try:
            with open(protocol_path, "w", encoding="utf-8") as f:
                json.dump(self._get_current_protocol_params(), f, indent=2, ensure_ascii=False)
            self._log(f"Protocol saved to {protocol_path}")
        except Exception as exc:
            self._log(f"Warning: could not save protocol.json: {exc}")

    def _on_pipeline_done(self, success: bool, payload):
        """Slot appelé sur le thread principal via pipeline_done_signal."""
        if success:
            self._on_pipeline_success(payload)
        else:
            self._reset_pipeline_state()
            if payload == "file_in_use":
                err_msg = "Le fichier PDF est déjà ouvert.\n\nVeuillez fermer le fichier (par exemple dans un lecteur PDF) puis cliquer sur Run pour réessayer."
            else:
                err_msg = payload
                self._log(f"ERROR: {payload}")
            self._show_error("Erreur de génération PDF", err_msg)

    def _on_pipeline_success(self, folder_path):
        self._reset_pipeline_state()
        QApplication.processEvents()
        self._save_protocol_to_output_folder(folder_path)
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
            return _is_file_in_use_error(e)

    def _run_pipeline_async(self):
        """Launch the pipeline in a subprocess. Logs are relayed via a queue."""
        self._save_last_session()
        self._set_run_button_state(False)
        params = self._collect_pipeline_params()
        if params is None:
            self._set_run_button_state(True)
            return
        # Vérifier si le PDF est déjà ouvert AVANT de lancer le sorting
        if self._is_pdf_file_in_use(params["folder_path"], params["sorter_name"]):
            self._set_run_button_state(True)
            QMessageBox.warning(
                self,
                "Fichier PDF ouvert",
                "Le fichier PDF de sortie est déjà ouvert par une autre application.\n\n"
                "Veuillez fermer le fichier (par exemple dans un lecteur PDF) avant de lancer le pipeline.",
            )
            return
        self._set_form_enabled(False)  # Lock form while pipeline runs
        # Queue for inter-process communication (child -> parent)
        self._log_queue = multiprocessing.Queue()
        self._pipeline_process = multiprocessing.Process(
            target=_run_pipeline_in_process,
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
            sorter_name = self.sorter_edit.text().strip()
            my_probe_path = self.probe_edit.text().strip()
            protocol_params = self._get_current_protocol_params()
            bandpass = protocol_params.get("preprocessing", {}).get("bandpass_filter", {})
            min_freq = float(bandpass.get("freq_min", 400))
            max_freq = float(bandpass.get("freq_max", 5000))
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
            if not my_probe_path or not os.path.isfile(my_probe_path):
                raise ValueError("my_probe_df path is missing or does not exist.")
            if min_freq <= 0 or max_freq <= 0:
                raise ValueError("protocol min_freq and max_freq must be > 0.")
            if min_freq >= max_freq:
                raise ValueError("protocol min_freq must be < max_freq.")
            if use_trigger:
                if trigger_edge not in (-1, 1):
                    raise ValueError("trigger polarity must be 'Rising Edge' or 'Falling Edge'.")
                if trigger_channel_index < 0:
                    raise ValueError("trigger_channel_index must be >= 0.")

            # Build params dict for the subprocess (must be picklable)
            return {
                "folder_path": folder_path,
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

def _is_file_in_use_error(exc):
    """Détecte si une exception indique qu'un fichier est ouvert par un autre processus."""
    err_msg = str(exc).lower()
    return (
        "being used" in err_msg
        or "another process" in err_msg
        or "permission denied" in err_msg
        or "accès refusé" in err_msg
        or (hasattr(exc, "winerror") and getattr(exc, "winerror", None) == 32)
    )


def _run_pipeline_in_process(params, log_queue):
    """
    Entry point for the pipeline subprocess.
    Receives params dict and a multiprocessing.Queue for log/progress/done messages.
    Message format: ("log", str), ("progress", bool), ("done", "success"|"error", payload).
    """
    def _log(msg):
        log_queue.put(("log", msg))

    def _progress(visible):
        log_queue.put(("progress", visible))

    try:
        folder_path = params["folder_path"]
        use_trigger = params["use_trigger"]
        sorter_name = params["sorter_name"]
        my_probe_path = params["my_probe_path"]
        protocol_params = params["protocol_params"]
        min_freq = params["min_freq"]
        max_freq = params["max_freq"]
        trigger_type = params["trigger_type"]
        my_protocol_path = os.path.join(folder_path, "protocol.json")

        # Log startup info
        _log("Starting pipeline...")
        _log(f"folder_path: {folder_path}")
        _log(f"sorter_name: {sorter_name}")
        _log(f"use_trigger: {use_trigger}")
        if use_trigger:
            _log(
                f"trigger: type={trigger_type}, "
                f"threshold={params['trigger_threshold']}, "
                f"polarity={'Rising' if params['trigger_edge'] == 1 else 'Falling'} Edge, "
                f"min_interval={params['trigger_min_interval']}"
            )
            _log(f"trigger_channel_index: {params['trigger_channel_index']}")
        _log(f"protocol_path (auto): {my_protocol_path}")
        _log(f"probe_path: {my_probe_path}")
        _log(f"bandpass: {min_freq} -> {max_freq} Hz")

        # Trigger and timestamps
        timestamps_parameters = None
        if use_trigger:
            trigger = Trigger(
                params["trigger_threshold"],
                params["trigger_edge"],
                params["trigger_min_interval"],
            )
            timestamps_parameters = TimestampsParameters(
                trigger=trigger,
                trigger_channel_index=params["trigger_channel_index"],
                trigger_type=trigger_type,
            )
        # Load Intan data
        sorter = Sorter(sorter_name)
        _log("Loading Intan files...")
        rhs_files = IntanFile(folder_path)
        _log(f"Channel IDs: {rhs_files.channel_ids}")
        _log(f"Sampling frequency: {rhs_files.frequency}")
        _log(f"Number of channels: {rhs_files.number_of_channels}")
        _log(f"Number of segments: {rhs_files.number_of_segments}")
        if use_trigger:
            _log("Computing trigger timestamps...")
            rhs_files.generate_trigger_timestamps(timestamps_parameters)
        else:
            _log("Trigger disabled: skipping trigger timestamp extraction.")

        protocol_params = copy.deepcopy(params["protocol_params"])
        protocol_params["_file_path"] = my_protocol_path
        my_probe_df = Probe(my_probe_path)
        _log("Associating probe...")
        rhs_files.associate_probe(my_probe_df)

        _log("Running sorter + analyzer (this can take time)...")
        _progress(True)
        pipeline = Pipeline(sorter, folder_path, protocol_params, rhs_files)

        _log("Generating PDF report...")
        try:
            PDFGenerator(folder_path, pipeline)
        except (PermissionError, OSError) as pdf_exc:
            if _is_file_in_use_error(pdf_exc):
                _progress(False)
                _log("ERROR: Le fichier PDF est déjà ouvert.")
                log_queue.put(("done", "error", "file_in_use"))
                return
            raise
        pdf_path = os.path.join(folder_path, f"Summary_figures_sorting_{sorter_name}.pdf")
        _log(f"PDF generated: {pdf_path}")
        _progress(False)

        log_queue.put(("done", "success", folder_path))
    except Exception as exc:
        _progress(False)
        _log(f"ERROR: {exc}")
        _log(traceback.format_exc())
        log_queue.put(("done", "error", str(exc)))  # Notify parent of failure


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
