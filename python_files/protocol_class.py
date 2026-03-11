# -*- coding: utf-8 -*-
"""
Protocol configuration for the spike-sorting pipeline.

This module provides:
- TypedDict for protocol structure (type hints, IDE support),
- default_protocol_params() as single source of truth for the protocol dict.

The resulting dictionary is consumed by Pipeline and PDFGenerator.
"""

from typing import TypedDict


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
