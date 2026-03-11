# -*- coding: utf-8 -*-
"""
Pipeline subprocess entry point.

Runs the full Intan -> sorting -> PDF pipeline in a separate process.
Receives params via pickling, sends log/progress/done via multiprocessing.Queue.
"""

import json
import os
import shutil
import copy

from trigger_class import Trigger
from timestamps_class import TimestampsParameters
from sorter_class import Sorter
from intan_class import IntanFile
from probe_class import Probe
from pipeline_class import Pipeline
from pdf_generator_class import PDFGenerator


def is_file_in_use_error(exc):
    """Détecte si une exception indique qu'un fichier est ouvert par un autre processus."""
    err_msg = str(exc).lower()
    return (
        "being used" in err_msg
        or "another process" in err_msg
        or "permission denied" in err_msg
        or "accès refusé" in err_msg
        or (hasattr(exc, "winerror") and getattr(exc, "winerror", None) == 32)
    )


def run_pipeline_in_process(params, log_queue):
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
        output_folder = params["output_folder"]
        use_trigger = params["use_trigger"]
        sorter_name = params["sorter_name"]
        my_probe_path = params["my_probe_path"]
        trigger_type = params["trigger_type"]
        my_protocol_path = os.path.join(output_folder, "protocol.json")

        _log("Starting pipeline...")
        _log(f"Output folder: {output_folder}")

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

        sorter = Sorter(sorter_name)
        _log("Loading Intan recording...")
        rhs_files = IntanFile(folder_path)
        _log(f"Recording: {rhs_files.number_of_channels} channels, {rhs_files.frequency} Hz")
        if use_trigger:
            rhs_files.generate_trigger_timestamps(timestamps_parameters)
            _log(f"Triggers detected: {len(rhs_files.trigger_timestamps)}")
        else:
            _log("Trigger detection disabled.")

        protocol_params = copy.deepcopy(params["protocol_params"])
        protocol_params["_file_path"] = my_protocol_path
        my_probe_df = Probe(my_probe_path)
        rhs_files.associate_probe(my_probe_df)

        # Save probe to output folder before PDF so the report shows the final path
        probe_dest = os.path.join(output_folder, "probe.json")
        try:
            shutil.copy2(my_probe_path, probe_dest)
            rhs_files._probe_file_path = probe_dest
            if os.path.basename(my_probe_path) == "probe_pipeline_temp.json":
                try:
                    os.unlink(my_probe_path)
                except Exception:
                    pass
        except Exception as probe_exc:
            _log(f"Warning: could not save probe.json: {probe_exc}")

        _log(f"Running {sorter_name} (sorting + analyzer)...")
        _progress(True)
        pipeline = Pipeline(sorter, output_folder, protocol_params, rhs_files)

        _log("Generating PDF report...")
        try:
            PDFGenerator(output_folder, pipeline)
        except (PermissionError, OSError) as pdf_exc:
            if is_file_in_use_error(pdf_exc):
                _progress(False)
                _log("ERROR: PDF file is already open.")
                log_queue.put(("done", "error", "file_in_use"))
                return
            raise

        # Save protocol to output folder
        try:
            protocol_to_save = {k: v for k, v in protocol_params.items() if k != "_file_path"}
            protocol_path = os.path.join(output_folder, "protocol.json")
            with open(protocol_path, "w", encoding="utf-8") as f:
                json.dump(protocol_to_save, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            _log(f"Warning: could not save protocol.json: {exc}")

        _log("Done.")

        _progress(False)
        log_queue.put(("done", "success", output_folder))
    except Exception as exc:
        _progress(False)
        _log(f"ERROR: {exc}")
        log_queue.put(("done", "error", str(exc)))
