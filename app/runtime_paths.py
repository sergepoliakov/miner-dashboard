# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 sergepoliakov
"""Untracked per-user configuration and runtime state; never stored in the clone."""
import os
from pathlib import Path
CONFIG_DIR = Path(os.environ.get('MINER_DASHBOARD_CONFIG', Path(os.environ.get('XDG_CONFIG_HOME', Path.home()/'.config'))/'miner-dashboard'))
STATE_DIR = Path(os.environ.get('MINER_DASHBOARD_STATE', Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'miner-dashboard'))
