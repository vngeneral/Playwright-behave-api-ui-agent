"""
TestRail Pending Results Store
==============================
Manages the review queue for test results awaiting human approval before
being pushed to TestRail.

File: reports/testrail/pending_results.json

Lifecycle:
    1. after_scenario hook calls ``add(result)``              → status: "pending_review"
    2. Human reviews via ``!testrail preview``                → reads queue, no write
    3. Human approves via ``!testrail push``                  → results POSTed + marked "pushed"
    4. Human discards via ``!testrail discard``               → file cleared

JSON structure::

    {
        "results": [
            {
                "case_id":       "448337",
                "status_id":     1,
                "comment":       "All steps passed",
                "scenario_name": "Successful vehicle registration",
                "elapsed":       "3s",
                "step_details":  [...],
                "status":        "pending_review",
                "added_at":      "2026-07-01T10:23:45.123456"
            },
            ...
        ]
    }

Thread safety:
    File I/O is protected by a threading.Lock — safe for the Flask webhook
    server dispatching commands while Behave scenarios run in background threads.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent.testrail.result_mapper import TestRailResult

# ---------------------------------------------------------------------------
# Storage location
# ---------------------------------------------------------------------------

_DEFAULT_STORE_PATH = Path("reports/testrail/pending_results.json")

# Status constants for entries in the queue
STATUS_PENDING = "pending_review"
STATUS_PUSHED  = "pushed"
STATUS_DISCARDED = "discarded"


class PendingStore:
    """
    Thread-safe JSON file store for TestRail results awaiting review.

    Usage::

        store = PendingStore()             # uses default path
        store.add(testrail_result)         # queue a result after a scenario
        items = store.get_pending()        # preview — returns list of dicts
        store.mark_all_pushed()            # after successful push
        store.clear()                      # discard everything
    """

    def __init__(self, path: Path | str | None = None) -> None:
        self._path = Path(path) if path else _DEFAULT_STORE_PATH
        self._lock = threading.Lock()
        self._ensure_dir()

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def add(self, result: TestRailResult) -> None:
        """
        Append one result to the pending queue.

        Called from environment.py ``after_scenario`` for every scenario
        tagged with ``@testrail_C<id>``.
        """
        entry = self._result_to_entry(result)
        with self._lock:
            data = self._load()
            data["results"].append(entry)
            self._save(data)

    def mark_all_pushed(self) -> None:
        """
        Update status of all pending_review entries to 'pushed'.

        Called after a successful ``!testrail push`` command.
        """
        with self._lock:
            data = self._load()
            for entry in data["results"]:
                if entry.get("status") == STATUS_PENDING:
                    entry["status"] = STATUS_PUSHED
                    entry["pushed_at"] = _now_iso()
            self._save(data)

    def mark_pushed(self, case_ids: list[str]) -> None:
        """
        Mark specific case IDs as pushed.

        Called when ``!testrail push --case N`` targets individual cases.
        """
        with self._lock:
            data = self._load()
            for entry in data["results"]:
                if entry.get("case_id") in case_ids and entry.get("status") == STATUS_PENDING:
                    entry["status"] = STATUS_PUSHED
                    entry["pushed_at"] = _now_iso()
            self._save(data)

    def clear(self) -> None:
        """
        Remove all entries from the store.

        Called by ``!testrail discard``.
        """
        with self._lock:
            self._save({"results": []})

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_pending(self) -> list[dict[str, Any]]:
        """Return entries with status 'pending_review'."""
        with self._lock:
            data = self._load()
        return [e for e in data["results"] if e.get("status") == STATUS_PENDING]

    def get_all(self) -> list[dict[str, Any]]:
        """Return all entries (any status)."""
        with self._lock:
            data = self._load()
        return data["results"]

    def get_status(self) -> dict[str, Any]:
        """
        Return a summary dict suitable for the ``!testrail status`` command.

        Example::

            {
                "pending_count": 3,
                "pushed_count":  7,
                "store_path":    "reports/testrail/pending_results.json",
                "pending": [...]
            }
        """
        all_entries = self.get_all()
        pending = [e for e in all_entries if e.get("status") == STATUS_PENDING]
        pushed  = [e for e in all_entries if e.get("status") == STATUS_PUSHED]
        return {
            "pending_count": len(pending),
            "pushed_count":  len(pushed),
            "store_path":    str(self._path),
            "pending":       pending,
        }

    def has_pending(self) -> bool:
        """Return True if there are any pending_review entries."""
        return bool(self.get_pending())

    # ------------------------------------------------------------------
    # Private
    # ------------------------------------------------------------------

    def _ensure_dir(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, Any]:
        """Load JSON from disk. Returns empty structure if file missing or corrupt."""
        if not self._path.exists():
            return {"results": []}
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data.get("results"), list):
                return {"results": []}
            return data
        except (json.JSONDecodeError, OSError):
            return {"results": []}

    def _save(self, data: dict[str, Any]) -> None:
        """Atomically write JSON to disk (write-then-rename)."""
        self._ensure_dir()
        tmp = self._path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, self._path)

    @staticmethod
    def _result_to_entry(result: TestRailResult) -> dict[str, Any]:
        return {
            "case_id":       result.case_id,
            "status_id":     result.status_id,
            "comment":       result.comment,
            "scenario_name": result.scenario_name,
            "elapsed":       result.elapsed,
            "step_details":  result.step_details,
            "status":        STATUS_PENDING,
            "added_at":      _now_iso(),
        }


# ---------------------------------------------------------------------------
# Module-level default instance
# ---------------------------------------------------------------------------

#: Shared singleton — use this in environment.py and command handlers.
#: Constructed lazily to avoid creating the directory on import.
_default_store: PendingStore | None = None
_store_lock = threading.Lock()


def get_default_store() -> PendingStore:
    """Return the module-level PendingStore singleton (lazy init)."""
    global _default_store
    if _default_store is None:
        with _store_lock:
            if _default_store is None:
                _default_store = PendingStore()
    return _default_store


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
