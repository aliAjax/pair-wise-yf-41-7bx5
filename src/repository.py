import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, InvalidTransition, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    updated_by TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS entity_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_entity_versions_entity
                    ON entity_versions(entity_id, version);
                CREATE TABLE IF NOT EXISTS revisions (
                    id TEXT PRIMARY KEY,
                    event_id TEXT NOT NULL,
                    revision_no INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    data TEXT NOT NULL,
                    event_version INTEGER NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(event_id, revision_no)
                );
                CREATE INDEX IF NOT EXISTS idx_revisions_event
                    ON revisions(event_id, revision_no);
                CREATE INDEX IF NOT EXISTS idx_revisions_status
                    ON revisions(event_id, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(entities)")
            }
            if "updated_by" not in columns:
                connection.execute("ALTER TABLE entities ADD COLUMN updated_by TEXT")
            connection.execute(
                """
                INSERT OR IGNORE INTO entity_versions(
                    entity_id, version, kind, status, data, created_by, updated_by,
                    created_at, updated_at
                )
                SELECT id, version, kind, status, data, created_by, updated_by,
                       created_at, updated_at
                FROM entities
                """
            )

    @staticmethod
    def _decode(value):
        return json.loads(value)

    @classmethod
    def _entity_from_row(cls, row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": cls._decode(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }

    @classmethod
    def _version_from_row(cls, row):
        return {
            "entity_id": row["entity_id"],
            "version": int(row["version"]),
            "kind": row["kind"],
            "status": row["status"],
            "data": cls._decode(row["data"]),
            "created_by": row["created_by"],
            "updated_by": row["updated_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @classmethod
    def _revision_from_row(cls, row):
        return {
            "id": row["id"],
            "kind": "revision",
            "event_id": row["event_id"],
            "revision_no": int(row["revision_no"]),
            "status": row["status"],
            "data": cls._decode(row["data"]),
            "event_version": int(row["event_version"]),
            "reviewed_by": row["reviewed_by"],
            "reviewed_at": row["reviewed_at"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO entities(
                    id, kind, status, version, data, created_by, created_at,
                    updated_at, updated_by
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)
                """,
                (entity_id, kind, status, payload, actor_id, now, now, actor_id),
            )
            connection.execute(
                """
                INSERT INTO entity_versions(
                    entity_id, version, kind, status, data, created_by, updated_by,
                    created_at, updated_at
                ) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (entity_id, kind, status, payload, actor_id, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data, actor_id=None):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            next_version = current_version + 1
            created_by = row["created_by"]
            created_at = row["created_at"]
            kind = row["kind"]
            connection.execute(
                """
                UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ?,
                                    updated_by = ?
                WHERE id = ? AND version = ?
                """,
                (status, next_version, payload, now, actor_id, entity_id, current_version),
            )
            connection.execute(
                """
                INSERT INTO entity_versions(
                    entity_id, version, kind, status, data, created_by, updated_by,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    next_version,
                    kind,
                    status,
                    payload,
                    created_by,
                    actor_id,
                    created_at,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def list_entity_versions(self, entity_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entity_versions WHERE entity_id = ? ORDER BY version",
                (entity_id,),
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def create_revision(self, event_id, expected_event_version, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            event_row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'event'",
                (event_id,),
            ).fetchone()
            if not event_row:
                raise NotFoundError("event not found: " + event_id)
            current_version = int(event_row["version"])
            if expected_event_version is not None and current_version != int(expected_event_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_event_version, current_version)
                )
            pending = connection.execute(
                "SELECT id FROM revisions WHERE event_id = ? AND status = 'pending_review'",
                (event_id,),
            ).fetchone()
            if pending:
                raise ConflictError(
                    "a pending revision already exists",
                    {"revision_id": pending["id"]},
                )
            next_no = int(
                connection.execute(
                    "SELECT COALESCE(MAX(revision_no), 0) + 1 AS next_no FROM revisions WHERE event_id = ?",
                    (event_id,),
                ).fetchone()["next_no"]
            )
            revision_id = "%s-R%03d" % (event_id, next_no)
            connection.execute(
                """
                INSERT INTO revisions(
                    id, event_id, revision_no, status, data, event_version,
                    created_by, created_at, updated_at
                ) VALUES (?, ?, ?, 'pending_review', ?, ?, ?, ?, ?)
                """,
                (revision_id, event_id, next_no, payload, current_version, actor_id, now, now),
            )
            connection.commit()
        except ConflictError:
            connection.rollback()
            raise
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_revision(revision_id)

    def get_revision(self, revision_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM revisions WHERE id = ?", (revision_id,)
            ).fetchone()
        return self._revision_from_row(row) if row else None

    def list_revisions(self, event_id=None, status=None):
        clauses = []
        params = []
        if event_id:
            clauses.append("event_id = ?")
            params.append(event_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM revisions" + where + " ORDER BY event_id, revision_no",
                params,
            ).fetchall()
        return [self._revision_from_row(row) for row in rows]

    def get_pending_revision(self, event_id):
        revisions = self.list_revisions(event_id=event_id, status="pending_review")
        return revisions[0] if revisions else None

    def decide_revision(self, revision_id, action, data, reviewer_id, apply_event_update):
        next_status = "approved" if action == "approve" else "rejected"
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            revision_row = connection.execute(
                "SELECT * FROM revisions WHERE id = ?", (revision_id,)
            ).fetchone()
            if not revision_row:
                raise NotFoundError("revision not found: " + revision_id)
            if revision_row["status"] != "pending_review":
                raise InvalidTransition(
                    "cannot %s revision from status %s"
                    % (action, revision_row["status"])
                )
            event_row = connection.execute(
                "SELECT * FROM entities WHERE id = ? AND kind = 'event'",
                (revision_row["event_id"],),
            ).fetchone()
            if not event_row:
                raise NotFoundError("event not found: " + revision_row["event_id"])

            updated_event = None
            current_event = self._entity_from_row(event_row)
            revision = self._revision_from_row(revision_row)
            if action == "approve":
                next_event_status, event_data = apply_event_update(revision, current_event, data)
                event_payload = json.dumps(event_data, ensure_ascii=False, sort_keys=True)
                next_version = int(event_row["version"]) + 1
                connection.execute(
                    """
                    UPDATE entities SET status = ?, version = ?, data = ?, updated_at = ?,
                                        updated_by = ?
                    WHERE id = ? AND version = ?
                    """,
                    (
                        next_event_status,
                        next_version,
                        event_payload,
                        now,
                        reviewer_id,
                        event_row["id"],
                        event_row["version"],
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO entity_versions(
                        entity_id, version, kind, status, data, created_by, updated_by,
                        created_at, updated_at
                    ) VALUES (?, ?, 'event', ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_row["id"],
                        next_version,
                        next_event_status,
                        event_payload,
                        event_row["created_by"],
                        reviewer_id,
                        event_row["created_at"],
                        now,
                    ),
                )
                updated_event = dict(current_event)
                updated_event.update(
                    {
                        "status": next_event_status,
                        "version": next_version,
                        "data": event_data,
                        "updated_at": now,
                        "updated_by": reviewer_id,
                    }
                )

            merged_revision_data = dict(revision["data"])
            merged_revision_data.update(data or {})
            merged_revision_data["decision"] = action
            merged_revision_data["decided_by"] = reviewer_id
            merged_revision_data["decided_at"] = now
            connection.execute(
                """
                UPDATE revisions SET status = ?, data = ?, reviewed_by = ?,
                                    reviewed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'pending_review'
                """,
                (
                    next_status,
                    json.dumps(merged_revision_data, ensure_ascii=False, sort_keys=True),
                    reviewer_id if action == "approve" else reviewer_id,
                    now,
                    now,
                    revision_id,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_revision(revision_id), updated_event

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO audit_log(
                    entity_id, actor_id, actor_role, action, from_status, to_status,
                    detail, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True
