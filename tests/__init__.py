"""Test package.

Pins SPOTTYTUBE_SETTINGS to a non-existent temp path so neither the tools
nor the bridge ever read the developer's real
``~/.config/spottytube/settings.json`` during a test run: every settings
value must come from the schema defaults unless a test says otherwise.
Test cases that point the variable at their own fixture must restore the
previous value when they finish (see ``TempSettingsCase``).
"""

import os
import tempfile

_settings_home = tempfile.mkdtemp(prefix="spottytube-tests-")
os.environ["SPOTTYTUBE_SETTINGS"] = os.path.join(_settings_home, "settings.json")
