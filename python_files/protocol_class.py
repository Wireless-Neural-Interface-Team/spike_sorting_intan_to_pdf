# -*- coding: utf-8 -*-
"""
Protocol configuration for the spike-sorting pipeline.

This module provides:
- TypedDict for protocol structure (type hints, IDE support),
- default_protocol_params() as single source of truth for the protocol dict.

The resulting dictionary is consumed by Pipeline and PDFGenerator.
"""

import copy
from typing import TypedDict

# SpikeInterface preprocessing keys for the frequency filter step only.
_PREPROCESSING_FILTER_KEYS = frozenset({"bandpass_filter", "highpass_filter", "gaussian_filter"})


class ProtocolParams(TypedDict, total=False):
    """Type hints for the protocol params dict structure."""
    preprocessing: dict
    postprocessing: dict
    _file_path: str


def default_protocol_params(min_freq: int = 400, max_freq: int = 5000) -> dict:
    """Build the default protocol dictionary.

    Single source of truth for preprocessing and postprocessing structure.
    """
    return {
        "preprocessing": {
            "filter_type": "bandpass",
            "bandpass_filter": {"freq_min": min_freq, "freq_max": max_freq},
        },
        "postprocessing": {
            "random_spikes": {},
            "noise_levels": {},
            "correlograms": {},
            "waveforms": {},
            "templates": {},
            "amplitude_scalings": {},
            "spike_amplitudes": {},
            "unit_locations": {"method": "center_of_mass"},
            "spike_locations": {},
            "template_similarity": {},
            "template_metrics": {},
            "quality_metrics": {},
        },
    }


def apply_preprocessing_filter_to_dict(
    preprocessing: dict, filter_type: str, freq_min: float, freq_max: float
) -> None:
    """
    Keep only the SpikeInterface filter key that matches the selected type.
    Mutates ``preprocessing`` in place (other steps e.g. remove_artifacts are kept).
    """
    ft = str(filter_type or "bandpass").lower()
    if ft not in ("bandpass", "highpass", "lowpass"):
        ft = "bandpass"
    for k in _PREPROCESSING_FILTER_KEYS:
        preprocessing.pop(k, None)
    if ft == "highpass":
        preprocessing["highpass_filter"] = {"freq_min": float(freq_min)}
    elif ft == "lowpass":
        preprocessing["gaussian_filter"] = {"freq_min": None, "freq_max": float(freq_max)}
    else:
        preprocessing["bandpass_filter"] = {
            "freq_min": float(freq_min),
            "freq_max": float(freq_max),
        }
    preprocessing["filter_type"] = ft


def get_preprocessing_filter_freqs(preprocessing: dict) -> tuple[str, float, float]:
    """
    Values for the GUI spin boxes: (filter_type, freq_min, freq_max).
    Supports legacy protocols that only stored bandpass_filter.
    """
    ft = str(preprocessing.get("filter_type", "bandpass") or "bandpass").lower()
    if ft not in ("bandpass", "highpass", "lowpass"):
        ft = "bandpass"
    bp = preprocessing.get("bandpass_filter") or {}
    hp = preprocessing.get("highpass_filter") or {}
    gf = preprocessing.get("gaussian_filter") or {}
    if ft == "highpass":
        fmin = float(hp.get("freq_min", bp.get("freq_min", 400)))
        fmax = float(bp.get("freq_max", 5000))
        return ft, fmin, fmax
    if ft == "lowpass":
        fmin = float(bp.get("freq_min", 400))
        fmax = float(gf.get("freq_max", bp.get("freq_max", 5000)))
        return ft, fmin, fmax
    return ft, float(bp.get("freq_min", 400)), float(bp.get("freq_max", 5000))


def preprocessing_dict_for_spikeinterface(preprocessing: dict) -> dict:
    """
    Build the dict passed to apply_preprocessing_pipeline: drops filter_type,
    keeps a single frequency-filter step first, then other steps (e.g. remove_artifacts).
    """
    p = copy.deepcopy(preprocessing)
    filter_type = str(p.pop("filter_type", None) or "bandpass").lower()
    if filter_type not in ("bandpass", "highpass", "lowpass"):
        filter_type = "bandpass"
    present = [k for k in sorted(_PREPROCESSING_FILTER_KEYS) if k in p]
    consistent = (
        len(present) == 1
        and (
            (filter_type == "bandpass" and present[0] == "bandpass_filter")
            or (filter_type == "highpass" and present[0] == "highpass_filter")
            or (filter_type == "lowpass" and present[0] == "gaussian_filter")
        )
    )
    if consistent:
        k = present[0]
        first = {k: p.pop(k)}
        return {**first, **p}
    # Legacy or mismatch (e.g. old save: filter_type highpass but only bandpass_filter).
    freqs_bp = p.pop("bandpass_filter", None) or {}
    freqs_hp = p.pop("highpass_filter", None) or {}
    freqs_gf = p.pop("gaussian_filter", None) or {}
    freq_min = float(freqs_hp.get("freq_min", freqs_bp.get("freq_min", 400)))
    freq_max = float(freqs_gf.get("freq_max", freqs_bp.get("freq_max", 5000)))
    if filter_type == "highpass":
        first = {"highpass_filter": {"freq_min": freq_min}}
    elif filter_type == "lowpass":
        first = {"gaussian_filter": {"freq_min": None, "freq_max": freq_max}}
    else:
        first = {"bandpass_filter": {"freq_min": freq_min, "freq_max": freq_max}}
    return {**first, **p}
