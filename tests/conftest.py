"""
pytest conftest.py — Unit Test Path Setup
==========================================
Ensures that pytest can import modules from both the repo root and the e2e/
subdirectory when running:

    python -m pytest tests/ -v

The repo root must be on sys.path first so root-level packages take priority:
    agent/   — AI, TestRail, integrations, monitoring
    utils/    — logger, misc (shared)
    helpers/  — framework_constants (shared)
    ai/       — original AI module (still present for backwards compat)

e2e/ follows so its sub-packages are importable as:
    utils.api.vehicle_client  → e2e/utils/api/vehicle_client.py
    utils.browser.browser     → e2e/utils/browser/browser.py
    pages.page_factory        → e2e/pages/page_factory.py
"""
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent.resolve()   # repo root
_E2E  = _ROOT / "e2e"

for _path in (_ROOT, _E2E):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))
