# -*- coding: utf-8 -*-
"""
Manual execution script for Intan file analysis.

This script builds and runs a full spike-sorting workflow step by step:
1) define trigger and timestamp extraction parameters,
2) load Intan recordings,
3) define preprocessing/postprocessing protocol,
4) associate probe geometry,
5) run pipeline and generate a PDF report.

It is intended for interactive/manual runs (not as a reusable library module).
"""

# SpikeInterface ecosystem imports (core + optional helper modules).
import spikeinterface.full as sif
import spikeinterface as si  # Core API.
import spikeinterface.extractors as se
import spikeinterface.preprocessing as spre
import spikeinterface.sorters as ss

import os
import traceback
from datetime import datetime
import json
import threadpoolctl

# Project classes composing the analysis pipeline.
from trigger_class import Trigger
from timestamps_class import TimestampsParameters
from sorter_class import Sorter
from protocol_class import default_protocol_params
from intan_class import IntanFile
from probe_class import Probe
from pipeline_class import Pipeline
from pdf_generator_class import PDFGenerator




# Path to the recording session folder containing Intan files.
folder_path = r"C:\Spike Electrophysiology\20251205 - P8 retina\Recordings\20251205 - BlueLEDStim_5sON_20s_interStim_RetinaP8_retina3_251205_165648_251205_170112"

# Redirect runtime errors to a dedicated traceback file for debugging.
with open(os.path.join(folder_path, "errors_traceback.txt"), "w") as error_file:
    # Trigger definition (parameter order is important):
    # - 1st: threshold (signal value used to detect crossings),
    # - 2nd: edge (slope change that triggers detection: -1 falling, +1 rising),
    # - 3rd: minimum interval in seconds between two detected edges.
    trigger = Trigger(37000, -1, 5.1)

    # Timestamp extraction configuration using the trigger on channel 0.
    timestamps_parameters = TimestampsParameters(trigger, trigger_channel_index=0)

    # Sorting backend to use for spike sorting.
    sorter = Sorter("tridesclous2")

    # Load Intan files from the recording folder.
    rhs_files = IntanFile(folder_path)

    # Compute trigger timestamps before running downstream analyses.
    rhs_files.generate_trigger_timestamps(timestamps_parameters)

    # Protocol describing preprocessing and postprocessing settings.
    my_protocol_path = r"C:\Spike Electrophysiology\20251205 - P8 retina\Recordings\my_protocol.json"
    protocol_params = default_protocol_params(400, 5000)
    protocol_params["_file_path"] = my_protocol_path

    # Probe geometry/mapping file used to assign channels to electrode positions.
    myProbe_df = Probe("C:/Spikesorting_utilities/MEA_RdLGN64.json")
    rhs_files.associate_probe(myProbe_df)

    # Build and execute the full analysis pipeline.
    pipeline = Pipeline(sorter, folder_path, protocol_params, rhs_files)

    # Generate the final PDF report from pipeline outputs.
    PDFGenerator(folder_path, pipeline)
    

    
    


