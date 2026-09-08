"""A small SQLite operation/artifact journal alongside LangGraph checkpoints.

This is not a spend ledger and cannot make remote effects exactly once. A lost
claim is uncertain until reconciled; a checkpoint retry never authorizes replay.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from contextlib import closing, contextmanager
from pathlib import Path
from uuid import uuid4

from repo2rlenv.tasksmith.models import (
    AdmissionContext,
    ArtifactRef,
    Deadline,
    Lease,
    LedgerRef,
    OperationClaim,
    OperationKey,
    OperationRecord,
    PRIdentity,
    QualityReport,
    admission_reasons,
    canonical_json,
    safe_relative,
)


class JournalConflict(RuntimeError):
    """An existing identity or effect cannot be silently replaced."""


class LeaseBusy(JournalConflict):
    pass


class LeaseLost(JournalConflict):
    pass


class ReconciliationRequired(JournalConflict):
    pass


class ArtifactCorrupt(JournalConflict):
    pass


class Journal:
    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path).absolute()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink():
            raise JournalConflict("Journal may not be a symlink")
        self.storage = self.path.parent / (self.path.stem + "-artifacts")
        self.storage.mkdir(exist_ok=True)
        if self.storage.is_symlink():
            raise JournalConflict("Artifact storage may not be a symlink")
        self.clock = clock
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS prs (
                    pr_id TEXT PRIMARY KEY, identity TEXT NOT NULL,
                    deadline TEXT NOT NULL, ledger TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS revisions (
                    pr_id TEXT NOT NULL REFERENCES prs(pr_id), revision INTEGER NOT NULL,
                    task_digest TEXT NOT NULL, parent_digest TEXT, manifest TEXT NOT NULL,
                    PRIMARY KEY(pr_id, revision), UNIQUE(pr_id, task_digest)
                );
                CREATE TABLE IF NOT EXISTS leases (
                    pr_id TEXT PRIMARY KEY REFERENCES prs(pr_id), owner TEXT NOT NULL,
                    token TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY, effect_id TEXT NOT NULL,
                    pr_id TEXT NOT NULL REFERENCES prs(pr_id), key_json TEXT NOT NULL,
                    owner_token TEXT NOT NULL, status TEXT NOT NULL, record TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS operations_effect ON operations(effect_id);
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    at REAL NOT NULL, event TEXT NOT NULL, payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
                    name TEXT NOT NULL, reference TEXT NOT NULL, temporary TEXT NOT NULL,
                    status TEXT NOT NULL,
                    PRIMARY KEY(operation_id, name)
                );
                CREATE TABLE IF NOT EXISTS selections (
                    pr_id TEXT PRIMARY KEY REFERENCES prs(pr_id), task_digest TEXT NOT NULL,
                    report TEXT NOT NULL, context TEXT NOT NULL
                );
            """)

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def register_pr(self, pr: PRIdentity, *, deadline: Deadline, ledger: LedgerRef) -> str:
        """Freeze lineage deadline/ledger once, before any attempted work."""
        ledger = ledger.model_copy(update={"path": str(Path(ledger.path).resolve())})
        values = canonical_json(pr), canonical_json(deadline), canonical_json(ledger)
        with self._transaction() as connection:
            prior = connection.execute("SELECT * FROM prs WHERE pr_id=?", (pr.key,)).fetchone()
            if prior:
                if tuple(prior[key] for key in ("identity", "deadline", "ledger")) != values:
                    raise JournalConflict(
                        "PR identity, original deadline or ledger binding changed"
                    )
            else:
                connection.execute(
                    "INSERT INTO prs VALUES (?,?,?,?,?)", (pr.key, *values, self.clock())
                )
        return pr.key

    def acquire_lease(self, pr_id: str, *, owner: str, ttl_seconds: float) -> Lease:
        """An expired deadline permits reconciliation/cleanup, but never a new effect claim."""
        now = self.clock()
        if not owner or not 0 < ttl_seconds <= 3600:
            raise ValueError("Lease needs an owner and a TTL in (0, 3600] seconds")
        with self._transaction() as connection:
            pr = self._pr(connection, pr_id)
            prior = connection.execute("SELECT * FROM leases WHERE pr_id=?", (pr_id,)).fetchone()
            if prior and prior["expires_at"] > now:
                raise LeaseBusy(f"PR already leased by {prior['owner']}")
            self._abandon(connection, pr_id, "Previous worker lease ended without reconciliation")
            lease = Lease(
                pr_id=pr_id,
                owner=owner,
                token=uuid4().hex,
                expires_at=now + ttl_seconds,
                deadline=Deadline.model_validate_json(pr["deadline"]),
            )
            connection.execute(
                "INSERT OR REPLACE INTO leases VALUES (?,?,?,?)",
                (pr_id, owner, lease.token, lease.expires_at),
            )
        return lease

    def renew_lease(self, lease: Lease, *, ttl_seconds: float) -> Lease:
        if not 0 < ttl_seconds <= 3600:
            raise ValueError("Lease TTL must be in (0, 3600] seconds")
        with self._transaction() as connection:
            self._lease(connection, lease)
            renewed = lease.model_copy(update={"expires_at": self.clock() + ttl_seconds})
            connection.execute(
                "UPDATE leases SET expires_at=? WHERE pr_id=?", (renewed.expires_at, lease.pr_id)
            )
        return renewed

    def release_lease(self, lease: Lease) -> None:
        with self._transaction() as connection:
            self._lease(connection, lease, allow_expired=True)
            self._abandon(
                connection, lease.pr_id, "Worker released lease with an unfinished effect"
            )
            connection.execute("DELETE FROM leases WHERE pr_id=?", (lease.pr_id,))

    def _pr(self, connection, pr_id):
        row = connection.execute("SELECT * FROM prs WHERE pr_id=?", (pr_id,)).fetchone()
        if row is None:
            raise JournalConflict("PR must be registered before work")
        return row

    def _lease(self, connection, lease, *, allow_expired=False):
        row = connection.execute("SELECT * FROM leases WHERE pr_id=?", (lease.pr_id,)).fetchone()
        if (
            not row
            or row["token"] != lease.token
            or row["owner"] != lease.owner
            or (not allow_expired and row["expires_at"] <= self.clock())
        ):
            raise LeaseLost("Worker no longer owns the PR lease")
        return self._pr(connection, lease.pr_id)

    def _abandon(self, connection, pr_id, reason):
        rows = connection.execute(
            "SELECT * FROM operations WHERE pr_id=? AND status IN ('claimed','submitted')", (pr_id,)
        ).fetchall()
        for row in rows:
            record = OperationRecord.model_validate_json(row["record"])
            record.status, record.error = "uncertain", reason
            self._save_operation(connection, record, "lease_ended")

    def register_revision(
        self,
        lease: Lease,
        *,
        revision: int,
        task_digest: str,
        parent_digest: str | None,
        manifest: dict,
    ) -> None:
        """Append one exact revision; an earlier revision is never counted as another PR."""
        # Reuse the key's digest validation without inventing a second hash grammar.
        OperationKey(
            pr_id=lease.pr_id,
            revision_digest=task_digest,
            stage="revision",
            input_digest=task_digest,
            policy_digest=task_digest,
            attempt=0,
            effect="register",
        )
        if type(revision) is not int or revision < 0 or not manifest:
            raise ValueError("Revision needs a nonnegative integer and validated manifest")
        encoded = canonical_json(manifest)
        with self._transaction() as connection:
            self._lease(connection, lease)
            row = connection.execute(
                "SELECT * FROM revisions WHERE pr_id=? AND revision=?", (lease.pr_id, revision)
            ).fetchone()
            if row:
                if (row["task_digest"], row["parent_digest"], row["manifest"]) != (
                    task_digest,
                    parent_digest,
                    encoded,
                ):
                    raise JournalConflict("Revision digest, parent or manifest changed")
                return
            if connection.execute(
                "SELECT 1 FROM selections WHERE pr_id=?", (lease.pr_id,)
            ).fetchone():
                raise JournalConflict("PR already has a selected revision")
            latest = connection.execute(
                "SELECT * FROM revisions WHERE pr_id=? ORDER BY revision DESC LIMIT 1",
                (lease.pr_id,),
            ).fetchone()
            expected = (latest["revision"] + 1, latest["task_digest"]) if latest else (0, None)
            if (revision, parent_digest) != expected:
                raise JournalConflict("Revision must extend the current exact parent")
            if latest and task_digest == latest["task_digest"]:
                raise JournalConflict("Unchanged package is not a new revision")
            if connection.execute(
                "SELECT 1 FROM revisions WHERE pr_id=? AND task_digest=?",
                (lease.pr_id, task_digest),
            ).fetchone():
                raise JournalConflict("This exact package already belongs to a retained revision")
            connection.execute(
                "INSERT INTO revisions VALUES (?,?,?,?,?)",
                (lease.pr_id, revision, task_digest, parent_digest, encoded),
            )

    def _current_revision(self, connection, pr_id, task_digest):
        latest = connection.execute(
            "SELECT task_digest FROM revisions WHERE pr_id=? ORDER BY revision DESC LIMIT 1",
            (pr_id,),
        ).fetchone()
        if not latest or latest["task_digest"] != task_digest:
            raise JournalConflict("Operation evidence is not for the current frozen revision")

    def claim_operation(
        self, lease: Lease, key: OperationKey, *, retry_of: str | None = None
    ) -> OperationClaim:
        with self._transaction() as connection:
            pr = self._lease(connection, lease)
            if key.pr_id != lease.pr_id:
                raise JournalConflict("Operation belongs to another PR")
            self._current_revision(connection, lease.pr_id, key.revision_digest)
            prior = connection.execute(
                "SELECT * FROM operations WHERE operation_id=?", (key.operation_id,)
            ).fetchone()
            if prior:
                previous_record = OperationRecord.model_validate_json(prior["record"])
                if previous_record.status == "completed":
                    for reference in previous_record.artifacts.values():
                        self.verify_artifact(reference)
                return OperationClaim(operation=previous_record, acquired=False)
            deadline = Deadline.model_validate_json(pr["deadline"])
            deadline.require_remaining(now=self.clock())
            previous = connection.execute(
                "SELECT record FROM operations WHERE effect_id=?", (key.effect_id,)
            ).fetchall()
            records = [OperationRecord.model_validate_json(row["record"]) for row in previous]
            if records:
                last = max(records, key=lambda record: record.key.attempt)
                if any(
                    record.status in {"claimed", "submitted", "uncertain"} for record in records
                ):
                    raise ReconciliationRequired(
                        "Reconcile the prior effect before attempting another"
                    )
                if (
                    last.status != "failed"
                    or retry_of != last.key.operation_id
                    or key.attempt != last.key.attempt + 1
                ):
                    raise JournalConflict(
                        "Retry must explicitly reference the preceding failed effect; completed reviews are not rerolled"
                    )
            elif retry_of is not None or key.attempt != 0:
                raise JournalConflict(
                    "The first effect attempt is zero and cannot reference a retry"
                )
            record = OperationRecord(
                key=key,
                status="claimed",
                ledger=LedgerRef.model_validate_json(pr["ledger"]),
                deadline=deadline,
            )
            connection.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?)",
                (
                    key.operation_id,
                    key.effect_id,
                    key.pr_id,
                    canonical_json(key),
                    lease.token,
                    record.status,
                    canonical_json(record),
                ),
            )
            self._event(connection, key.operation_id, "claimed", record.model_dump(mode="json"))
            return OperationClaim(operation=record, acquired=True)

    def _operation(self, connection, operation_id):
        row = connection.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise JournalConflict("Unknown operation")
        return row, OperationRecord.model_validate_json(row["record"])

    def _event(self, connection, operation_id, event, payload):
        connection.execute(
            "INSERT INTO events(operation_id,at,event,payload) VALUES (?,?,?,?)",
            (operation_id, self.clock(), event, canonical_json(payload)),
        )

    def _save_operation(self, connection, record, event):
        connection.execute(
            "UPDATE operations SET status=?, record=? WHERE operation_id=?",
            (record.status, canonical_json(record), record.key.operation_id),
        )
        self._event(connection, record.key.operation_id, event, record.model_dump(mode="json"))

    def record_submitted(
        self,
        lease: Lease,
        operation_id: str,
        *,
        external_id: str,
        receipt: dict,
        reservation_ids: list[str] = (),
    ) -> OperationRecord:
        """Retain late provider IDs even after lease loss; their outcome stays uncertain."""
        if not external_id:
            raise ValueError("Submitted effect needs its provider/request ID")
        with self._transaction() as connection:
            row, record = self._operation(connection, operation_id)
            if row["owner_token"] != lease.token or record.key.pr_id != lease.pr_id:
                raise LeaseLost("Only the originating claim may attach its provider ID")
            if record.external_id is not None and record.external_id != external_id:
                raise JournalConflict("Provider identity cannot change")
            if record.status in {"completed", "failed"}:
                if record.external_id == external_id:
                    return record
                raise JournalConflict("Terminal operation cannot be resubmitted")
            try:
                self._lease(connection, lease)
            except LeaseLost:
                record.status, record.error = (
                    "uncertain",
                    "Provider ID returned after worker lease loss",
                )
            else:
                if record.status == "claimed":
                    record.status = "submitted"
            record.external_id = external_id
            record.reservation_ids = sorted(set(record.reservation_ids) | set(reservation_ids))
            record.receipt = dict(receipt)
            self._save_operation(connection, record, "submitted_receipt")
            return record

    def mark_uncertain(
        self, lease: Lease, operation_id: str, *, reason: str, receipt: dict | None = None
    ) -> OperationRecord:
        with self._transaction() as connection:
            row, record = self._operation(connection, operation_id)
            if record.key.pr_id != lease.pr_id:
                raise LeaseLost("Operation belongs to another PR")
            if row["owner_token"] != lease.token:
                self._lease(connection, lease)
            if record.status in {"completed", "failed"}:
                raise JournalConflict("Terminal operation cannot become uncertain")
            record.status, record.error = "uncertain", reason
            if receipt is not None:
                record.receipt = dict(receipt)
            self._save_operation(connection, record, "uncertain")
            return record

    def finish_operation(
        self,
        lease: Lease,
        operation_id: str,
        *,
        receipt: dict,
        failed: bool = False,
        error: str | None = None,
    ) -> OperationRecord:
        return self._finish(
            lease, operation_id, receipt=receipt, failed=failed, error=error, reconcile=False
        )

    def reconcile_operation(
        self,
        lease: Lease,
        operation_id: str,
        *,
        receipt: dict,
        failed: bool = False,
        error: str | None = None,
    ) -> OperationRecord:
        """Caller must have independently established the existing effect's actual outcome."""
        if not receipt:
            raise ValueError("Reconciliation needs an observed outcome receipt")
        return self._finish(
            lease, operation_id, receipt=receipt, failed=failed, error=error, reconcile=True
        )

    def _finish(self, lease, operation_id, *, receipt, failed, error, reconcile):
        with self._transaction() as connection:
            self._lease(connection, lease)
            row, record = self._operation(connection, operation_id)
            if record.key.pr_id != lease.pr_id:
                raise JournalConflict("Operation belongs to another PR")
            status = "failed" if failed else "completed"
            if record.status in {"completed", "failed"}:
                if (record.status, record.receipt, record.error) != (status, receipt, error):
                    raise JournalConflict("Terminal effect receipt is immutable")
                for reference in record.artifacts.values():
                    self.verify_artifact(reference)
                return record
            if not reconcile and (
                row["owner_token"] != lease.token or record.status == "uncertain"
            ):
                raise ReconciliationRequired(
                    "Lost/uncertain effects require explicit reconciliation"
                )
            if failed and not error:
                raise ValueError("Failed operation requires a causal diagnostic")
            artifacts = connection.execute(
                "SELECT * FROM artifacts WHERE operation_id=?", (operation_id,)
            ).fetchall()
            for artifact in artifacts:
                if artifact["status"] != "committed":
                    raise ArtifactCorrupt(
                        "Recover pending artifact commits before finishing the operation"
                    )
                reference = ArtifactRef.model_validate_json(artifact["reference"])
                self.verify_artifact(reference)
                record.artifacts[artifact["name"]] = reference
            record.status, record.receipt, record.error = status, dict(receipt), error
            self._save_operation(connection, record, "reconciled" if reconcile else "finished")
            return record

    def get_operation(self, operation_id: str) -> OperationRecord:
        with closing(self._connect()) as connection:
            return self._operation(connection, operation_id)[1]

    def operations(self, pr_id: str) -> list[OperationRecord]:
        with closing(self._connect()) as connection:
            return [
                OperationRecord.model_validate_json(row["record"])
                for row in connection.execute(
                    "SELECT record FROM operations WHERE pr_id=? ORDER BY rowid", (pr_id,)
                )
            ]

    def events(self, operation_id: str) -> list[dict]:
        with closing(self._connect()) as connection:
            return [
                dict(row) | {"payload": json.loads(row["payload"])}
                for row in connection.execute(
                    "SELECT * FROM events WHERE operation_id=? ORDER BY sequence", (operation_id,)
                )
            ]

    def _artifact_path(self, relative: str) -> Path:
        safe_relative(relative)
        path = self.storage / relative
        for part in [path, *path.parents]:
            if part == self.storage.parent:
                break
            if part.is_symlink():
                raise ArtifactCorrupt("Artifact paths may not contain symlinks")
        return path

    def verify_artifact(self, reference: ArtifactRef) -> Path:
        expected_path = f"objects/{reference.sha256[:2]}/{reference.sha256}"
        if reference.path != expected_path:
            raise ArtifactCorrupt("Artifact path is not its content-derived identity")
        path = self._artifact_path(reference.path)
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != reference.size_bytes:
                    raise ArtifactCorrupt("Artifact size/type differs from its receipt")
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
        except OSError as exc:
            raise ArtifactCorrupt(f"Artifact unavailable: {reference.path}") from exc
        if actual != reference.sha256:
            raise ArtifactCorrupt("Artifact content hash differs from its receipt")
        return path

    @staticmethod
    def _fsync_directory(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _publish_blob(self, reference, temporary):
        destination = self._artifact_path(reference.path)
        if destination.exists():
            self.verify_artifact(reference)
            return
        temporary = self._artifact_path(temporary)
        if (
            not temporary.is_file()
            or temporary.stat().st_size != reference.size_bytes
            or hashlib.sha256(temporary.read_bytes()).hexdigest() != reference.sha256
        ):
            raise ArtifactCorrupt("Pending artifact bytes are missing or incomplete")
        destination.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_path(reference.path)
        try:
            os.link(
                temporary, destination
            )  # Exclusive publication: never overwrite a bucket/blob key.
        except FileExistsError:
            self.verify_artifact(reference)
        self._fsync_directory(destination.parent)
        self._fsync_directory(destination.parent.parent)
        self._fsync_directory(self.storage)

    def commit_artifact(
        self,
        lease: Lease,
        operation_id: str,
        *,
        name: str,
        data: bytes,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        if not name or not isinstance(data, bytes) or len(data) > 64 * 1024 * 1024:
            raise ValueError("Artifact needs a name and bounded bytes (maximum 64 MiB)")
        sha = hashlib.sha256(data).hexdigest()
        reference = ArtifactRef(
            sha256=sha, size_bytes=len(data), path=f"objects/{sha[:2]}/{sha}", media_type=media_type
        )
        with self._transaction() as connection:
            self._lease(connection, lease)
            _, record = self._operation(connection, operation_id)
            if record.key.pr_id != lease.pr_id:
                raise JournalConflict("Artifact belongs to another PR")
            prior = connection.execute(
                "SELECT * FROM artifacts WHERE operation_id=? AND name=?", (operation_id, name)
            ).fetchone()
            if prior:
                if ArtifactRef.model_validate_json(prior["reference"]) != reference:
                    raise JournalConflict(
                        "Committed artifact name cannot change content or media type"
                    )
                if prior["status"] == "committed":
                    self.verify_artifact(reference)
                    return reference
                temporary = prior["temporary"]
            else:
                if record.status in {"completed", "failed"}:
                    raise JournalConflict("Terminal operation artifacts are immutable")
                temporary = f"pending/{uuid4().hex}"
                connection.execute(
                    "INSERT INTO artifacts VALUES (?,?,?,?,?)",
                    (operation_id, name, canonical_json(reference), temporary, "staging"),
                )
                self._event(
                    connection,
                    operation_id,
                    "artifact_staged",
                    {"name": name, "reference": reference.model_dump()},
                )
        path = self._artifact_path(temporary)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._artifact_path(temporary)
        try:
            with path.open("xb") as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            if path.read_bytes() != data:
                raise ArtifactCorrupt(
                    "Pending artifact write was interrupted; retain it for diagnosis"
                ) from None
        self._publish_blob(reference, temporary)
        self._complete_artifact(operation_id, name, reference, temporary)
        return reference

    def _complete_artifact(self, operation_id, name, reference, temporary):
        self.verify_artifact(reference)
        with self._transaction() as connection:
            connection.execute(
                "UPDATE artifacts SET status='committed' WHERE operation_id=? AND name=?",
                (operation_id, name),
            )
            self._event(
                connection,
                operation_id,
                "artifact_committed",
                {"name": name, "reference": reference.model_dump()},
            )
        self._artifact_path(temporary).unlink(missing_ok=True)

    def recover_artifacts(self, lease: Lease) -> list[ArtifactRef]:
        """Finish only existing hash-bound write intents, never regenerate stage outputs."""
        with self._transaction() as connection:
            self._lease(connection, lease)
            rows = connection.execute(
                "SELECT artifacts.* FROM artifacts JOIN operations USING(operation_id) WHERE operations.pr_id=? AND artifacts.status='staging'",
                (lease.pr_id,),
            ).fetchall()
        recovered = []
        for row in rows:
            reference = ArtifactRef.model_validate_json(row["reference"])
            self._publish_blob(reference, row["temporary"])
            self._complete_artifact(row["operation_id"], row["name"], reference, row["temporary"])
            recovered.append(reference)
        return recovered

    def select_revision(
        self, lease: Lease, *, report: QualityReport, context: AdmissionContext
    ) -> bool:
        """At most one accepted revision per canonical PR, with exact admission receipts."""
        reasons = admission_reasons(report, context)
        if reasons or context.pr.key != lease.pr_id:
            raise JournalConflict(
                "Revision is not admissible: " + "; ".join(reasons or ["wrong PR"])
            )
        for evidence in context.evidence.values():
            self.verify_artifact(evidence.artifact)
        with self._transaction() as connection:
            self._lease(connection, lease)
            self._current_revision(connection, lease.pr_id, report.revision_digest)
            encoded = canonical_json(report), canonical_json(context)
            row = connection.execute(
                "SELECT * FROM selections WHERE pr_id=?", (lease.pr_id,)
            ).fetchone()
            if row:
                if (row["task_digest"], row["report"], row["context"]) != (
                    report.revision_digest,
                    *encoded,
                ):
                    raise JournalConflict("Selected revision or its admission receipts changed")
                return False
            connection.execute(
                "INSERT INTO selections VALUES (?,?,?,?)",
                (lease.pr_id, report.revision_digest, *encoded),
            )
            return True

    def counts(self) -> dict[str, int]:
        with closing(self._connect()) as connection:
            return {
                "attempted_prs": connection.execute("SELECT COUNT(*) FROM prs").fetchone()[0],
                "revisions": connection.execute("SELECT COUNT(*) FROM revisions").fetchone()[0],
                "selected_prs": connection.execute("SELECT COUNT(*) FROM selections").fetchone()[0],
            }
