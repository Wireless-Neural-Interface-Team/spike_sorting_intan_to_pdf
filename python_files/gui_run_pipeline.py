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
import shutil
from datetime import datetime
import ctypes
import threading
import multiprocessing
import subprocess
from queue import Empty
from collections import defaultdict

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
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    QScrollArea,
    QFrame,
)
from PySide6.QtCore import Qt, QTimer, Signal, QThread
from PySide6.QtGui import QAction

from protocol_class import default_protocol_params
from gui_components import (
    ChannelsLoaderWorker,
    EmbeddedMEAEditor,
    MEA_EDITOR_AVAILABLE,
    save_electrodes_to_file,
)
from gui_pipeline_runner import run_pipeline_in_process, is_file_in_use_error

try:
    from spikeinterface.sorters import (
        available_sorters,
        get_default_sorter_params,
        get_sorter_params_description,
    )
    SORTERS_AVAILABLE = True
except ImportError:
    SORTERS_AVAILABLE = False
    available_sorters = lambda: ["tridesclous2"]
    get_default_sorter_params = lambda n: {}
    get_sorter_params_description = lambda n: {}


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
        self._current_sorter_name = None
        self._preprocessing_steps_order = [
            "unsigned_to_signed",
            "bandpass_filter",
            "highpass_filter",
            "notch_filter",
            "gaussian_filter",
            "common_reference",
            "detect_bad_channels",
            "phase_shift",
            "rectify",
        ]
        self._filter_steps = ["bandpass_filter", "highpass_filter", "notch_filter", "gaussian_filter"]
        self._preprocessing_step_defaults = {
            "unsigned_to_signed": {},
            "bandpass_filter": {"freq_min": 400.0, "freq_max": 5000.0},
            "highpass_filter": {"freq_min": 300.0},
            "notch_filter": {"freq": 50.0, "q": 30.0},
            "gaussian_filter": {"freq_min": 300.0, "freq_max": 6000.0},
            "common_reference": {"reference": "global", "operator": "median"},
            "detect_bad_channels": {"method": "std", "std_mad_threshold": 5.0},
            "phase_shift": {},
            "rectify": {},
        }
        self._preprocessing_step_param_options = {
            "common_reference": {
                "reference": ["global", "local"],
                "operator": ["median", "average"],
            },
            "detect_bad_channels": {
                "method": ["std", "mad", "coherence+psd"],
            },
        }
        self._preprocessing_step_enabled_widgets = {}
        self._preproc_step_params_widgets = {}
        self._preproc_step_param_input_widgets = {}
        self._channels_load_thread = None
        self._channels_load_worker = None
        self._channels_debounce_timer = None
        self._channels_id_cache = {}
        self._channels_loading_key = None
        self._save_debounce_timer = None
        self._mea_editor_window = None
        self._probe_temp_path = None
        self._last_probe_from_mea_editor = False
        self._mea_editor_sync_timer = None
        self._probe_path = ""
        # Pipeline runs in a subprocess for immediate stop capability
        self._pipeline_process = None  # multiprocessing.Process instance
        self._log_queue = None  # multiprocessing.Queue for log messages from child
        self._queue_reader_thread = None  # Thread that reads queue and updates GUI
        self.log_signal.connect(self._log_impl)
        self.progress_signal.connect(self._progress_impl)
        self.pipeline_done_signal.connect(self._on_pipeline_done)
        self._build_ui()
        self._load_last_session()

    def _make_info_badge(self, tooltip_text):
        """Small blue 'i' badge with tooltip."""
        badge = QLabel("i")
        badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badge.setFixedSize(12, 12)
        badge.setToolTip(tooltip_text or "")
        badge.setStyleSheet(
            "QLabel {"
            "background-color: #2b7de9;"
            "color: white;"
            "border-radius: 6px;"
            "font-size: 8px;"
            "font-weight: bold;"
            "}"
        )
        return badge

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

        # Main content: left panel + right column
        content = QHBoxLayout()
        content.setSpacing(12)

        # Left column: folder, probe, sorter, protocol
        left_widget = QWidget()
        left_layout = QGridLayout(left_widget)
        left_layout.setColumnStretch(1, 1)
        left_layout.setColumnMinimumWidth(3, 16)

        r = 0
        left_layout.addWidget(QLabel("Intan files folder path"), r, 0)
        self.folder_edit = QLineEdit()
        self.folder_edit.setMinimumWidth(400)
        self.folder_edit.editingFinished.connect(self._schedule_refresh_channels)
        left_layout.addWidget(self.folder_edit, r, 1)
        self._folder_btn = QPushButton("Browse")
        self._folder_btn.clicked.connect(self._on_folder_browse)
        left_layout.addWidget(self._folder_btn, r, 2)
        left_layout.addWidget(self._make_info_badge("Folder containing Intan files to process."), r, 3)
        r += 1

        left_layout.addWidget(QLabel("Channels in file"), r, 0)
        self.channels_display = QTableWidget()
        self.channels_display.setMaximumWidth(280)
        self.channels_display.setMinimumHeight(280)
        self.channels_display.setMaximumHeight(400)
        self.channels_display.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.channels_display.verticalHeader().setVisible(False)
        left_layout.addWidget(self.channels_display, r, 1, 1, 2)
        left_layout.addWidget(self._make_info_badge("Detected channel IDs from selected folder."), r, 3)
        r += 1

        left_layout.addWidget(QLabel("Probe"), r, 0)
        self.probe_name_display = QLineEdit()
        self.probe_name_display.setReadOnly(True)
        self.probe_name_display.setPlaceholderText("—")
        self.probe_name_display.setToolTip("")
        left_layout.addWidget(self.probe_name_display, r, 1)
        self._probe_edit_btn = QPushButton("Load / Edit probe")
        self._probe_edit_btn.clicked.connect(self._open_mea_editor)
        self._probe_edit_btn.setEnabled(MEA_EDITOR_AVAILABLE)
        left_layout.addWidget(self._probe_edit_btn, r, 2)
        left_layout.addWidget(
            self._make_info_badge(
                "Load/Edit: open MEA Editor. Unsaved probe is used by the pipeline."
            ),
            r,
            3,
        )
        r += 1

        # Right column: Trigger section (independent from left table)
        trigger_group = QGroupBox("Trigger")
        trigger_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        trigger_layout = QGridLayout(trigger_group)
        trigger_layout.setColumnStretch(1, 1)
        trigger_layout.setColumnMinimumWidth(2, 16)

        t = 0
        self.use_trigger_cb = QCheckBox("Use trigger detection")
        self.use_trigger_cb.setChecked(True)
        self.use_trigger_cb.toggled.connect(self._toggle_trigger_fields_state)
        trigger_layout.addWidget(self.use_trigger_cb, t, 0, 1, 2)
        trigger_layout.addWidget(self._make_info_badge("Enable trigger extraction from ADC channel."), t, 2)
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
        trigger_layout.addWidget(self._make_info_badge("Select preset for trigger defaults."), t, 2)
        self._trigger_widgets.extend([self.rb_led, self.rb_electric])
        t += 1

        trigger_layout.addWidget(QLabel("Trigger threshold"), t, 0)
        self.trigger_threshold_edit = QLineEdit()
        self.trigger_threshold_edit.setText("37000")
        self.trigger_threshold_edit.setMaximumWidth(150)
        trigger_layout.addWidget(self.trigger_threshold_edit, t, 1)
        trigger_layout.addWidget(self._make_info_badge("Threshold used to detect trigger events."), t, 2)
        self._trigger_widgets.append(self.trigger_threshold_edit)
        t += 1

        trigger_layout.addWidget(QLabel("Trigger polarity"), t, 0)
        self.polarity_combo = QComboBox()
        self.polarity_combo.addItems(["Rising Edge", "Falling Edge"])
        self.polarity_combo.setCurrentText("Falling Edge")
        self.polarity_combo.setMaximumWidth(150)
        trigger_layout.addWidget(self.polarity_combo, t, 1)
        trigger_layout.addWidget(self._make_info_badge("Edge direction for trigger detection."), t, 2)
        self._trigger_widgets.append(self.polarity_combo)
        t += 1

        trigger_layout.addWidget(QLabel("Minimum elapsed time between triggers (s)"), t, 0)
        self.trigger_interval_edit = QLineEdit()
        self.trigger_interval_edit.setText("5.1")
        self.trigger_interval_edit.setMaximumWidth(150)
        trigger_layout.addWidget(self.trigger_interval_edit, t, 1)
        trigger_layout.addWidget(self._make_info_badge("Minimum interval between triggers (seconds)."), t, 2)
        self._trigger_widgets.append(self.trigger_interval_edit)
        t += 1

        trigger_layout.addWidget(QLabel("Trigger channel index"), t, 0)
        self.trigger_channel_edit = QLineEdit()
        self.trigger_channel_edit.setText("0")
        self.trigger_channel_edit.setMaximumWidth(150)
        trigger_layout.addWidget(self.trigger_channel_edit, t, 1)
        trigger_layout.addWidget(self._make_info_badge("ADC trigger channel index (0-based)."), t, 2)
        self._trigger_widgets.append(self.trigger_channel_edit)

        # Right column: Trigger first, then Sorter below
        right_column = QWidget()
        right_layout = QVBoxLayout(right_column)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.addWidget(trigger_group)

        sorter_group = QGroupBox("Sorter")
        sorter_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        sorter_group_layout = QGridLayout(sorter_group)
        sorter_group_layout.setColumnStretch(1, 1)
        sorter_group_layout.setColumnMinimumWidth(2, 16)
        sorter_group_layout.addWidget(QLabel("Sorter"), 0, 0)
        self.sorter_combo = QComboBox()
        self.sorter_combo.setMinimumWidth(180)
        self._populate_sorter_combo()
        self.sorter_combo.currentTextChanged.connect(self._on_sorter_changed)
        sorter_group_layout.addWidget(self.sorter_combo, 0, 1)
        sorter_group_layout.addWidget(self._make_info_badge("Choose spike sorting backend."), 0, 2)
        right_layout.addWidget(sorter_group)

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
        preprocessing_group.setStyleSheet(
            "QGroupBox { font-weight: bold; margin-top: 8px; } "
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        )
        preprocessing_main = QVBoxLayout(preprocessing_group)
        preprocessing_main.setSpacing(8)
        preprocessing_main.setContentsMargins(12, 16, 12, 12)

        # Filter block: compact row
        filter_frame = QFrame()
        filter_frame.setFrameShape(QFrame.Shape.StyledPanel)
        filter_frame.setStyleSheet("QFrame { background-color: palette(base); border-radius: 4px; }")
        filter_layout = QHBoxLayout(filter_frame)
        filter_layout.setContentsMargins(10, 8, 10, 8)
        filter_layout.setSpacing(12)
        filter_layout.addWidget(QLabel("Filter:"))
        self.preproc_filter_choice_combo = QComboBox()
        self.preproc_filter_choice_combo.addItems(
            ["None", "bandpass_filter", "highpass_filter", "notch_filter", "gaussian_filter"]
        )
        self.preproc_filter_choice_combo.currentTextChanged.connect(self._on_filter_choice_changed)
        self.preproc_filter_choice_combo.setMinimumWidth(140)
        filter_layout.addWidget(self.preproc_filter_choice_combo)
        self.preproc_filter_params_container = QWidget()
        self.preproc_filter_params_layout = QHBoxLayout(self.preproc_filter_params_container)
        self.preproc_filter_params_layout.setContentsMargins(0, 0, 0, 0)
        self.preproc_filter_params_layout.setSpacing(12)
        filter_layout.addWidget(self.preproc_filter_params_container, 1)
        filter_layout.addStretch()
        preprocessing_main.addWidget(filter_frame)

        # Steps block: compact list
        steps_frame = QFrame()
        steps_frame.setFrameShape(QFrame.Shape.StyledPanel)
        steps_frame.setStyleSheet("QFrame { background-color: palette(base); border-radius: 4px; }")
        self.preproc_steps_layout = QVBoxLayout(steps_frame)
        self.preproc_steps_layout.setContentsMargins(10, 8, 10, 8)
        self.preproc_steps_layout.setSpacing(6)
        for step_name in self._preprocessing_steps_order:
            if step_name in self._filter_steps:
                panel, param_widgets = self._create_preproc_step_panel(step_name)
                self._preproc_step_param_input_widgets[step_name] = param_widgets
                self._preproc_step_params_widgets[step_name] = panel
                self.preproc_filter_params_layout.addWidget(panel)
                continue
            panel, param_widgets = self._create_preproc_step_panel(step_name)
            self._preproc_step_param_input_widgets[step_name] = param_widgets
            self._preproc_step_params_widgets[step_name] = panel
            step_row = QHBoxLayout()
            step_row.setSpacing(8)
            cb = QCheckBox(step_name.replace("_", " "))
            cb.toggled.connect(lambda checked, s=step_name: self._on_preproc_step_toggled(s, checked))
            self._preprocessing_step_enabled_widgets[step_name] = cb
            step_row.addWidget(cb)
            step_row.addWidget(panel, 1)
            step_row.addStretch()
            self.preproc_steps_layout.addLayout(step_row)
        preprocessing_main.addWidget(steps_frame)

        protocol_main.addWidget(preprocessing_group)

        # --- Postprocessing group ---
        postprocessing_group = QGroupBox("Protocol postprocessing")
        postprocessing_group.setToolTip("")
        postprocessing_layout = QGridLayout(postprocessing_group)
        postprocessing_layout.setColumnStretch(1, 1)
        postprocessing_layout.setColumnMinimumWidth(2, 16)

        post = 0
        postprocessing_layout.addWidget(QLabel("Unit locations method"), post, 0)
        self.protocol_unit_locations_method = QComboBox()
        self.protocol_unit_locations_method.addItems(["center_of_mass", "monopolar_triangulation"])
        self.protocol_unit_locations_method.setMaximumWidth(180)
        self.protocol_unit_locations_method.currentTextChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_unit_locations_method, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Method to estimate unit locations."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Random spikes max_spikes_per_unit"), post, 0)
        self.protocol_random_spikes_max = QSpinBox()
        self.protocol_random_spikes_max.setRange(1, 10000)
        self.protocol_random_spikes_max.setValue(1000)
        self.protocol_random_spikes_max.setMaximumWidth(120)
        self.protocol_random_spikes_max.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_random_spikes_max, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Max random spikes per unit to sample."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Waveforms ms_before"), post, 0)
        self.protocol_waveforms_ms_before = QDoubleSpinBox()
        self.protocol_waveforms_ms_before.setRange(0.1, 10)
        self.protocol_waveforms_ms_before.setValue(1.0)
        self.protocol_waveforms_ms_before.setSingleStep(0.1)
        self.protocol_waveforms_ms_before.setMaximumWidth(120)
        self.protocol_waveforms_ms_before.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_waveforms_ms_before, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Waveform window before peak (ms)."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Waveforms ms_after"), post, 0)
        self.protocol_waveforms_ms_after = QDoubleSpinBox()
        self.protocol_waveforms_ms_after.setRange(0.1, 10)
        self.protocol_waveforms_ms_after.setValue(2.0)
        self.protocol_waveforms_ms_after.setSingleStep(0.1)
        self.protocol_waveforms_ms_after.setMaximumWidth(120)
        self.protocol_waveforms_ms_after.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_waveforms_ms_after, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Waveform window after peak (ms)."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Correlograms window_ms"), post, 0)
        self.protocol_correlograms_window = QDoubleSpinBox()
        self.protocol_correlograms_window.setRange(1, 500)
        self.protocol_correlograms_window.setValue(50.0)
        self.protocol_correlograms_window.setMaximumWidth(120)
        self.protocol_correlograms_window.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_correlograms_window, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Correlogram total window (ms)."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Correlograms bin_ms"), post, 0)
        self.protocol_correlograms_bin = QDoubleSpinBox()
        self.protocol_correlograms_bin.setRange(0.1, 10)
        self.protocol_correlograms_bin.setValue(1.0)
        self.protocol_correlograms_bin.setSingleStep(0.1)
        self.protocol_correlograms_bin.setMaximumWidth(120)
        self.protocol_correlograms_bin.valueChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_correlograms_bin, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Correlogram bin size (ms)."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Spike amplitudes peak_sign"), post, 0)
        self.protocol_spike_amplitudes_peak = QComboBox()
        self.protocol_spike_amplitudes_peak.addItems(["neg", "pos"])
        self.protocol_spike_amplitudes_peak.setMaximumWidth(120)
        self.protocol_spike_amplitudes_peak.currentTextChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_spike_amplitudes_peak, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Peak sign used for spike amplitudes."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Template similarity method"), post, 0)
        self.protocol_template_similarity_method = QComboBox()
        self.protocol_template_similarity_method.addItems(["cosine_similarity"])
        self.protocol_template_similarity_method.setMaximumWidth(180)
        self.protocol_template_similarity_method.currentTextChanged.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_template_similarity_method, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Template similarity method."), post, 2)
        post += 1

        postprocessing_layout.addWidget(QLabel("Template metrics multi_channel"), post, 0)
        self.protocol_template_metrics_multi = QCheckBox("")
        self.protocol_template_metrics_multi.setChecked(False)
        self.protocol_template_metrics_multi.toggled.connect(self._update_protocol_from_form)
        postprocessing_layout.addWidget(self.protocol_template_metrics_multi, post, 1)
        postprocessing_layout.addWidget(self._make_info_badge("Include multi-channel template metrics."), post, 2)

        protocol_main.addWidget(postprocessing_group)

        # Protocol buttons (Load / Save / Reset apply to full protocol)
        protocol_btns = QHBoxLayout()
        self._protocol_load_btn = QPushButton("Load protocol")
        self._protocol_load_btn.clicked.connect(self._load_protocol_from_file)
        self._protocol_save_btn = QPushButton("Save protocol")
        self._protocol_save_btn.clicked.connect(self._save_protocol_to_file)
        self._protocol_reset_btn = QPushButton("Reset to defaults")
        self._protocol_reset_btn.clicked.connect(self._reset_protocol_defaults)
        protocol_btns.addWidget(self._protocol_load_btn)
        protocol_btns.addWidget(self._protocol_save_btn)
        protocol_btns.addWidget(self._protocol_reset_btn)
        protocol_btns.addStretch()
        protocol_main.addLayout(protocol_btns)

        # Sorter parameters (dynamic, adapts to selected sorter)
        self.sorter_params_group = QGroupBox("Sorter parameters")
        self.sorter_params_group.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        sorter_params_outer = QVBoxLayout(self.sorter_params_group)
        self.sorter_params_scroll = QScrollArea()
        self.sorter_params_scroll.setWidgetResizable(True)
        self.sorter_params_scroll.setMaximumHeight(16777215)
        self.sorter_params_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.sorter_params_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sorter_params_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.sorter_params_container = QWidget()
        self.sorter_params_layout = QGridLayout(self.sorter_params_container)
        self.sorter_params_layout.setColumnStretch(1, 1)
        self.sorter_params_layout.setColumnMinimumWidth(1, 16)
        self.sorter_params_scroll.setWidget(self.sorter_params_container)
        sorter_params_outer.addWidget(self.sorter_params_scroll)
        self._sorter_param_widgets = {}
        self.sorter_params_reset_btn = QPushButton("Reset sorter params to defaults")
        self.sorter_params_reset_btn.clicked.connect(self._reset_sorter_params_to_defaults)
        sorter_params_outer.addWidget(self.sorter_params_reset_btn)
        right_layout.addWidget(self.sorter_params_group, 1)
        protocol_container_layout.addWidget(protocol_content)

        # Left panel: top area (inputs + protocol), bottom area (logs)
        left_top = QWidget()
        left_top_layout = QHBoxLayout(left_top)
        left_top_layout.setContentsMargins(0, 0, 0, 0)
        left_top_layout.setSpacing(12)
        left_top_layout.addWidget(left_widget, 1)
        left_top_layout.addWidget(protocol_container, 1)

        left_panel = QWidget()
        left_panel_layout = QVBoxLayout(left_panel)
        left_panel_layout.setContentsMargins(0, 0, 0, 0)
        left_panel_layout.setSpacing(8)
        left_panel_layout.addWidget(left_top, 1)

        # Run / Stop / Clear controls (above logs)
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
        left_panel_layout.addLayout(controls)

        # Logs (bottom-left)
        left_panel_layout.addWidget(QLabel("Logs"))
        self._progressbar = QProgressBar()
        # Always visible: idle state is determinate (not spinning).
        self._progressbar.setRange(0, 1)
        self._progressbar.setValue(0)
        self._progressbar.setTextVisible(False)
        left_panel_layout.addWidget(self._progressbar)

        self.logs = QTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setMinimumHeight(200)
        left_panel_layout.addWidget(self.logs, 1)

        content.addWidget(left_panel, 2)
        content.addWidget(right_column, 1)
        self._apply_protocol_to_form(self._protocol_params)  # Apply default protocol to dynamic form
        self._update_protocol_from_form()  # Sync initial form values to dict
        self._rebuild_sorter_params_ui()  # Build sorter params for initial sorter

        main_layout.addLayout(content, 1)

        # Widgets to disable when pipeline is running
        self._form_widgets = [
            self._file_btn, self.folder_edit, self._folder_btn,
            self.channels_display,
            self.probe_name_display, self._probe_edit_btn, self.sorter_combo,
            *self._get_protocol_form_widgets(),
            self._protocol_load_btn, self._protocol_save_btn, self._protocol_reset_btn,
            self.sorter_params_group, self.sorter_params_reset_btn,
            self.use_trigger_cb, self.rb_led, self.rb_electric,
            self.trigger_threshold_edit, self.polarity_combo,
            self.trigger_interval_edit, self.trigger_channel_edit,
            self._clear_logs_btn,
        ]

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
        """When sorter selection changes, save current params and rebuild UI for new sorter."""
        # Save OLD sorter's params before rebuild (combo already shows NEW sorter, widgets still show OLD)
        if self._current_sorter_name:
            self._update_sorter_params_from_form(target_sorter=self._current_sorter_name)
        self._rebuild_sorter_params_ui()
        self._save_last_session()

    def _rebuild_sorter_params_ui(self):
        """Rebuild the sorter parameters section for the currently selected sorter."""
        sorter_name = self.sorter_combo.currentText().strip()
        if not sorter_name:
            return
        # Clear existing widgets (labels + value widgets) from the layout
        while self.sorter_params_layout.count():
            item = self.sorter_params_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._sorter_param_widgets.clear()
        if not SORTERS_AVAILABLE:
            self._current_sorter_name = sorter_name
            return
        try:
            params = get_default_sorter_params(sorter_name)
            descriptions = get_sorter_params_description(sorter_name)
        except Exception:
            params = {}
            descriptions = {}
        # Restore saved values for this sorter (block signals to avoid N saves during init)
        saved = self._protocol_params.get("sorter_params", {}).get(sorter_name, {})
        row = 0
        for key in sorted(params.keys()):
            val = saved.get(key, params[key])
            desc = descriptions.get(key, "")
            if isinstance(val, dict):
                continue
            if isinstance(val, list) and val and not isinstance(val[0], (int, float, str, bool)):
                continue
            label = QLabel(key)
            label.setToolTip("")
            if isinstance(val, bool):
                w = QCheckBox()
                w.blockSignals(True)
                w.setChecked(val)
                w.blockSignals(False)
                w.toggled.connect(self._update_sorter_params_from_form)
            elif isinstance(val, int):
                w = QSpinBox()
                w.setRange(-999999, 999999)
                w.blockSignals(True)
                w.setValue(val)
                w.blockSignals(False)
                w.valueChanged.connect(self._update_sorter_params_from_form)
            elif isinstance(val, float):
                w = QDoubleSpinBox()
                w.setRange(-1e9, 1e9)
                w.setDecimals(4)
                w.blockSignals(True)
                w.setValue(val)
                w.blockSignals(False)
                w.valueChanged.connect(self._update_sorter_params_from_form)
            elif isinstance(val, str):
                w = QLineEdit()
                w.blockSignals(True)
                w.setText(val)
                w.blockSignals(False)
                w.textChanged.connect(self._update_sorter_params_from_form)
            elif isinstance(val, list):
                w = QLineEdit()
                w.blockSignals(True)
                w.setText(json.dumps(val))
                w.blockSignals(False)
                w.textChanged.connect(self._update_sorter_params_from_form)
            else:
                w = QLineEdit()
                w.blockSignals(True)
                w.setText(str(val))
                w.blockSignals(False)
                w.textChanged.connect(self._update_sorter_params_from_form)
            w.setMaximumWidth(180)
            w.setToolTip("")
            self.sorter_params_layout.addWidget(label, row, 0)
            self.sorter_params_layout.addWidget(self._make_info_badge(desc), row, 1)
            self.sorter_params_layout.addWidget(w, row, 2)
            self._sorter_param_widgets[key] = w
            row += 1
        self._current_sorter_name = sorter_name

    def _update_sorter_params_from_form(self, target_sorter=None):
        """Read sorter param widgets and store in protocol_params."""
        sorter_name = (target_sorter or self.sorter_combo.currentText()).strip()
        if not sorter_name:
            return
        if not SORTERS_AVAILABLE:
            return
        try:
            defaults = get_default_sorter_params(sorter_name)
        except Exception:
            defaults = {}
        self._protocol_params.setdefault("sorter_params", {})
        self._protocol_params["sorter_params"].setdefault(sorter_name, {})
        for key, w in self._sorter_param_widgets.items():
            default_val = defaults.get(key)
            if isinstance(w, QCheckBox):
                val = w.isChecked()
            elif isinstance(w, (QSpinBox, QDoubleSpinBox)):
                val = w.value()
            elif isinstance(w, QLineEdit):
                txt = w.text()
                if isinstance(default_val, list):
                    try:
                        val = json.loads(txt)
                    except (json.JSONDecodeError, TypeError):
                        val = default_val if default_val is not None else []
                elif isinstance(default_val, (int, float)):
                    try:
                        val = int(txt) if isinstance(default_val, int) else float(txt)
                    except (ValueError, TypeError):
                        val = default_val
                else:
                    val = txt
            else:
                val = default_val
            self._protocol_params["sorter_params"][sorter_name][key] = val
        self._save_last_session()

    def _reset_sorter_params_to_defaults(self):
        """Reset sorter params to SpikeInterface defaults for current sorter."""
        sorter_name = self.sorter_combo.currentText().strip()
        if not sorter_name or not SORTERS_AVAILABLE:
            return
        try:
            defaults = get_default_sorter_params(sorter_name)
        except Exception:
            return
        self._protocol_params.setdefault("sorter_params", {})
        self._protocol_params["sorter_params"][sorter_name] = copy.deepcopy(defaults)
        self._rebuild_sorter_params_ui()
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
        self._update_sorter_params_from_form()
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
        self.sorter_combo.blockSignals(True)
        try:
            idx = self.sorter_combo.findText(sorter_name)
            if idx >= 0:
                self.sorter_combo.setCurrentIndex(idx)
            else:
                self.sorter_combo.setCurrentText(sorter_name)
        finally:
            self.sorter_combo.blockSignals(False)
        self._set_probe_path(state.get("my_probe_path", self._probe_path))
        self._on_probe_path_changed()
        protocol_params = state.get("protocol_params")
        if isinstance(protocol_params, dict):
            self._protocol_params = copy.deepcopy(protocol_params)
            self._apply_protocol_to_form(protocol_params)
            self._rebuild_sorter_params_ui()
        if state.get("protocol_freq_min") is not None or state.get("protocol_freq_max") is not None:
            # Backward compatibility with older settings files.
            self.preproc_filter_choice_combo.setCurrentText("bandpass_filter")
            vals = self._preproc_step_param_input_widgets.get("bandpass_filter", {})
            if "freq_min" in vals:
                self._set_preproc_param_widget_value(vals["freq_min"], float(state.get("protocol_freq_min", 400)))
            if "freq_max" in vals:
                self._set_preproc_param_widget_value(vals["freq_max"], float(state.get("protocol_freq_max", 5000)))
            self._update_protocol_from_form()
        self._refresh_intan_channels()

    def _load_last_session(self):
        if not os.path.isfile(self._session_file):
            return
        try:
            with open(self._session_file, "r", encoding="utf-8") as f:
                self._apply_form_state(json.load(f))
            # No probe at launch: always start with empty probe.
            self._probe_path = ""
            self._update_probe_name_display()
        except Exception:
            pass

    def _save_last_session(self, immediate=False):
        """Save settings to the default session file. Debounced unless immediate=True."""
        if immediate:
            if self._save_debounce_timer:
                self._save_debounce_timer.stop()
                self._save_debounce_timer = None
            self._do_save_last_session()
            return
        if self._save_debounce_timer is None:
            self._save_debounce_timer = QTimer(self)
            self._save_debounce_timer.setSingleShot(True)
            self._save_debounce_timer.timeout.connect(self._flush_save_last_session)
        self._save_debounce_timer.start(300)

    def _flush_save_last_session(self):
        """Called when debounce timer fires; also used for immediate save."""
        if self._save_debounce_timer:
            self._save_debounce_timer.stop()
            self._save_debounce_timer = None
        self._do_save_last_session()

    def _do_save_last_session(self):
        """Perform the actual save to disk."""
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
            self._save_last_session(immediate=True)  # Also update auto-restore file
            QMessageBox.information(self, "Settings saved", f"Settings saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def closeEvent(self, event):
        self._stop_mea_editor_sync_timer()
        if MEA_EDITOR_AVAILABLE and self._mea_editor_window is not None:
            self._mea_editor_window._force_close = True
            self._mea_editor_window.close()
            self._mea_editor_window = None
        self._save_last_session(immediate=True)
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
        self._channels_debounce_timer.start(200)

    def _normalize_folder_key(self, path):
        """Normalized key for folder cache and stale-load checks."""
        p = (path or "").strip()
        if not p:
            return ""
        try:
            return os.path.normcase(os.path.abspath(p))
        except Exception:
            return p

    def _refresh_intan_channels(self):
        """Start background load of channel IDs (non-blocking)."""
        folder_path = self.folder_edit.text().strip()
        folder_key = self._normalize_folder_key(folder_path)
        self._populate_channels_table([])
        if not folder_path or not os.path.isdir(folder_path):
            self._channels_loading_key = None
            return
        # Instant display when already loaded once for this folder.
        cached = self._channels_id_cache.get(folder_key)
        if cached is not None:
            self._populate_channels_table(cached)
            return
        # Avoid duplicate background loads for the same folder.
        if (
            self._channels_loading_key == folder_key
            and self._channels_load_thread is not None
            and self._channels_load_thread.isRunning()
        ):
            self._populate_channels_table(None)
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
        self._channels_loading_key = folder_key

    def _on_channels_loaded(self, folder_path, channel_ids):
        """Called when channel load completes (on main thread). Ignore stale results."""
        loaded_key = self._normalize_folder_key(folder_path)
        current_key = self._normalize_folder_key(self.folder_edit.text())
        if loaded_key:
            self._channels_loading_key = None
        if channel_ids is not None:
            self._channels_id_cache[loaded_key] = channel_ids
        if loaded_key != current_key:
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
        self._update_probe_name_display()
        self._save_last_session()

    def _is_mea_editor_dirty(self):
        """Best-effort dirty flag check across mea-editor versions."""
        if not (MEA_EDITOR_AVAILABLE and self._mea_editor_window is not None):
            return False
        dirty = getattr(self._mea_editor_window, "is_dirty", None)
        if dirty is None:
            dirty = getattr(self._mea_editor_window, "_is_dirty", False)
        return bool(dirty)

    def _update_probe_name_display(self):
        """Update probe filename display, with '*' when editor has unsaved changes."""
        base = os.path.basename(self._probe_path) if self._probe_path else ""
        if MEA_EDITOR_AVAILABLE and self._mea_editor_window is not None:
            path = getattr(self._mea_editor_window, "current_file_path", None) or getattr(self._mea_editor_window, "_initial_path", "")
            if path:
                base = os.path.basename(path)
            elif list(getattr(self._mea_editor_window, "electrodes", {}).values()):
                base = "(unsaved probe)"
        if self._is_mea_editor_dirty() and base:
            base = f"{base} *"
        self.probe_name_display.setText(base)

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
        self._update_probe_name_display()
        self._start_mea_editor_sync_timer()

    def _on_probe_file_loaded(self, path):
        """Called when a probe file is loaded in MEA Editor (immediate update)."""
        if path and os.path.isfile(path):
            self._set_probe_path(path)
        else:
            self._update_probe_name_display()

    def _on_mea_editor_closed(self, path):
        """Called when user hides MEA Editor (clicks X). Window stays alive, content preserved."""
        self._stop_mea_editor_sync_timer()
        if path and os.path.isfile(path):
            self._set_probe_path(path)
        else:
            self._update_probe_name_display()

    def _start_mea_editor_sync_timer(self):
        """Start timer to sync probe display with MEA Editor state (modifications)."""
        self._stop_mea_editor_sync_timer()
        self._mea_editor_sync_timer = QTimer(self)
        self._mea_editor_sync_timer.timeout.connect(self._sync_probe_display_from_mea_editor)
        self._mea_editor_sync_timer.start(500)

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
        self._update_probe_name_display()

    def _get_probe_path_for_pipeline(self, folder_path=None):
        """
        Return the probe file path to use for the pipeline.
        If MEA editor was opened and has content, ALWAYS export current state (all modifications)
        to a file in the output folder so the subprocess can read it reliably.
        """
        if MEA_EDITOR_AVAILABLE and self._mea_editor_window is not None:
            electrodes = list(self._mea_editor_window.electrodes.values())
            if electrodes and folder_path and os.path.isdir(folder_path):
                try:
                    # Save directly as final run artifact (no temp probe file).
                    path = os.path.join(folder_path, "probe_used.json")
                    save_electrodes_to_file(path, electrodes, self._mea_editor_window.si_units)
                    self._probe_temp_path = None
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

    def _create_preproc_step_panel(self, step_name):
        """Create a compact panel with param widgets. Returns (panel, param_widgets_dict)."""
        panel = QWidget()
        defaults = self._preprocessing_step_defaults.get(step_name, {})
        is_filter = step_name in self._filter_steps
        param_widgets = {}
        if is_filter:
            layout = QHBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            for key, default_val in defaults.items():
                layout.addWidget(QLabel(key + ":"))
                w = self._create_preproc_param_widget(step_name, key, default_val)
                w.setMaximumWidth(90)
                param_widgets[key] = w
                layout.addWidget(w)
            layout.addStretch()
        else:
            layout = QHBoxLayout(panel)
            layout.setContentsMargins(0, 0, 0, 0)
            layout.setSpacing(8)
            for key, default_val in defaults.items():
                layout.addWidget(QLabel(key + ":"))
                w = self._create_preproc_param_widget(step_name, key, default_val)
                w.setMaximumWidth(100)
                param_widgets[key] = w
                layout.addWidget(w)
            layout.addStretch()
        panel.setVisible(False)
        return panel, param_widgets

    def _create_preproc_param_widget(self, step_name, param_name, default_val):
        options = self._preprocessing_step_param_options.get(step_name, {}).get(param_name)
        if options:
            w = QComboBox()
            w.addItems([str(v) for v in options])
            idx = w.findText(str(default_val))
            if idx >= 0:
                w.setCurrentIndex(idx)
            w.currentTextChanged.connect(self._update_protocol_from_form)
            return w
        if isinstance(default_val, bool):
            w = QCheckBox("")
            w.setChecked(default_val)
            w.toggled.connect(self._update_protocol_from_form)
            return w
        if isinstance(default_val, int):
            w = QSpinBox()
            w.setRange(-999999, 999999)
            w.setValue(default_val)
            w.valueChanged.connect(self._update_protocol_from_form)
            return w
        if isinstance(default_val, float):
            w = QDoubleSpinBox()
            w.setRange(-1e9, 1e9)
            w.setDecimals(4)
            w.setValue(default_val)
            w.valueChanged.connect(self._update_protocol_from_form)
            return w
        w = QLineEdit(str(default_val))
        w.textChanged.connect(self._update_protocol_from_form)
        return w

    def _get_preproc_param_widget_value(self, widget, default_val):
        if isinstance(widget, QComboBox):
            txt = widget.currentText()
            if isinstance(default_val, int):
                try:
                    return int(txt)
                except ValueError:
                    return default_val
            if isinstance(default_val, float):
                try:
                    return float(txt)
                except ValueError:
                    return default_val
            if isinstance(default_val, bool):
                return txt.lower() in ("1", "true", "yes")
            return txt
        if isinstance(widget, QCheckBox):
            return widget.isChecked()
        if isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            return widget.value()
        if isinstance(widget, QLineEdit):
            txt = widget.text().strip()
            if isinstance(default_val, int):
                try:
                    return int(txt)
                except ValueError:
                    return default_val
            if isinstance(default_val, float):
                try:
                    return float(txt)
                except ValueError:
                    return default_val
            if isinstance(default_val, bool):
                return txt.lower() in ("1", "true", "yes")
            return txt
        return default_val

    def _set_preproc_param_widget_value(self, widget, value):
        if isinstance(widget, QComboBox):
            idx = widget.findText(str(value))
            if idx >= 0:
                widget.setCurrentIndex(idx)
            elif widget.count() > 0:
                widget.setCurrentIndex(0)
        elif isinstance(widget, QCheckBox):
            widget.setChecked(bool(value))
        elif isinstance(widget, (QSpinBox, QDoubleSpinBox)):
            try:
                widget.setValue(value)
            except Exception:
                pass
        elif isinstance(widget, QLineEdit):
            widget.setText(str(value))

    def _set_preproc_step_enabled(self, step_name, enabled):
        """Set one preprocessing step checkbox/panel without emitting its toggled signal."""
        cb = self._preprocessing_step_enabled_widgets.get(step_name)
        if cb is not None:
            cb.blockSignals(True)
            cb.setChecked(bool(enabled))
            cb.blockSignals(False)
        panel = self._preproc_step_params_widgets.get(step_name)
        if panel is not None:
            panel.setVisible(bool(enabled))

    def _get_active_filter_step(self):
        selected = self.preproc_filter_choice_combo.currentText()
        if selected in self._filter_steps:
            return selected
        return None

    def _set_filter_steps_exclusive(self, selected_filter):
        for step_name in self._filter_steps:
            self._set_preproc_step_enabled(step_name, step_name == selected_filter)

    def _sync_filter_choice_from_checkboxes(self):
        active = "None"
        for step_name in self._filter_steps:
            panel = self._preproc_step_params_widgets.get(step_name)
            if panel is not None and panel.isVisible():
                active = step_name
                break
        self.preproc_filter_choice_combo.blockSignals(True)
        self.preproc_filter_choice_combo.setCurrentText(active)
        self.preproc_filter_choice_combo.blockSignals(False)

    def _on_preproc_step_toggled(self, step_name, checked):
        # Non-filter steps are controlled by checkboxes.
        self._set_preproc_step_enabled(step_name, checked)
        self._update_protocol_from_form()

    def _on_filter_choice_changed(self, filter_name):
        selected = None if filter_name == "None" else filter_name
        self._set_filter_steps_exclusive(selected)
        self._update_protocol_from_form()

    def _update_protocol_from_form(self):
        """Met à jour le dictionnaire protocol à chaque modification d'un champ."""
        p = self._protocol_params
        existing_pre = p.get("preprocessing", {}) if isinstance(p.get("preprocessing", {}), dict) else {}
        preprocessing = {}
        for step_name in self._preprocessing_steps_order:
            if step_name in self._filter_steps:
                if step_name != self._get_active_filter_step():
                    continue
            else:
                cb = self._preprocessing_step_enabled_widgets.get(step_name)
                if cb is None or not cb.isChecked():
                    continue
            defaults = self._preprocessing_step_defaults.get(step_name, {})
            # Preserve unknown/additional keys for this step.
            values = dict(existing_pre.get(step_name, {})) if isinstance(existing_pre.get(step_name, {}), dict) else {}
            for key, default_val in defaults.items():
                w = self._preproc_step_param_input_widgets.get(step_name, {}).get(key)
                if w is not None:
                    values[key] = self._get_preproc_param_widget_value(w, default_val)
            preprocessing[step_name] = values

        # Preserve preprocessing steps that are not represented by current GUI controls.
        for step_name, step_params in existing_pre.items():
            if step_name in preprocessing:
                continue
            if step_name in self._preprocessing_steps_order:
                # Steps controlled by GUI remain driven by UI state.
                continue
            preprocessing[step_name] = copy.deepcopy(step_params)

        p["preprocessing"] = preprocessing
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
        widgets = [
            self.preproc_filter_choice_combo,
            self.protocol_unit_locations_method,
            self.protocol_random_spikes_max, self.protocol_waveforms_ms_before, self.protocol_waveforms_ms_after,
            self.protocol_correlograms_window, self.protocol_correlograms_bin,
            self.protocol_spike_amplitudes_peak, self.protocol_template_similarity_method,
            self.protocol_template_metrics_multi,
        ]
        widgets.extend(self._preprocessing_step_enabled_widgets.values())
        for step_widgets in self._preproc_step_param_input_widgets.values():
            widgets.extend(step_widgets.values())
        return widgets

    def _apply_protocol_to_form(self, params):
        """Remplit les champs à partir du dict protocol (bloque les signaux)."""
        widgets = self._get_protocol_form_widgets()
        for w in widgets:
            w.blockSignals(True)
        pre = params.get("preprocessing", {}) if isinstance(params.get("preprocessing", {}), dict) else {}
        for step_name in self._preprocessing_steps_order:
            enabled = step_name in pre
            if step_name in self._filter_steps:
                self._set_preproc_step_enabled(step_name, enabled)
            else:
                cb = self._preprocessing_step_enabled_widgets.get(step_name)
                if cb is None:
                    continue
                self._set_preproc_step_enabled(step_name, enabled)
            defaults = self._preprocessing_step_defaults.get(step_name, {})
            current_vals = pre.get(step_name, {}) if isinstance(pre.get(step_name, {}), dict) else {}
            for key, default_val in defaults.items():
                w = self._preproc_step_param_input_widgets.get(step_name, {}).get(key)
                if w is not None:
                    self._set_preproc_param_widget_value(w, current_vals.get(key, default_val))

        self._sync_filter_choice_from_checkboxes()
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
            self._rebuild_sorter_params_ui()
            self._save_last_session(immediate=True)
            QMessageBox.information(self, "Protocol loaded", f"Protocol loaded from:\n{path}")
        except json.JSONDecodeError as e:
            QMessageBox.critical(self, "Invalid JSON", str(e))
        except (ValueError, TypeError) as e:
            QMessageBox.critical(self, "Invalid protocol", str(e))

    def _save_protocol_to_file(self):
        """Save the current protocol (preprocessing, postprocessing, sorter params) to a JSON file."""
        self._update_sorter_params_from_form()
        path, _ = QFileDialog.getSaveFileName(self, "Save protocol", "", JSON_FILTER)
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._protocol_params, f, indent=2, ensure_ascii=True)
            self._save_last_session(immediate=True)
            QMessageBox.information(self, "Protocol saved", f"Protocol saved to:\n{path}")
        except Exception as exc:
            QMessageBox.critical(self, "Save failed", str(exc))

    def _reset_protocol_defaults(self):
        default = default_protocol_params(400, 5000)
        self._protocol_params = copy.deepcopy(default)
        self._apply_protocol_to_form(default)
        self._reset_sorter_params_to_defaults()
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
            pid = self._pipeline_process.pid
            try:
                # On Windows, kill full process tree to stop sorter children instantly.
                if os.name == "nt" and pid:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True,
                        text=True,
                        timeout=2.0,
                    )
                else:
                    self._pipeline_process.terminate()
                    self._pipeline_process.join(timeout=1.0)
                    if self._pipeline_process.is_alive():
                        self._pipeline_process.kill()
                        self._pipeline_process.join(timeout=0.5)
            except Exception:
                # Last resort local kill if tree-kill failed.
                try:
                    self._pipeline_process.terminate()
                    self._pipeline_process.join(timeout=0.5)
                    if self._pipeline_process.is_alive():
                        self._pipeline_process.kill()
                        self._pipeline_process.join(timeout=0.5)
                except Exception:
                    pass
            self._pipeline_process = None
            self._reset_pipeline_state()
            self._log("Pipeline stopped.")

    def _set_sorter_progress(self, running):
        self.progress_signal.emit(running)

    def _progress_impl(self, visible):
        if visible:
            # Running: indeterminate busy animation.
            self._progressbar.setRange(0, 0)
        else:
            # Idle: keep bar visible but not animated.
            self._progressbar.setRange(0, 1)
            self._progressbar.setValue(0)

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
            self._save_last_session(immediate=True)
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
        self._save_last_session(immediate=True)
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
        try:
            self._save_run_context_files(params["output_folder"], params)
        except Exception as exc:
            self._set_run_button_state(True)
            self._show_error(
                "Save failed",
                f"Could not save probe/settings to output folder:\n{exc}",
            )
            return
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
            self._update_sorter_params_from_form()
            folder_path = self.folder_edit.text().strip()
            use_trigger = self.use_trigger_cb.isChecked()
            sorter_name = self.sorter_combo.currentText().strip()
            protocol_params = self._protocol_params
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
                "trigger_threshold": trigger_threshold,
                "trigger_edge": trigger_edge,
                "trigger_min_interval": trigger_min_interval,
                "trigger_channel_index": trigger_channel_index,
                "trigger_type": "led" if self.rb_led.isChecked() else "electric",
            }
        except Exception as exc:
            self._log(f"Validation error: {exc}")
            return None

    def _save_run_context_files(self, output_folder, pipeline_params):
        """Save probe and settings snapshots in the run output folder."""
        os.makedirs(output_folder, exist_ok=True)

        params_path = os.path.join(output_folder, "pipeline_params_used.json")
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(pipeline_params, f, indent=2, ensure_ascii=True)

        probe_path = pipeline_params.get("my_probe_path", "")
        if probe_path and os.path.isfile(probe_path):
            _, ext = os.path.splitext(probe_path)
            probe_copy_path = os.path.join(output_folder, f"probe_used{ext or '.json'}")
            if os.path.abspath(probe_path) != os.path.abspath(probe_copy_path):
                shutil.copy2(probe_path, probe_copy_path)

        self._log(
            "Saved run context files: pipeline_params_used.json, probe_used.*"
        )

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
    window.showMaximized()
    app.exec()


if __name__ == "__main__":
    run_app()
