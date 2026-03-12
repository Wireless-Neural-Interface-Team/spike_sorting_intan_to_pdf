#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Subprocess pipeline runner used by gui_run_pipeline.py.
"""

import os
import traceback

from trigger_class import Trigger
from timestamps_class import TimestampsParameters
from sorter_class import Sorter
from intan_class import IntanFile
from probe_class import Probe
from pipeline_class import Pipeline
from pdf_generator_class import PDFGenerator


def is_file_in_use_error(exc):
    """Best-effort detection for 'file already open/locked' errors."""
    text = str(exc).lower()
    return (
        isinstance(exc, PermissionError)
        or "winerror 32" in text
        or "being used" in text
        or "another process" in text
        or "permission denied" in text
        or "accès refusé" in text
    )


def run_pipeline_in_process(params, log_queue):
    """Run full Intan -> sorter -> PDF pipeline in child process."""

    def _log(msg):
        log_queue.put(("log", str(msg)))

    def _progress(running):
        log_queue.put(("progress", bool(running)))

    try:
        folder_path = params["folder_path"]
        output_folder = params["output_folder"]
        sorter_name = params["sorter_name"]
        my_probe_path = params["my_probe_path"]
        protocol_params = params["protocol_params"]
        use_trigger = params.get("use_trigger", False)

        _log(f"folder_path: {folder_path}")
        _log(f"output_folder: {output_folder}")
        _log(f"sorter_name: {sorter_name}")

        rhs_files = IntanFile(folder_path)

        if use_trigger:
            trigger = Trigger(
                threshold=params["trigger_threshold"],
                edge=params["trigger_edge"],
                min_interval=params["trigger_min_interval"],
            )
            ts_params = TimestampsParameters(
                trigger=trigger,
                trigger_channel_index=params["trigger_channel_index"],
                trigger_type=params.get("trigger_type", "electric"),
            )
            _log("Generating trigger timestamps...")
            rhs_files.generate_trigger_timestamps(ts_params)

        _log("Associating probe...")
        my_probe_df = Probe(my_probe_path)
        rhs_files.associate_probe(my_probe_df)

        _log("Running sorter + analyzer (this can take time)...")
        _progress(True)
        sorter = Sorter(sorter_name)
        pipeline = Pipeline(sorter, output_folder, protocol_params, rhs_files)

        _log("Generating PDF report...")
        PDFGenerator(output_folder, pipeline)
        pdf_path = os.path.join(output_folder, f"Summary_figures_sorting_{sorter_name}.pdf")
        _log(f"PDF generated: {pdf_path}")

        _progress(False)
        log_queue.put(("done", "success", output_folder))
    except Exception as exc:
        _progress(False)
        if is_file_in_use_error(exc):
            log_queue.put(("done", "error", "file_in_use"))
        else:
            log_queue.put(("log", "ERROR: Spike sorting error trace:"))
            log_queue.put(("log", traceback.format_exc()))
            log_queue.put(("done", "error", str(exc)))

