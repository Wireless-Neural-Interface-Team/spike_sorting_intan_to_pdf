#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Compatibility wrapper.

Some code refers to `gui_pipeline_run`, while the canonical module name is
`gui_pipeline_runner`. Re-export the public runner API from here.
"""

from gui_pipeline_runner import run_pipeline_in_process, is_file_in_use_error

__all__ = ["run_pipeline_in_process", "is_file_in_use_error"]

