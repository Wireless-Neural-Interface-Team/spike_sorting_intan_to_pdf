# -*- coding: utf-8 -*-
"""
Created on Fri Feb  6 12:18:36 2026

@author: WNIlabs
"""

import spikeinterface.sorters as ss

class Sorter:
    """
    This class stores:
      - the sorter name selected by the user (e.g. "tridesclous2"),
      - the default parameters provided by SpikeInterface for this sorter,
      - a human-readable description of these parameters.
    """

    def __init__(self, sorter_name):
        # Name used by spikeinterface.sorters.run_sorter(sorter_name=...).
        self.name = sorter_name
        try:
            # Default parameter dictionary for the selected sorter.
            self.param = ss.get_default_sorter_params(sorter_name)
            # Text descriptions that explain each sorter parameter.
            self.param_description = ss.get_sorter_params_description(sorter_name)
        except Exception as exc:
            try:
                installed = ss.installed_sorters()
            except Exception:
                installed = None
            msg = f"Sorter '{sorter_name}' is not available in this SpikeInterface environment."
            if installed is not None:
                msg += f" Installed sorters: {installed}"
            raise RuntimeError(msg) from exc

    def __repr__(self):
        # Developer-friendly string representation for debug/printing.
        return (f"Sorter(name='{self.name}', "
                f"param={self.param}, "
                f"param_description={self.param_description})")