#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GUI helper components used by gui_run_pipeline.py.

This module intentionally keeps a minimal surface:
- ChannelsLoaderWorker: async channel-id loader worker
- EmbeddedMEAEditor: optional editor integration (disabled if unavailable)
- save_electrodes_to_file: optional export helper
"""

from PySide6.QtCore import QObject, Signal

from intan_class import load_channel_ids_only


class ChannelsLoaderWorker(QObject):
    """Worker that loads Intan channel IDs in a background QThread."""

    finished = Signal(str, object)  # (folder_path, channel_ids or None)

    def __init__(self, folder_path):
        super().__init__()
        self._folder_path = folder_path

    def run(self):
        try:
            channel_ids = load_channel_ids_only(self._folder_path)
            self.finished.emit(self._folder_path, channel_ids)
        except Exception:
            self.finished.emit(self._folder_path, None)


# Optional MEA editor integration (keep GUI functional when package is missing).
MEA_EDITOR_AVAILABLE = False
EmbeddedMEAEditor = None


def save_electrodes_to_file(path, electrodes, si_units):
    """Placeholder when external MEA editor package is not installed."""
    raise RuntimeError("MEA editor is not available in this environment.")

