"""Governed fixture access and atomic local drafts, using trusted resource policies."""

import copy
import json
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path

from .contracts import validate_data
from .errors import ConfirmationRequired, ExecutionError, PermissionDenied
from .expressions import evaluate
from .paths import project_data


class Gateway:
    def __init__(self, grants, db_path=":memory:", data_dir=None, subject="demo-user"):
        self.grants = set(grants)
        self.subject = subject
        root = project_data()
        self.policy = json.loads((root / "specs" / "resources.json").read_text())
        examples = Path(data_dir) if data_dir else root / "examples"
        self.resources = {}
        for domain in ("food", "finance"):
            for path in sorted((examples / domain).glob("*.json")):
                self.resources[path.stem] = json.loads(path.read_text())
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(db_path))
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS records (id TEXT PRIMARY KEY, resource TEXT NOT NULL, body TEXT NOT NULL)"
        )
        self.connection.commit()
        self.audit = []
        self._in_transaction = False

    def close(self):
        self.connection.close()

    def _authorize(self, manifest, resource, action):
        policy = self.policy.get(resource, {})
        accepted = set(policy.get(f"{action}_permissions", []))
        allowed = (
            resource in manifest["resources"]
            and manifest["domain"] == policy.get("domain")
            and bool(accepted & self.grants & set(manifest["permissions"]))
        )
        self.audit.append({
            "plugin": manifest["id"], "version": manifest["version"],
            "subject": self.subject, "resource": resource, "action": action,
            "decision": "allow" if allowed else "deny",
        })
        if not allowed:
            raise PermissionDenied(f"{manifest['id']}: {action} {resource} 未获授权（领域、声明与用户权限必须同时满足）")
        return policy

    def authorize_manifest(self, manifest):
        missing = set(manifest["permissions"]) - self.grants
        if missing:
            self.audit.append({"plugin": manifest["id"], "action": "invoke", "decision": "deny", "missing": sorted(missing)})
            raise PermissionDenied(f"{manifest['id']}: 缺少权限 {', '.join(sorted(missing))}")

    def read(self, manifest, resource):
        policy = self._authorize(manifest, resource, "read")
        rows = copy.deepcopy(self.resources.get(resource, []))
        rows.extend(json.loads(row[0]) for row in self.connection.execute(
            "SELECT body FROM records WHERE resource=? ORDER BY rowid", (resource,)
        ))
        return [row for row in rows if not policy.get("private") or row.get("owner_id") == self.subject]

    @contextmanager
    def transaction(self):
        if self._in_transaction:
            raise ExecutionError("Nested transactions are not supported")
        self.connection.execute("BEGIN IMMEDIATE")
        self._in_transaction = True
        try:
            yield
            self.connection.commit()
            self.audit.append({"action": "transaction", "decision": "commit"})
        except BaseException:
            self.connection.rollback()
            self.audit.append({"action": "transaction", "decision": "rollback"})
            raise
        finally:
            self._in_transaction = False

    def insert(self, manifest, resource, values, inputs, confirmed=False):
        policy = self._authorize(manifest, resource, "write")
        if not confirmed:
            raise ConfirmationRequired("生成草稿需要明确授权；当前仅完成只读预览")
        if not self._in_transaction:
            raise ExecutionError("Writes require a runtime transaction")
        validate_data(values, policy["write_schema"], f"resource {resource}")
        resources = {name: self.read(manifest, name) for name in policy.get("check_resources", [])}
        context = {"row": values, "input": inputs, "resources": resources}
        for check in policy.get("checks", []):
            if evaluate(check["condition"], context) is not True:
                raise ExecutionError(check["message"])
        row = {**values, "id": str(uuid.uuid4()), "owner_id": self.subject}
        self.connection.execute(
            "INSERT INTO records(id,resource,body) VALUES (?,?,?)",
            (row["id"], resource, json.dumps(row, ensure_ascii=False, allow_nan=False)),
        )
        return row

    def draft_count(self):
        return self.connection.execute(
            "SELECT COUNT(*) FROM records WHERE resource='food_order_drafts'"
        ).fetchone()[0]
