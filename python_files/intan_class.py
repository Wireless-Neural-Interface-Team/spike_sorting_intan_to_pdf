# -*- coding: utf-8 -*-
"""
Created on Fri Feb  6 10:16:39 2026

@author: WNIlabs
"""
from pathlib import Path

import numpy as np
import spikeinterface.extractors as se
import probeinterface as ProbeI


def load_channel_ids_only(folder_path):
    """
    Lightweight load: reads only the first Intan file in the folder to get channel_ids.
    All split files in a session share the same channel configuration, so one file
    is sufficient. Much faster than loading the entire folder with read_split_intan_files.
    Returns list of channel IDs or None on error.
    """
    try:
        folder = Path(folder_path)
        if not folder.exists() or not folder.is_dir():
            return None
        file_list = [p for p in folder.iterdir() if p.suffix.lower() in [".rhd", ".rhs"]]
        if not file_list:
            return None
        file_list.sort(key=lambda x: x.name)
        first_file = file_list[0]
        rec = se.read_intan(
            first_file,
            stream_name="RHS2000 amplifier channel",
            use_names_as_ids=False,
            all_annotations=True,
        )
        return list(rec.get_channel_ids())
    except Exception:
        return None


class IntanFile:
    """
    Container class for one Intan recording folder and derived objects.

    It centralizes:
      - raw recording streams (stim/ADC/amplifier),
      - extracted traces and metadata (sampling rate, channel ids),
      - trigger timestamps used for artifact handling,
      - probe association information,
      - outputs produced later by pipeline stages (sorting/analyzer).
    """

    def __init__(self, folder_path):
        # Input folder containing split Intan files.
        self.folder_path = folder_path
        
        # Public metadata populated after loading.
        self.trigger_timestamps = np.array([])
        self.frequency = None
        self.channel_ids = None
        
        # SpikeInterface recording extractors by stream.
        self._adc_channel_recording = None
        self._amplifier_channel_recording = None
        
        # Placeholder for preprocessing output.
        self._pre_processed_recording = None 
        
        # Probe and downstream outputs.
        self._probe = None
        self._timestamps_parameters = None

        self._computed_analyzer_result = None
        self._sorting_dedup = None  
        
        # Load recording streams immediately at object creation.
        self._load_recording()
        
        
        
        
    def _load_recording(self):
        """
        Load Intan split files into SpikeInterface recording extractors.

        Two streams are loaded:
          - USB board ADC input channel,
          - RHS2000 amplifier channel.
        """
        # Concatenate files from the folder as one logical recording.
        mode = "concatenate"
        all_annotations = True
        use_names_as_ids = False
        
        # Read ADC stream (used for trigger detection in this project).
        self._adc_channel_recording = se.read_split_intan_files(
            self.folder_path,
            mode=mode,
            stream_name="USB board ADC input channel",
            use_names_as_ids=use_names_as_ids,
            all_annotations=all_annotations,
        )
        # Read amplifier stream (main neural data used for sorting).
        self._amplifier_channel_recording = se.read_split_intan_files(
            self.folder_path,
            mode=mode,
            stream_name="RHS2000 amplifier channel",
            use_names_as_ids=use_names_as_ids,
            all_annotations=all_annotations,
        )
        self.frequency = self._amplifier_channel_recording.get_sampling_frequency()
        self.channel_ids = self._amplifier_channel_recording.get_channel_ids()
        self.number_of_channels = self._amplifier_channel_recording.get_num_channels()
        self.number_of_segments = self._amplifier_channel_recording.get_num_segments()
        # Keep raw amplifier recording; unsigned_to_signed is handled by protocol preprocessing.
    

    def generate_trigger_timestamps(self,
                       timestamps_parameters):
        """
        Detect trigger timestamps from the ADC signal.

        Detection logic:
          - thresholding on ADC samples,
          - edge detection on thresholded transitions,
          - sample index to seconds conversion.
        """
        # Read ADC trace for the requested trigger channel only.
        self._timestamps_parameters = timestamps_parameters
        channel_ids = self._adc_channel_recording.get_channel_ids()
        channel_index = timestamps_parameters.trigger_channel_index
        if channel_index < 0 or channel_index >= len(channel_ids):
            raise ValueError(
                f"trigger_channel_index={channel_index} is out of range "
                f"for {len(channel_ids)} ADC channels."
            )

        signal = self._adc_channel_recording.get_traces(
                                      channel_ids=[channel_ids[channel_index]],
                                      start_frame=None,
                                      end_frame=None,
                                      ).squeeze()
        frequency = self._adc_channel_recording.get_sampling_frequency()
        # Boolean mask under threshold, then edge transitions.
        signal_under_threshold = signal < timestamps_parameters.trigger.threshold
        trigger_samples = np.where(np.diff(signal_under_threshold.astype(int)) == timestamps_parameters.trigger.edge)[0]
        # Store trigger timestamps in seconds.
        trigger_timestamps = trigger_samples / frequency

        # Apply minimum inter-trigger interval filtering in seconds.
        min_interval = timestamps_parameters.trigger.min_interval
        if min_interval > 0 and trigger_timestamps.size > 0:
            kept_timestamps = [trigger_timestamps[0]]
            for ts in trigger_timestamps[1:]:
                if (ts - kept_timestamps[-1]) >= min_interval:
                    kept_timestamps.append(ts)
            trigger_timestamps = np.array(kept_timestamps)

        self.trigger_timestamps = trigger_timestamps
    
    def associate_probe (self, probe):
        """
        Attach probe geometry to amplifier recording after channel alignment.

        Steps:
          - load probe dataframe from file,
          - keep only channels present in the recording,
          - reorder and reindex device_channel_indices,
          - convert dataframe to Probe object and attach it.
        """
        self._probe_file_path = getattr(probe, "_file_path", None)
        self._probe = probe
        probe_df = self._probe._dataframe

        # Keep only probe contacts present in recording channels.
        probe_df["contact_ids"] = probe_df["contact_ids"].astype(str)
        probe_df = probe_df[probe_df["contact_ids"].isin([str(ch) for ch in self.channel_ids])]
        probe_df = probe_df.drop_duplicates(subset="contact_ids")
        # Vérifier que le nombre de contacts du côté probe correspond à celui de l'enregistrement
        n_probe_contacts = len(probe_df)
        n_recording_channels = len(self.channel_ids)
        if n_probe_contacts != n_recording_channels:
            raise ValueError(
                f"Incohérence entre le nombre de contacts du probe ({n_probe_contacts}) "
                f"et le nombre de canaux de l'enregistrement ({n_recording_channels}). "
                "Vérifiez le mapping des 'contact_ids' ou la sélection des canaux."
            )
        probe_df = probe_df.sort_values(by="device_channel_indices")
        # Renumber device_channel_indices (0-based) for SpikeInterface recording channel mapping.
        probe_df["device_channel_indices"] = range(len(probe_df))

        # Build probe object and attach to recording.
        self._probe = ProbeI.Probe.from_dataframe(probe_df)
        self._amplifier_channel_recording = self._amplifier_channel_recording.set_probe(self._probe)


    