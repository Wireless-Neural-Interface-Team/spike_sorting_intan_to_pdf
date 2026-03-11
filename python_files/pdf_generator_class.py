# -*- coding: utf-8 -*-
"""
Created on Fri Feb  6 10:13:49 2026

@author: WNIlabs
"""

import spikeinterface.widgets as sw
import os
from pprint import pformat
import textwrap
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages

plt.rcParams["figure.max_open_warning"] = 1000

class PDFGenerator:
    """
    Generate a multi-page PDF report from sorting/analyzer results.

    Report content includes:
      - protocol parameters summary text page,
      - recording traces and rasters,
      - waveform/template/location visualizations,
      - one unit summary page per detected unit,
      - waveform density map.

    """

    def __init__(self, folder, pipeline):
        # Kept for compatibility with older code style.
        self._figs = None
        self._dpi = 'figure'
        # Core objects needed to build report and output path.
        self.__format ='pdf'
        self.__rhs_files = pipeline._rhs_files
        self.__folder = folder
        self.__pipeline = pipeline
        # Generate report immediately when instance is created.
        self.__generate_pdf()

    @staticmethod
    def _save_widget_to_pdf(pdf, widget):
        """Save a SpikeInterface widget figure and close it."""
        figure = getattr(widget, "figure", None)
        if figure is not None:
            pdf.savefig(figure)
            plt.close(figure)

    def _save_extracted_spike_curves_to_pdf(self, pdf, max_spikes_per_unit=1):
        """
        Save extracted spike curves as simple custom plots.

        X axis: time (ms)
        Y axis: amplitude (V)
        """
        analyzer = self.__rhs_files._computed_analyzer_result
        waveforms_ext = analyzer.get_extension("waveforms")
        if waveforms_ext is None:
            return

        unit_ids = list(self.__rhs_files._sorting_dedup.get_unit_ids())
        # Reuse SpikeInterface unit color mapping to match widget colors.
        unit_colors = None
        get_some_colors_fn = getattr(sw, "get_some_colors", None)
        if callable(get_some_colors_fn):
            try:
                unit_colors = get_some_colors_fn(unit_ids)
            except Exception:
                unit_colors = None
        if unit_colors is None:
            try:
                from spikeinterface.widgets.utils import get_some_colors as get_some_colors_fn_utils
                unit_colors = get_some_colors_fn_utils(unit_ids)
            except Exception:
                unit_colors = {}

        sampling_frequency = float(analyzer.sampling_frequency)
        for i, unit_id in enumerate(unit_ids):
            waveforms = waveforms_ext.get_waveforms_one_unit(unit_id=unit_id, force_dense=True)
            if waveforms is None or waveforms.shape[0] == 0:
                continue

            # Use the channel with highest peak-to-peak across extracted waveforms.
            # One unit_id can have multiple channels, so we need to find the best channel.
            best_channel = int(np.argmax(np.ptp(waveforms, axis=(0, 1))))
            channel_ids = getattr(analyzer, "channel_ids", None)
            if channel_ids is not None and best_channel < len(channel_ids):
                channel_label = channel_ids[best_channel]
            else:
                channel_label = best_channel

            # Build a time axis centered on spike peak (t=0), converted from samples to milliseconds.
            num_samples = waveforms.shape[1]
            nbefore = num_samples // 2
            time_ms = (np.arange(num_samples) - nbefore) * 1000.0 / sampling_frequency
            
            # Plot a limited number of spikes (max_spikes_per_unit) to avoid overcrowding.
            # Currently, max_spikes_per_unit is set to 1, so we will plot only one spike per unit.
            spikes_to_plot = min(int(max_spikes_per_unit), int(waveforms.shape[0]))
            for spike_index in range(spikes_to_plot):
                # Waveforms are usually in uV, convert to V.
                amplitude_v = waveforms[spike_index, :, best_channel] * 1e-6

                fig, ax = plt.subplots(figsize=(8.3, 4.2))
                line_color = unit_colors.get(unit_id, f"C{i % 10}")
                ax.plot(time_ms, amplitude_v, linewidth=1.0, color=line_color)
                if spikes_to_plot > 1:
                    ax.set_title(f"Spike {spike_index + 1} | unit_id={unit_id} | channel={channel_label}")
                else:
                    ax.set_title(f"unit_id={unit_id} | channel={channel_label}")
                ax.set_xlabel("Time (ms)")
                ax.set_ylabel("Amplitude (V)")
                ax.grid(True, alpha=0.3)
                pdf.savefig(fig)
                plt.close(fig)

    def _build_summary_text(self):
        """Build a structured text report for PDF page 1."""
        rhs = self.__rhs_files
        protocol_params = self.__pipeline._protocol_params
        sorter = self.__pipeline._sorter
        ts_params = getattr(rhs, "_timestamps_parameters", None)
        trigger = getattr(ts_params, "trigger", None)

        protocol_pre = pformat(protocol_params.get("preprocessing", {}), width=100, compact=False)
        protocol_post_keys = list(protocol_params.get("postprocessing", {}).keys())
        bandpass = protocol_params.get("preprocessing", {}).get("bandpass_filter", {})

        channel_count = len(rhs.channel_ids) if rhs.channel_ids is not None else 0
        channel_ids_full = ", ".join(map(str, rhs.channel_ids)) if channel_count else "N/A"

        trigger_type = getattr(ts_params, "trigger_type", "N/A")
        trigger_threshold = getattr(trigger, "threshold", "N/A")
        trigger_edge_val = getattr(trigger, "edge", None)
        trigger_polarity = "Rising Edge" if trigger_edge_val == 1 else "Falling Edge" if trigger_edge_val == -1 else "N/A"
        trigger_min_interval = getattr(trigger, "min_interval", "N/A")
        trigger_channel_index = getattr(ts_params, "trigger_channel_index", "N/A")

        summary_lines = [
            "SPIKESORTING SUMMARY REPORT",
            "=" * 80,
            "",
            "[1] Protocol",
            f"- Protocol file path: {protocol_params.get('_file_path', 'N/A')}",
            f"- Bandpass min/max (Hz): {bandpass.get('freq_min', 'N/A')} / {bandpass.get('freq_max', 'N/A')}",
            f"- Preprocessing config: {protocol_pre}",
            f"- Postprocessing steps ({len(protocol_post_keys)}): {', '.join(protocol_post_keys)}",
            "",
            "[2] Intan Recording",
            f"- Folder path: {getattr(rhs, 'folder_path', 'N/A')}",
            f"- Sampling frequency (Hz): {getattr(rhs, 'frequency', 'N/A')}",
            f"- Number of channels: {getattr(rhs, 'number_of_channels', 'N/A')}",
            f"- Number of segments: {getattr(rhs, 'number_of_segments', 'N/A')}",
            f"- Channel IDs (full list): {channel_ids_full}",
            "",
            "[3] Trigger",
            f"- Type: {trigger_type}",
            f"- Threshold: {trigger_threshold}",
            f"- Polarity: {trigger_polarity}",
            f"- Min interval (s): {trigger_min_interval}",
            f"- Detected trigger count: {len(getattr(rhs, 'trigger_timestamps', []))}",
            "",
            "[4] Probe",
            f"- Probe file path: {getattr(rhs, '_probe_file_path', 'N/A')}",
            f"- Probe attached: {getattr(rhs, '_probe', None) is not None}",
            "",
            "[5] Sorter",
            f"- Sorter name: {getattr(sorter, 'name', 'N/A')}",
            f"- Output sorter folder: {self.__pipeline._output_sorter_folder}",
            f"- Output analyzer folder: {self.__pipeline._output_analyzer_folder}",
            "",
            "[6] Timestamp Parameters",
            f"- Trigger channel index: {trigger_channel_index}",
            "",
        ]
        # Wrap long lines to keep text inside page width.
        # break_long_words=True is important for very long file paths.
        wrapped_lines = []
        for line in summary_lines:
            if not line.strip():
                wrapped_lines.append("")
            else:
                wrapped_lines.extend(textwrap.wrap(line, width=84, break_long_words=True, break_on_hyphens=True))
        return "\n".join(wrapped_lines)

    def __generate_pdf(self):
        # Output file path in the recording folder.
        filename = os.path.join(
            self.__folder,
            f"Summary_figures_sorting_{self.__pipeline._sorter.name}.{self.__format}",
        )
        print(f"PDF output: {filename}")
        with PdfPages(filename) as pdf:
            # Page 1: structured project/pipeline summary.
            text = self._build_summary_text()
            line_count = text.count("\n") + 1
            # Estimate page height from number of lines to avoid text clipping.
            page_height = max(11, 1.5 + (line_count * 0.18))
            fig_text = plt.figure(figsize=(8.3, page_height))
            # Use a wider text area to reduce unused right-side whitespace.
            ax = fig_text.add_axes([0.005, 0, 0.995, 1])
            ax.text(
                0.01,
                0.98,
                text,
                fontsize=8.5,
                family='monospace',
                va='top',
                ha='left',
                transform=ax.transAxes,
                wrap=True,
            )
            ax.axis('off')  # Désactiver les axes
            pdf.savefig(fig_text)
            plt.close(fig_text)
            
            # Plot preprocessed traces (first minute).
            plot = sw.plot_spikes_on_traces(self.__rhs_files._computed_analyzer_result, time_range=(0, 60))
            self._save_widget_to_pdf(pdf, plot)
            
            # Plot spike rasters (first 5 minutes).
            w_rs = sw.plot_rasters(self.__rhs_files._computed_analyzer_result, time_range=(0, 300))
            self._save_widget_to_pdf(pdf, w_rs)
            
            # Plot unit waveforms with max 8 unit_id per PDF page.
            unit_ids = list(self.__rhs_files._sorting_dedup.get_unit_ids())
            units_per_page = 8
            for start in range(0, len(unit_ids), units_per_page):
                unit_ids_chunk = unit_ids[start:start + units_per_page]
                plot_waveforms = sw.plot_unit_waveforms(
                    self.__rhs_files._computed_analyzer_result,
                    unit_ids=unit_ids_chunk,
                    same_axis=False,
                    scalebar=True,
                    plot_legend=False,
                    plot_templates=False,
                    plot_channels=False,
                )

                fig_waveforms = getattr(plot_waveforms, "figure", None)
                if fig_waveforms is not None:
                    # Keep enough margins so scalebar labels are not clipped at figure borders.
                    fig_waveforms.subplots_adjust(left=0.06, right=0.97, bottom=0.07, top=0.93, wspace=0.35, hspace=0.5)
                    for i, ax in enumerate(fig_waveforms.axes):
                        if i < len(unit_ids_chunk):
                            ax.set_title(f"unit_id={unit_ids_chunk[i]}")
                        # Reposition scalebar labels inside each axis to avoid right-edge clipping.
                        ax.set_xticks([])
                        ax.set_yticks([])
                        ax.set_xlabel("")
                        ax.set_ylabel("")
                        for spine in ax.spines.values():
                            spine.set_visible(True)
                self._save_widget_to_pdf(pdf, plot_waveforms)

            # Extract and plot one spike curve per unit (time in ms, amplitude in V).
            self._save_extracted_spike_curves_to_pdf(pdf, max_spikes_per_unit=1)

            # Plot templates over probe/channel geometry context.
            plot_templates_probe = sw.plot_unit_templates(
                            self.__rhs_files._computed_analyzer_result,
                            same_axis=True,
                            templates_percentile_shading=(),
                            plot_channels=True,
                            plot_legend=False,
                        )
            fig_templates_probe = getattr(plot_templates_probe, "figure", None)
            if fig_templates_probe is not None:
                fig_templates_probe.axes[0].set_title("Units location")                
            self._save_widget_to_pdf(pdf, plot_templates_probe)
            
            # One detailed summary page per unit.
            for unit_id in self.__rhs_files._sorting_dedup.get_unit_ids():
                plot_summary = sw.plot_unit_summary(
                    self.__rhs_files._computed_analyzer_result,
                    unit_id=unit_id,
                    subwidget_kwargs={
                        # API-native customization for each subpanel.
                        "unit_locations": {},
                        "unit_waveforms": {},
                        "unit_waveform_density_map": {},
                        "autocorrelograms": {},
                        "amplitudes": {},
                    },
                )
                self._save_widget_to_pdf(pdf, plot_summary)

            # Global waveform density map overview.
            plot_density = sw.plot_unit_waveforms_density_map(self.__rhs_files._computed_analyzer_result, same_axis=True)
            self._save_widget_to_pdf(pdf, plot_density)


            
            
            
            
        