"""Locate source-checkout or installed distribution resources."""

import sysconfig
from pathlib import Path


def project_data() -> Path:
    checkout = Path(__file__).resolve().parents[2]
    if (checkout / "specs" / "plugin.schema.json").is_file():
        return checkout
    return Path(sysconfig.get_path("data")) / "share" / "splice"
