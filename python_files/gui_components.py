#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GUI helper components used by gui_run_pipeline.py.
"""

from PySide6.QtCore import QObject, Signal

try:
    from intan_class import load_channel_ids_only
except ImportError:
    # Support package-style imports too.
    from .intan_class import load_channel_ids_only


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


try:
    from mea_editor.electrode_array_editor_qt import ElectrodeArrayEditorQt
    from mea_editor.electrode_array_editor_io import save_electrodes_to_file

    MEA_EDITOR_AVAILABLE = True

    class EmbeddedMEAEditor(ElectrodeArrayEditorQt):
        """
        Adapter around mea_editor window. When user clicks X, we hide instead of
        destroy so the probe content is preserved. Re-show on next Load/Edit.
        """

        def __init__(self, initial_path="", on_file_loaded=None, on_close_callback=None):
            super().__init__()
            self._initial_path = initial_path or ""
            self._on_file_loaded_cb = on_file_loaded
            self._on_close_cb = on_close_callback
            self._force_close = False  # True when main app closes, to really destroy

            if self._initial_path:
                try:
                    self._load_array_from_file(self._initial_path)
                    self.current_file_path = self._initial_path
                except Exception:
                    pass

        def _load_array_from_file(self, path):
            super()._load_array_from_file(path)
            self._initial_path = path
            self.current_file_path = path
            if callable(self._on_file_loaded_cb):
                self._on_file_loaded_cb(path)

        def closeEvent(self, event):
            try:
                self.is_dirty = False
            except Exception:
                pass
            if self._force_close:
                event.accept()
                return
            # User clicked X: hide instead of destroy to preserve probe content.
            if callable(self._on_close_cb):
                try:
                    current = getattr(self, "current_file_path", "") or self._initial_path
                    self._on_close_cb(current)
                except Exception:
                    pass
            event.ignore()
            self.hide()

except Exception:
    MEA_EDITOR_AVAILABLE = False
    EmbeddedMEAEditor = None

    def save_electrodes_to_file(path, electrodes, si_units):
        raise RuntimeError("MEA editor is not available in this environment.")

