# -*- coding: utf-8 -*-
"""
Created on Mon Feb  9 12:03:23 2026

@author: WNIlabs
"""
import os
import copy
import spikeinterface.sorters as ss
import spikeinterface.curation as scur
import spikeinterface.preprocessing as spre
import spikeinterface as si

class Pipeline:
    """
    End-to-end processing pipeline for one recording and one sorter.

    Responsibilities:
      - Prepare output folder paths.
      - Inject artifact-removal preprocessing when trigger timestamps exist.
      - Run preprocessing + spike sorting.
      - Remove duplicated spikes.
      - Build and compute a SortingAnalyzer for downstream visualization/export.
    """
    
    def __init__(self, sorter, folder_path, protocol_params: dict, rhs_files):
        # Core objects passed from the main script/GUI.
        self._sorter = sorter
        # Keep an independent protocol copy to avoid side effects outside pipeline.
        self._protocol_params = copy.deepcopy(protocol_params)
        self._rhs_files = rhs_files
        # Output folders for sorter files and analyzer binary folder.
        self._output_sorter_folder = os.path.join(folder_path, f"Sorting_pipeline_{sorter.name}")
        self._output_analyzer_folder = os.path.join(folder_path, f"Analyzer_binary_pipeline_{sorter.name}")
        # Update preprocessing settings, then run the full pipeline immediately.
        self.__remove_artifacts()
        self.__pipeline_sorter_analyzer()
    
    def __remove_artifacts(self):
        """
        Add artifact-removal preprocessing when trigger timestamps are available.

        The artifacts are replaced by zeros around trigger events, based on
        current protocol conventions.
        """
        if len(self._rhs_files.trigger_timestamps) != 0:
                self._protocol_params['preprocessing']['remove_artifacts'] = {
                                                        "list_triggers": self._rhs_files.trigger_timestamps,
                                                        "mode": "zeros"}

    def __pipeline_sorter_analyzer (self):
        """
        Run preprocessing, sorter execution, curation, and analyzer computation.
        """
        # 1) Apply preprocessing and keep a single local recording object.
        rec = spre.apply_preprocessing_pipeline(
            self._rhs_files._signed_amplifier_channel_recording,
            self._protocol_params['preprocessing'],
        )
        # 2) Re-attach probe after preprocessing when available.
        if getattr(self._rhs_files, "_probe", None) is not None:
            rec = rec.set_probe(self._rhs_files._probe)
        self._rhs_files._pre_processed_signed_amplifier_channel_recording = rec

        # 3) Run sorter with the same recording object.
        sorting_results = ss.run_sorter(
            sorter_name=self._sorter.name,
            recording=rec,
            folder=self._output_sorter_folder,
            remove_existing_folder = True
        )
        
        # 4) Remove duplicated spikes and store curated sorting in rhs_files.
        self._rhs_files._sorting_dedup = scur.remove_duplicated_spikes(sorting_results)
        # 5) Create analyzer object from same recording + curated sorting.
        try:
            analyzer_result = si.create_sorting_analyzer(
                recording=rec,
                sorting=self._rhs_files._sorting_dedup,
                folder=self._output_analyzer_folder,
                format='binary_folder',
                overwrite = True
            )
        except ValueError as exc:
            if "need at least one array to concatenate" in str(exc):
                raise RuntimeError(
                    "Analyzer creation failed because sorting has no spikes to sample."
                ) from exc
            raise
        # 6) Compute postprocessing extensions and keep analyzer for plotting/PDF.
        analyzer_result.compute(self._protocol_params['postprocessing'])
        self._rhs_files._computed_analyzer_result = analyzer_result