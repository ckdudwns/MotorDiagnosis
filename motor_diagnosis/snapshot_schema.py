"""Transactional v3 -> v4 sensor identity migration; originals/jobs are retained."""


def add_sensor_identity(db):
    if db.execute("SELECT version FROM periodic_snapshot_schema").fetchone()[0] == 4:
        return
    columns = [{row[1] for row in db.execute("PRAGMA table_info("+table+")")}
               for table in ("snapshot_streams", "periodic_snapshots")]
    if all("sensor" in names for names in columns):
        # An earlier job-table-only migration may be applied to a v4 inbox.
        with db:
            db.execute("UPDATE periodic_snapshot_schema SET version=4")
        return
    if any("sensor" in names for names in columns):
        raise ValueError("Inconsistent snapshot sensor schema")
    # No foreign keys refer to these tables. Their public ordinals remain fixed.
    db.executescript("""
        BEGIN IMMEDIATE;
        CREATE TABLE snapshot_streams_v4(
            device TEXT NOT NULL, boot TEXT NOT NULL, context TEXT NOT NULL,
            anchor_uptime INTEGER NOT NULL, anchor_captured REAL NOT NULL,
            sensor TEXT NOT NULL DEFAULT '', PRIMARY KEY(device,sensor,boot));
        INSERT INTO snapshot_streams_v4 SELECT *,'' FROM snapshot_streams;
        CREATE TABLE periodic_snapshots_v4(
            ordinal INTEGER PRIMARY KEY AUTOINCREMENT,
            device TEXT NOT NULL, site TEXT NOT NULL, asset TEXT NOT NULL,
            boot TEXT NOT NULL, idx INTEGER NOT NULL, uptime INTEGER NOT NULL,
            captured REAL NOT NULL, received REAL NOT NULL,
            digest TEXT NOT NULL, body TEXT NOT NULL, late INTEGER NOT NULL,
            quality TEXT NOT NULL, status TEXT NOT NULL, result TEXT,
            sensor TEXT NOT NULL DEFAULT '', UNIQUE(device,sensor,boot,idx));
        INSERT INTO periodic_snapshots_v4 SELECT *,'' FROM periodic_snapshots;
        UPDATE sqlite_sequence SET seq=max(seq,coalesce(
            (SELECT seq FROM sqlite_sequence WHERE name='periodic_snapshots'),0))
            WHERE name='periodic_snapshots_v4';
        DROP TABLE periodic_snapshots;
        ALTER TABLE periodic_snapshots_v4 RENAME TO periodic_snapshots;
        DROP TABLE snapshot_streams;
        ALTER TABLE snapshot_streams_v4 RENAME TO snapshot_streams;
        CREATE INDEX snapshot_queue ON periodic_snapshots(status,ordinal);
        CREATE INDEX snapshot_latest ON periodic_snapshots(device,site,asset,captured DESC,late ASC,ordinal DESC);
        CREATE INDEX snapshot_device_latest ON periodic_snapshots(device,captured DESC,late ASC,ordinal DESC);
        CREATE INDEX snapshot_sensor_history ON periodic_snapshots(device,sensor,boot,captured DESC,ordinal DESC);
        UPDATE periodic_snapshot_schema SET version=4;
        COMMIT;
    """)
