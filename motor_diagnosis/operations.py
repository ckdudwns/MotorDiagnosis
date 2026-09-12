"""Local maintenance CLI. Reports contain metadata, never account/token contents.

Backups stop one systemd service and restart it in finally; restore drills write
only a NEW directory. No live restore, database deletion, or cloud API calls.
"""
from __future__ import annotations

import argparse
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import sqlite3
import subprocess
import time
import uuid

from .rf66_package import read_package_files

DB_DEFAULTS = {
    "STATE_DB_PATH": "output/runtime.sqlite3",
    "ALERT_DB_PATH": "output/alerts.sqlite3",
    "ANALYSIS_DB_PATH": "output/analysis.sqlite3",
    "VIBRATION_WINDOW_DB_PATH": "output/vibration-windows.sqlite3",
    "RAW_VIBRATION_WINDOW_DB_PATH": "output/raw-vibration-windows.sqlite3",
    "PERIODIC_SNAPSHOT_DB_PATH": "output/periodic-snapshots.sqlite3",
    "COMMUNICATION_QUALITY_DB_PATH": "output/communication-quality.sqlite3",
    "SHADOW_MODEL_DB_PATH": "output/model-inference.sqlite3",
}
FILE_KEYS = ("AUTH_USERS_FILE", "INGEST_TOKENS_FILE", "RF66_MODEL_ARTIFACT", "SHADOW_MODEL_ARTIFACT")
SAFE_ENV = {*DB_DEFAULTS, *FILE_KEYS, "RF66_MODEL_CHECKSUM", "RF66_EVENT_MODE", "SHADOW_MODEL_CHECKSUM"}
GiB = 1024 ** 3


def stamp():
    return datetime.now(timezone.utc).isoformat()


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=120).stdout.strip()


def service_info(service):
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.service)?", service):
        raise ValueError("Invalid service name")
    output = run("systemctl", "show", service, "--no-pager",
                 "--property=MainPID,ActiveState,WorkingDirectory,User,FragmentPath,DropInPaths")
    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def discover(service):
    """Use the running process's allowlisted paths, including systemd overrides."""
    info = service_info(service)
    pid = int(info.get("MainPID", "0"))
    if info.get("ActiveState") != "active" or pid <= 0:
        raise ValueError("Service must be active for configuration discovery")
    proc = Path("/proc") / str(pid)
    project = Path(os.readlink(proc / "cwd")).resolve(strict=True)
    argv = (proc / "cmdline").read_bytes().split(b"\0")
    if not any(os.fsdecode(arg) in {"app.py", str(project / "app.py")} for arg in argv):
        raise ValueError("Service process is not this application's app.py")
    env = {}
    for item in (proc / "environ").read_bytes().split(b"\0"):
        key, sep, value = item.partition(b"=")
        name = os.fsdecode(key)
        if sep and name in SAFE_ENV:
            env[name] = os.fsdecode(value)
    if service_info(service).get("MainPID") != str(pid):
        raise ValueError("Service changed during discovery; retry")
    return project, env, info


def local_path(project, value):
    if not value or value == ":memory:":
        raise ValueError("Operations require persistent paths")
    path = Path(value).expanduser()
    return (path if path.is_absolute() else project / path).absolute()


def regular(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected a regular file: {path}")
    return path


def readonly(path):
    regular(path)
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
    db.execute("PRAGMA query_only=ON")
    return db


def digest(path):
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def private_directory(path, *, fresh=False):
    path.mkdir(mode=0o700, parents=True, exist_ok=not fresh)
    if path.is_symlink() or not path.is_dir():
        raise ValueError("Backup/report directory must be a regular directory")
    if os.name == "posix" and path.stat().st_mode & 0o077:
        raise ValueError("Private directory requires mode 700")


def write_json(path, value):
    # Exclusive writes protect prior reports and backups. Partial work is retained.
    with path.open("x", encoding="utf-8") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


@contextmanager
def maintenance_lock(project):
    """One cooperating backup at a time, independent of destination directory."""
    path = project / ".operations-backup.lock"
    if path.is_symlink():
        raise ValueError("Maintenance lock cannot be a symlink")
    with path.open("a+b") as handle:
        os.chmod(path, 0o600)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "posix":
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        yield  # Closing releases the OS lock even after a failure.


def inventory(project, env, info):
    if env.get("RF66_MODEL_CHECKSUM") and not env.get("RF66_MODEL_ARTIFACT"):
        raise ValueError("RF66 checksum requires a model artifact")
    files, absent, seen = [], [], set()
    for key, default in DB_DEFAULTS.items():
        path = local_path(project, env.get(key, default))
        if not path.exists() and key == "SHADOW_MODEL_DB_PATH" and not env.get("SHADOW_MODEL_ARTIFACT"):
            absent.append(key)
            continue
        regular(path)
        resolved = path.resolve()
        if resolved in seen:
            raise ValueError("Database paths overlap; refusing partial backup")
        seen.add(resolved)
        files.append((key, path, "sqlite"))
    if not env.get("AUTH_USERS_FILE"):
        raise ValueError("Production AUTH_USERS_FILE is required")
    for key in FILE_KEYS:
        if env.get(key):
            path = regular(local_path(project, env[key]))
            files.append((key, path, "model" if "ARTIFACT" in key else "private_config"))
    # Systemd paths are returned by systemctl, not evaluated as shell commands.
    for index, value in enumerate([info.get("FragmentPath", ""), *info.get("DropInPaths", "").split()]):
        if value:
            files.append((f"SYSTEMD_{index}", regular(Path(value)), "private_config"))
    caddy = Path("/etc/caddy/Caddyfile")
    if caddy.is_file():
        files.append(("CADDYFILE", regular(caddy), "private_config"))
    return files, absent


def check_database(path):
    with closing(readonly(path)) as db:
        if db.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite integrity check failed")


def make_backup(project, env, info, destination, *, stopped):
    """Caller owns maintenance boundary; recheck it before and after every copy."""
    files, absent = inventory(project, env, info)
    private_directory(destination)
    needed = sum(p.stat().st_size + (Path(str(p)+"-wal").stat().st_size if Path(str(p)+"-wal").exists() else 0)
                 for _, p, _ in files) + 2 * GiB
    if shutil.disk_usage(destination).free < needed:
        raise ValueError("Insufficient backup free space (copy size plus 2 GiB reserve)")
    folder = destination / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:12])
    private_directory(folder, fresh=True)
    manifest = {"formatVersion": 2, "createdAt": stamp(), "complete": True,
                "consistency": "service_stopped", "project": str(project),
                "serviceUser": info.get("User"), "eventMode": env.get("RF66_EVENT_MODE", "shadow"),
                "modelChecksum": env.get("RF66_MODEL_CHECKSUM"), "files": [], "absentOptional": absent}
    try:
        manifest["commit"] = run("git", "-c", f"safe.directory={project}", "-C", str(project), "rev-parse", "HEAD")
    except (OSError, subprocess.SubprocessError):
        manifest["commit"] = None
    for key, path, kind in files:
        if not stopped():
            raise ValueError("Service is not stopped; incomplete backup retained")
        output = folder / (key + (".sqlite3" if kind == "sqlite" else ".bin"))
        if kind == "sqlite":
            with closing(readonly(path)) as source, closing(sqlite3.connect(output)) as target:
                source.backup(target)
            os.chmod(output, 0o600)
            check_database(output)
        else:
            # Private folder prevents readers even during copy.
            shutil.copyfile(path, output)
            os.chmod(output, 0o600)
        if not stopped():
            raise ValueError("Service restarted during backup; incomplete backup retained")
        sha = digest(output)
        if key == "RF66_MODEL_ARTIFACT":
            # sha tracks the entire backup ZIP; the configured checksum pins
            # model/candidate.joblib inside it. Never deserialize for maintenance.
            read_package_files(output, env.get("RF66_MODEL_CHECKSUM"))
        manifest["files"].append({"key": key, "file": output.name, "kind": kind,
                                  "originalPath": str(path), "bytes": output.stat().st_size, "sha256": sha})
    write_json(folder / "manifest.json", manifest)  # completion marker LAST
    return folder


def backup_service(service, destination):
    project, env, info = discover(service)
    with maintenance_lock(project):
        # Re-read effective configuration after acquiring the backup lock.
        latest_project, env, info = discover(service)
        if project != latest_project:
            raise ValueError("Service project changed during backup setup")
        return _backup_service_locked(service, project, env, info, destination)


def _backup_service_locked(service, project, env, info, destination):
    inventory(project, env, info)  # reject missing paths BEFORE stopping
    private_directory(destination)
    marker = project / ".operations-backup-resume.json"
    if marker.exists():
        raise ValueError("An earlier restart is pending; run resume-backup first")
    write_json(marker, {"service": service, "createdAt": stamp()})
    # Stop may fail after issuing the request, so restart belongs in finally too.
    try:
        run("systemctl", "stop", service)
        def stopped():
            state = service_info(service)
            return state.get("ActiveState") == "inactive" and state.get("MainPID") == "0"
        folder = make_backup(project, env, info, destination, stopped=stopped)
    finally:
        run("systemctl", "start", service)
        marker.unlink()
    return {"backup": str(folder), "verified": verify_backup(folder)["complete"],
            "serviceState": service_info(service).get("ActiveState")}


def resume_backup(project, service):
    """ExecStopPost only restarts when this backup actually requested a stop."""
    marker = project / ".operations-backup-resume.json"
    if not marker.exists():
        return {"restartScheduled": False}
    regular(marker)
    value = json.loads(marker.read_text(encoding="utf-8"))
    if value.get("service") != service:
        raise ValueError("Restart marker belongs to another service")
    service_info(service)  # Validate service name before passing to systemctl.
    run("systemctl", "--no-block", "start", service)
    marker.unlink()
    return {"restartScheduled": True}


def verify_backup(folder):
    regular(folder / "manifest.json")
    manifest = json.loads((folder / "manifest.json").read_text(encoding="utf-8"))
    if (manifest.get("formatVersion") not in (1, 2) or manifest.get("complete") is not True
            or manifest.get("consistency") != "service_stopped"):
        raise ValueError("Incomplete/unsupported backup")
    files = manifest.get("files", [])
    keys = [item["key"] for item in files]
    names = [item["file"] for item in files]
    required = (set(DB_DEFAULTS) - {"SHADOW_MODEL_DB_PATH"}) | {"AUTH_USERS_FILE"}
    if manifest["formatVersion"] == 1:
        # Backups created before the periodic inbox must remain restorable.
        required.discard("PERIODIC_SNAPSHOT_DB_PATH")
    if not required <= set(keys) or len(keys) != len(set(keys)) or len(names) != len(set(names)):
        raise ValueError("Incomplete/duplicate database set")
    if manifest.get("modelChecksum") and "RF66_MODEL_ARTIFACT" not in keys:
        raise ValueError("RF66 model is missing from backup")
    for item in files:
        if not re.fullmatch(r"[A-Z0-9_]+\.(sqlite3|bin)", item["file"]):
            raise ValueError("Invalid backup member name")
        path = regular(folder / item["file"])
        if path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise ValueError("Backup hash/size mismatch")
        if item["key"] in DB_DEFAULTS:
            if item["kind"] != "sqlite":
                raise ValueError("Database type mismatch")
            check_database(path)
        if item["key"] == "RF66_MODEL_ARTIFACT":
            read_package_files(path, manifest.get("modelChecksum"))
    return manifest


def restore_drill(folder, destination):
    manifest = verify_backup(folder)
    private_directory(destination, fresh=True)  # cannot overwrite a live directory
    for item in manifest["files"]:
        output = destination / item["file"]
        shutil.copyfile(folder / item["file"], output)
        os.chmod(output, 0o600)
    write_json(destination / "manifest.json", manifest)
    verify_backup(destination)
    return {"restoredTo": str(destination), "verified": True, "files": len(manifest["files"])}


def inspect(project, env, device, *, now=None):
    now = time.time() if now is None else now
    report = {"at": now, "deviceId": device, "eventMode": env.get("RF66_EVENT_MODE", "shadow"),
              "databases": {}, "issues": []}
    def issue(level, code):
        report["issues"].append({"level": level, "code": code})
    for key, default in DB_DEFAULTS.items():
        path = local_path(project, env.get(key, default))
        item = {"path": str(path), "exists": path.exists()}
        report["databases"][key] = item
        if not path.exists():
            if key != "SHADOW_MODEL_DB_PATH" or env.get("SHADOW_MODEL_ARTIFACT"):
                issue("critical", key + ":MISSING")
            continue
        usage = shutil.disk_usage(path.parent)
        item.update(bytes=path.stat().st_size, freeBytes=usage.free,
                    diskUsedPct=round(100 * usage.used / usage.total, 2),
                    walBytes=Path(str(path)+"-wal").stat().st_size if Path(str(path)+"-wal").exists() else 0)
        if usage.free < 2 * GiB or item["diskUsedPct"] >= 90:
            issue("critical", key + ":DISK_SPACE")
        elif item["diskUsedPct"] >= 80:
            issue("warning", key + ":DISK_SPACE")
        try:
            with closing(readonly(path)) as db:
                if key in {"RAW_VIBRATION_WINDOW_DB_PATH", "VIBRATION_WINDOW_DB_PATH"}:
                    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                    item["statuses"] = dict(db.execute("SELECT status,count(*) FROM vibration_windows GROUP BY status"))
                    item["rows"] = sum(item["statuses"].values())
                    limit = 300000 if key == "RAW_VIBRATION_WINDOW_DB_PATH" else 500000
                    item["rowLimit"] = limit
                    if item["rows"] >= limit * .9:
                        issue("warning", key + ":ROW_CAPACITY")
                    pending = item["statuses"].get("queued", 0)
                    oldest = db.execute("SELECT min(captured) FROM vibration_windows WHERE status='queued'").fetchone()[0]
                    if key == "RAW_VIBRATION_WINDOW_DB_PATH" and "raw_delivery" in tables:
                        item["oldestPendingMeasurementAgeSec"] = None if oldest is None else now-oldest
                        # Archival acquisition time is not processing wait time.
                        oldest = db.execute("""SELECT min(CASE WHEN d.metadata IS NOT NULL THEN d.received ELSE w.captured END)
                            FROM vibration_windows w LEFT JOIN raw_delivery d ON w.ordinal=d.ordinal WHERE w.status='queued'""").fetchone()[0]
                    item["oldestPendingAgeSec"] = None if oldest is None else now-oldest
                    if pending >= 3276 or (oldest is not None and now-oldest > 30):
                        issue("warning", key + ":PROCESSING_BACKLOG")
                    if key == "RAW_VIBRATION_WINDOW_DB_PATH":
                        row = db.execute("SELECT ordinal,captured,boot,idx,body,status,result FROM vibration_windows WHERE device=? ORDER BY captured DESC,ordinal DESC LIMIT 1", (device,)).fetchone()
                        sequence = db.execute("SELECT seq FROM sqlite_sequence WHERE name='vibration_windows'").fetchone()
                        item["acceptedTotal"] = sequence[0] if sequence else 0
                        if row:
                            body, analysis = json.loads(row[4]), json.loads(row[6]) if row[6] else {}
                            item["latest"] = {"ordinal": row[0], "captured": row[1], "bootId": row[2],
                                "index": row[3], "uptimeUs": body["startUptimeUs"], "quality": body["quality"],
                                "status": row[5], "reason": analysis.get("reason"), "ageSec": now-row[1]}
                            cadence_limit = 30
                            if "raw_delivery" in tables:
                                delivery = db.execute("SELECT received,metadata FROM raw_delivery WHERE ordinal=?", (row[0],)).fetchone()
                                if delivery and delivery[1]:
                                    metadata = json.loads(delivery[1])
                                    item["latest"]["transmission"] = metadata
                                    item["latest"]["receivedAtEpoch"] = delivery[0]
                                    if metadata.get("policyId") == "edge-trigger-batch-v1" and metadata.get("mode") == "periodic":
                                        cadence_limit = 360  # 300s reporting period + 60s transport grace; NOT event freshness.
                            item["latest"]["maxExpectedAgeSec"] = cadence_limit
                            if not -5 <= now-row[1] <= cadence_limit:
                                issue("warning", "RAW_INPUT_STALE_OR_FUTURE")
                            if body["quality"] != "valid":
                                issue("warning", "RAW_INPUT_QUALITY")
                            if row[5] in {"unavailable", "waiting_model"}:
                                issue("warning", "RF66_INFERENCE_UNAVAILABLE")
                        else:
                            issue("warning", "RAW_INPUT_ABSENT")
                        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if "rf66_incidents" in tables:
                            item["incidents"] = db.execute("SELECT count(*) FROM rf66_incidents").fetchone()[0]
                            item["unprojectedIncidents"] = db.execute("SELECT count(*) FROM rf66_incidents WHERE projected<revision").fetchone()[0]
                            if item["incidents"] >= 9000:
                                issue("warning", "RF66_INCIDENT_CAPACITY")
                            if item["unprojectedIncidents"]:
                                issue("warning", "RF66_EVENT_PROJECTION_PENDING")
                elif key == "PERIODIC_SNAPSHOT_DB_PATH":
                    from .periodic_snapshots import MAX_ROWS
                    item["statuses"] = dict(db.execute("SELECT status,count(*) FROM periodic_snapshots GROUP BY status"))
                    item["rows"] = sum(item["statuses"].values())
                    item["rowLimit"] = MAX_ROWS
                    item["processingEnabled"] = False
                    item["stage"] = "ingest_only"
                    # Queued rows are intentional in stage 1, not a failed worker.
                    oldest = db.execute("SELECT min(received) FROM periodic_snapshots WHERE status='queued'").fetchone()[0]
                    item["oldestPendingAgeSec"] = None if oldest is None else now-oldest
                    if item["rows"] >= MAX_ROWS * .9:
                        issue("warning", key + ":ROW_CAPACITY")
                    row = db.execute("""SELECT captured,received,boot,idx,quality,status FROM periodic_snapshots
                        WHERE device=? ORDER BY captured DESC,late ASC,ordinal DESC LIMIT 1""", (device,)).fetchone()
                    if row:
                        item["latest"] = dict(zip(("captured", "receivedAtEpoch", "bootId", "index", "quality", "status"), row))
                elif key == "ALERT_DB_PATH":
                    item["statuses"] = dict(db.execute("SELECT status,count(*) FROM alert_deliveries GROUP BY status"))
                    old = db.execute("SELECT count(*) FROM alert_deliveries WHERE status IN ('pending','sending') AND due_at<?", (now-120,)).fetchone()[0]
                    if old:
                        issue("warning", "ALERT_DELIVERY_BACKLOG")
        except (sqlite3.Error, ValueError, KeyError, TypeError):
            issue("critical", key + ":UNREADABLE_OR_UNSUPPORTED")
    report["status"] = "critical" if any(i["level"] == "critical" for i in report["issues"]) else "warning" if report["issues"] else "ok"
    return report


def backup_status(root, *, now=None):
    now = time.time() if now is None else now
    candidates = [p for p in root.glob("*/manifest.json") if p.is_file() and not p.is_symlink() and not p.parent.is_symlink()]
    if not candidates:
        return {"status": "warning", "code": "NO_COMPLETE_BACKUP"}
    latest = max(candidates, key=lambda p: p.stat().st_mtime)
    try:
        metadata = json.loads(latest.read_text(encoding="utf-8"))
        captured = datetime.fromisoformat(metadata["createdAt"]).timestamp()
        if metadata.get("complete") is not True:
            raise ValueError("Incomplete")
        age = now-captured
        return {"status": "ok" if 0 <= age <= 36*3600 else "warning", "ageSec": age,
                "backup": str(latest.parent), "code": "BACKUP_AGE", "hashesChecked": False}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "critical", "code": "BACKUP_MANIFEST_UNREADABLE"}


def observe(service, device, seconds, interval, destination):
    if seconds < interval or interval < 1:
        raise ValueError("Require duration >= interval >= 1 second")
    private_directory(destination.parent)
    deadline, previous, count, progressed, worst = time.monotonic()+seconds, None, 0, False, "ok"
    with destination.open("x", encoding="utf-8") as output:
        os.chmod(destination, 0o600)
        while True:
            try:
                project, env, info = discover(service)
                report = inspect(project, env, device)
                report["pid"] = info["MainPID"]
                latest = report["databases"]["RAW_VIBRATION_WINDOW_DB_PATH"].get("latest")
                if latest and previous and latest["ordinal"] > previous["ordinal"]:
                    progressed = True
                    if latest["captured"] <= previous["captured"]:
                        report["issues"].append({"level": "critical", "code": "MEASUREMENT_TIME_NOT_ADVANCING"})
                        report["status"] = "critical"
                if latest:
                    previous = latest
            except (OSError, ValueError, subprocess.SubprocessError):
                report = {"at": time.time(), "status": "critical", "issues": [{"level": "critical", "code": "SERVICE_UNAVAILABLE"}]}
            count += 1
            if report["status"] == "critical" or (worst == "ok" and report["status"] == "warning"):
                worst = report["status"]
            output.write(json.dumps(report, ensure_ascii=False, allow_nan=False)+"\n")
            output.flush()
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(interval, remaining))
    if not progressed and worst == "ok":
        worst = "warning"
    return {"report": str(destination), "observations": count, "status": worst,
            "rawInputProgressObserved": progressed, "fieldValidation": "not_performed"}


def revoke_upload_key(path, key, *, apply=False):
    if not re.fullmatch(r"[A-Za-z0-9+/]+={0,2}", key):
        raise ValueError("Expected an exact SSH public key's base64 field")
    regular(path)
    original = path.read_bytes()
    lines = original.splitlines(keepends=True)
    def matches(line):
        if line.lstrip().startswith(b"#"):
            return False
        try:
            fields = shlex.split(line.decode("utf-8"), comments=False)
        except (UnicodeError, ValueError):
            return False  # Preserve malformed/unrecognized lines for operator review.
        if len(fields) < 2:
            return False
        # A key line has either keytype/key or one (possibly quoted) options field.
        # Never search comments or command="..." for the public key substring.
        index = 0 if fields[0].startswith(("ssh-", "ecdsa-", "sk-")) else 1
        return len(fields) > index+1 and fields[index] == "ssh-ed25519" and fields[index+1] == key
    matched = [line for line in lines if matches(line)]
    result = {"matched": len(matched), "changed": False}
    if not apply or not matched:
        return result
    backup = path.with_name(path.name + ".before-rf66-revoke-" + uuid.uuid4().hex[:12])
    with backup.open("xb") as handle:
        os.chmod(backup, 0o600)
        handle.write(original)
    replacement = path.with_name(path.name + ".rf66-revoke-" + uuid.uuid4().hex[:12])
    with replacement.open("xb") as handle:
        os.chmod(replacement, 0o600)
        handle.writelines(line for line in lines if line not in matched)
    if os.name == "posix":
        stat = path.stat()
        os.chown(replacement, stat.st_uid, stat.st_gid)
    if path.read_bytes() != original:
        raise ValueError("authorized_keys changed concurrently; no replacement performed")
    os.replace(replacement, path)
    return {**result, "changed": True, "backup": str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "backup", "observe"):
        sub = commands.add_parser(name)
        sub.add_argument("--service", default="motordiagnosis")
        if name != "backup":
            sub.add_argument("--device", default="DEV-01-MOT-02")
        if name == "inspect":
            sub.add_argument("--backup-root", type=Path)
        if name == "backup":
            sub.add_argument("--destination", type=Path, required=True)
        elif name == "observe":
            sub.add_argument("--seconds", type=int, default=300)
            sub.add_argument("--interval", type=int, default=10)
            sub.add_argument("--output", type=Path, required=True)
    for name in ("verify-backup", "restore-drill"):
        sub = commands.add_parser(name)
        sub.add_argument("--backup", type=Path, required=True)
        if name == "restore-drill":
            sub.add_argument("--destination", type=Path, required=True)
    sub = commands.add_parser("revoke-upload-key")
    sub.add_argument("--authorized-keys", type=Path, required=True)
    sub.add_argument("--key-base64", required=True)
    sub.add_argument("--apply", action="store_true")
    sub = commands.add_parser("resume-backup")
    sub.add_argument("--service", default="motordiagnosis")
    args = parser.parse_args()
    try:
        if args.command == "inspect":
            project, env, info = discover(args.service)
            result = inspect(project, env, args.device)
            result["serviceState"] = info["ActiveState"]
            if args.backup_root:
                result["backup"] = backup_status(args.backup_root)
                if result["backup"]["status"] == "critical" or (result["status"] == "ok" and result["backup"]["status"] == "warning"):
                    result["status"] = result["backup"]["status"]
        elif args.command == "backup":
            result = backup_service(args.service, args.destination)
        elif args.command == "verify-backup":
            manifest = verify_backup(args.backup)
            result = {"verified": True, "createdAt": manifest["createdAt"], "files": len(manifest["files"])}
        elif args.command == "restore-drill":
            result = restore_drill(args.backup, args.destination)
        elif args.command == "observe":
            result = observe(args.service, args.device, args.seconds, args.interval, args.output)
        elif args.command == "resume-backup":
            result = resume_backup(Path.cwd(), args.service)
        else:
            result = revoke_upload_key(args.authorized_keys, args.key_base64, apply=args.apply)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return {"warning": 1, "critical": 2}.get(result.get("status"), 0)
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error, subprocess.SubprocessError):
        # Never print subprocess stderr, config values, or file contents.
        print(json.dumps({"ok": False, "error": "OPERATIONS_FAILED",
                          "action": args.command, "message": "Check service/path/space/permissions; incomplete outputs are retained."}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
