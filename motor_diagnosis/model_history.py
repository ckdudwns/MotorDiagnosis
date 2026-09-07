"""Read-only device identity protection, independent of model execution.

Ingest commits the identity with the input in ModelInferenceStore. This guard
reads that same persistent table even without a checkpoint or ML dependencies.
It never creates/migrates a database, prunes inputs, or runs pending inference.
"""

from contextlib import closing
from pathlib import Path
import sqlite3

from . import data, device_lifecycle


class ModelHistoryGuard:
    def __init__(self, database):
        # An in-memory store has no cross-restart history; its live inference
        # store retains the existing in-process guard.
        self.path = None if str(database) == ":memory:" else Path(database).absolute()
        self._observed_database = False
        if self.path is not None:
            try:
                self.path.stat()
                self._observed_database = True
            except FileNotFoundError:
                pass  # A fresh deployment need not create an unused model DB.
        device_lifecycle.register_history(self)

    @device_lifecycle.serialized
    def check_device_deletion(self, device_id):
        if self.path is None:
            return
        try:
            try:
                self.path.stat()
            except FileNotFoundError:
                if self._observed_database:
                    raise  # A known DB disappearing is not proof of no history.
                return
            self._observed_database = True
            # Query on every identity mutation: do not freeze a startup snapshot.
            # as_uri escapes spaces, # and Unicode safely. mode=ro cannot silently
            # replace an unavailable database with an empty writable database.
            with closing(
                sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
            ) as db:
                found = db.execute(
                    "SELECT 1 FROM identities WHERE device=?", (device_id,)
                ).fetchone()
        except (OSError, sqlite3.Error) as exc:
            raise data.ApiError(
                503,
                "MODEL_HISTORY_UNAVAILABLE",
                "Model device history could not be checked; identity changes are blocked.",
            ) from exc
        if found:
            raise data.ApiError(
                409,
                "DEVICE_HAS_HISTORY",
                "Model input history prevents device deletion and ID reuse.",
            )

    def close(self):
        device_lifecycle.unregister_history(self)
