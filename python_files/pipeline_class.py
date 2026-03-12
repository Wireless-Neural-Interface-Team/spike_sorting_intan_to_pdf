# -*- coding: utf-8 -*-
"""
Created on Mon Feb  9 12:03:23 2026

@author: WNIlabs
"""
import json
import os
import copy
from collections import OrderedDict
import spikeinterface.sorters as ss
import spikeinterface.curation as scur
import spikeinterface.preprocessing as spre
import spikeinterface as si
from spikeinterface.core.job_tools import fix_job_kwargs, get_best_job_kwargs


def _get_pipeline_job_kwargs():
    """Job kwargs for pipeline: native get_best_job_kwargs + overrides."""
    try:
        job_kwargs = get_best_job_kwargs()
        job_kwargs.update(chunk_memory="100M", progress_bar=True)
        return job_kwargs
    except Exception:
        return {"n_jobs": -1, "chunk_memory": "100M", "progress_bar": True}


def _sanitize_sorter_params(sorter_name, params):
    """
    Ensure sorter params have correct types. Params that should be dicts but are
    strings (e.g. from JSON serialization) are parsed or removed.
    """
    if not params:
        return params
    try:
        defaults = ss.get_default_sorter_params(sorter_name)
    except Exception:
        defaults = {}
    sanitized = {}
    for key, val in params.items():
        default_type = type(defaults.get(key)) if key in defaults else None
        if isinstance(val, str) and default_type is dict:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, dict):
                    sanitized[key] = parsed
                else:
                    sanitized[key] = val
            except (json.JSONDecodeError, TypeError):
                pass  # skip invalid param
        elif isinstance(val, str) and default_type is list:
            try:
                parsed = json.loads(val)
                if isinstance(parsed, list):
                    sanitized[key] = parsed
                else:
                    sanitized[key] = val
            except (json.JSONDecodeError, TypeError):
                pass
        elif key == "nested_params" and not isinstance(val, dict):
            # tridesclous expects nested_params to be dict or None
            pass
        else:
            sanitized[key] = val
    return sanitized

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
        self.__apply_job_kwargs()
        self.__pipeline_sorter_analyzer()
    
    def __apply_job_kwargs(self):
        """Apply job_kwargs using SpikeInterface's get_best_job_kwargs()."""
        try:
            job_kwargs = fix_job_kwargs(_get_pipeline_job_kwargs())
            si.set_global_job_kwargs(**job_kwargs)
        except Exception:
            pass

    def __remove_artifacts(self):
        """
        Add artifact-removal preprocessing when trigger timestamps are available.

        Merges list_triggers from recording with user-configured params (ms_before,
        ms_after, mode) from the protocol.
        """
        if len(self._rhs_files.trigger_timestamps) != 0:
            existing = self._protocol_params.get("preprocessing", {}).get("remove_artifacts", {})
            if not isinstance(existing, dict):
                existing = {}
            self._protocol_params.setdefault("preprocessing", {})["remove_artifacts"] = {
                "list_triggers": self._rhs_files.trigger_timestamps,
                "mode": existing.get("mode", "zeros"),
                "ms_before": existing.get("ms_before", 0.5),
                "ms_after": existing.get("ms_after", 3.0),
                **{k: v for k, v in existing.items() if k not in ("list_triggers", "mode", "ms_before", "ms_after")},
            }

    def __pipeline_sorter_analyzer (self):
        """
        Run preprocessing, sorter execution, curation, and analyzer computation.
        """
        # 1) Apply preprocessing and keep a single local recording object.
        # `detect_bad_channels` is not a recording preprocessor in SI pipeline;
        # handle it explicitly around apply_preprocessing_pipeline. Skip if
        # detect_and_remove_bad_channels or detect_and_interpolate_bad_channels
        # is used (they handle detection in-pipeline).
        preprocessing_params = copy.deepcopy(self._protocol_params["preprocessing"])
        has_detect_and_remove = "detect_and_remove_bad_channels" in preprocessing_params
        has_detect_and_interpolate = "detect_and_interpolate_bad_channels" in preprocessing_params
        detect_bad_cfg = None
        if not (has_detect_and_remove or has_detect_and_interpolate):
            detect_bad_cfg = preprocessing_params.pop("detect_bad_channels", None)
        input_rec = self._rhs_files._amplifier_channel_recording

        # Safety: always run unsigned_to_signed first when configured.
        if "unsigned_to_signed" in preprocessing_params:
            ordered_pre = OrderedDict()
            ordered_pre["unsigned_to_signed"] = preprocessing_params["unsigned_to_signed"]
            for key, value in preprocessing_params.items():
                if key == "unsigned_to_signed":
                    continue
                ordered_pre[key] = value
            preprocessing_params = ordered_pre

        # Skip steps with invalid params (e.g. zero_channel_pad with num_channels=0)
        if preprocessing_params.get("zero_channel_pad", {}).get("num_channels", 1) == 0:
            preprocessing_params = {k: v for k, v in preprocessing_params.items() if k != "zero_channel_pad"}

        rec = spre.apply_preprocessing_pipeline(
            input_rec,
            preprocessing_params,
        )

        if detect_bad_cfg is not None:
            detect_bad_cfg = detect_bad_cfg if isinstance(detect_bad_cfg, dict) else {}
            bad_channel_ids, _ = spre.detect_bad_channels(rec, **detect_bad_cfg)
            if len(bad_channel_ids) > 0:
                rec = rec.remove_channels(bad_channel_ids)
        # 2) Re-attach probe after preprocessing when available.
        if getattr(self._rhs_files, "_probe", None) is not None:
            rec = rec.set_probe(self._rhs_files._probe)
        self._rhs_files._pre_processed_recording = rec

        # 3) Run sorter with the same recording object.
        sorter_params = copy.deepcopy(self._protocol_params.get("sorter_params", {}).get(self._sorter.name, {}))
        sorter_params = _sanitize_sorter_params(self._sorter.name, sorter_params)
        job_kwargs = _get_pipeline_job_kwargs()
        try:
            default_params = ss.get_default_sorter_params(self._sorter.name)
            if "job_kwargs" in default_params:
                sorter_params.setdefault("job_kwargs", {}).update(job_kwargs)
        except Exception:
            pass
        sorting_results = ss.run_sorter(
            sorter_name=self._sorter.name,
            recording=rec,
            folder=self._output_sorter_folder,
            remove_existing_folder=True,
            **sorter_params,
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
        job_kwargs = _get_pipeline_job_kwargs()
        analyzer_result.compute(self._protocol_params['postprocessing'], **job_kwargs)
        self._rhs_files._computed_analyzer_result = analyzer_result