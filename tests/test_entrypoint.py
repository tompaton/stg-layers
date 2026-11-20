import subprocess
import sys

import pytest


@pytest.mark.skipif(sys.platform != "linux", reason="Checks the Linux process command line")
def test_entrypoint_sets_process_name_without_changing_terminal_title() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            """
from pathlib import Path
from unittest.mock import patch
import stg_layers

class App:
    def run(self):
        print(Path('/proc/self/cmdline').read_bytes().rstrip(b'\\0').decode())
        print(Path('/proc/self/comm').read_text().strip())

with patch.object(stg_layers.StGitRepository, 'discover'), \\
     patch.object(stg_layers, 'StgLayersApp', return_value=App()):
    stg_layers.main()
""",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == "stg-layers\nstg-layers\n"
    assert result.stderr == ""
