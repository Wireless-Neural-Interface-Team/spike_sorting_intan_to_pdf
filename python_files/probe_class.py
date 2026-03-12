# -*- coding: utf-8 -*-
"""
Lightweight wrapper for probe geometry loaded with probeinterface.

The dataframe is filtered/reordered in IntanFile.associate_probe()
to match the channels present in the loaded recording.
"""

import os

import probeinterface as ProbeI


class Probe:
    """
    Stores probe file path and dataframe of contacts/metadata.
    """

    def __init__(self, probe_file_path):
        path = str(probe_file_path).strip()
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(f"Probe file not found: {path}")
        self._file_path = path
        self._dataframe = ProbeI.read_probeinterface(self._file_path).to_dataframe(complete=True)
