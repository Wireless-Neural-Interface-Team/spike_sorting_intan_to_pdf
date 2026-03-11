# -*- coding: utf-8 -*-
"""
Reusable GUI components for the pipeline launcher.

- ChannelsLoaderWorker: loads Intan channel IDs in background thread
- EmbeddedMEAEditor: MEA editor window for probe modification (when mea-editor is installed)
"""

import os

from PySide6.QtCore import Qt, Signal, QObject

from intan_class import load_channel_ids_only

# MEA Editor (optional)
try:
    from mea_editor import ElectrodeArrayEditorQt, save_electrodes_to_file
    MEA_EDITOR_AVAILABLE = True
except ImportError:
    MEA_EDITOR_AVAILABLE = False
    ElectrodeArrayEditorQt = None

    def save_electrodes_to_file(path, electrodes, si_units):
        raise RuntimeError("mea-editor is not installed")


class ChannelsLoaderWorker(QObject):
    """Worker that loads channel IDs in a background thread."""
    finished = Signal(str, object)  # (folder_path, channel_ids or None)

    def __init__(self, folder_path):
        super().__init__()
        self._folder_path = folder_path

    def run(self):
        try:
            ch_ids = load_channel_ids_only(self._folder_path)
            self.finished.emit(self._folder_path, ch_ids)
        except Exception:
            self.finished.emit(self._folder_path, None)


if MEA_EDITOR_AVAILABLE:
    class EmbeddedMEAEditor(ElectrodeArrayEditorQt):
        """
        MEA Editor for pipeline GUI. Loads probe from path if provided, else opens empty.
        User loads probe via File > Open. Notifies parent when file is loaded or on close.
        """

        def __init__(self, initial_path: str, on_file_loaded=None, on_close_callback=None):
            super().__init__()
            self._initial_path = initial_path or ""
            self._startup_done = False
            self._on_file_loaded = on_file_loaded
            self._on_close_callback = on_close_callback
            self.setWindowTitle("MEA Editor - Probe")
            self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)

        def showEvent(self, event):
            if self._initial_path and os.path.isfile(self._initial_path) and not self._startup_done:
                self._startup_done = True
                try:
                    self._load_array_from_file(self._initial_path)
                    self.current_file_path = ""  # Ne pas afficher le chemin d'origine
                    self.is_dirty = False
                    self._update_title()
                    if self._on_file_loaded:
                        self._on_file_loaded(self._initial_path)
                except Exception:
                    pass
            super().showEvent(event)

        def _prompt_open_array_file(self):
            """Override: notify parent as soon as user loads a file via File > Open."""
            ok = super()._prompt_open_array_file()
            if ok and self._on_file_loaded:
                path = getattr(self, "current_file_path", None)
                if path and os.path.isfile(path):
                    self._on_file_loaded(path)
            return ok

        def _update_title(self):
            """Titre sans chemin de fichier."""
            dirty_suffix = " *" if self.is_dirty else ""
            self.setWindowTitle(f"MEA Editor - Probe{dirty_suffix}")

        def closeEvent(self, event):
            if self._on_close_callback:
                path = getattr(self, "current_file_path", None) or self._initial_path
                if path and os.path.isfile(path):
                    self._on_close_callback(path)
            event.accept()
            self.hide()
else:
    EmbeddedMEAEditor = None
