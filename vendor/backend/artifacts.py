"""Immutable text artifacts and explicit, read-only cross-agent handoffs.

Untrusted workspace paths are traversed through directory descriptors.  Artifact
content is snapshotted at registration; granting it never reads another agent's
live workspace.  The runtime must mount ``imports/<agent>/<run>`` read-only and
must authorize participants before calling ``grant``.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import threading
import time
import uuid

from .agents import AGENTS


class ArtifactError(ValueError):
    """An invalid path, unsafe file, exhausted quota, or invalid handoff."""


_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,96}\Z")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TEXT_TYPES = {
    ".txt": "text/plain", ".md": "text/markdown", ".rst": "text/plain",
    ".py": "text/x-python", ".json": "application/json",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values",
    ".yaml": "text/yaml", ".yml": "text/yaml", ".toml": "text/plain",
    ".ini": "text/plain", ".cfg": "text/plain", ".log": "text/plain",
    ".sql": "text/plain", ".js": "text/javascript", ".mjs": "text/javascript",
    ".cjs": "text/javascript", ".ts": "text/plain", ".tsx": "text/plain",
    ".jsx": "text/plain", ".css": "text/css", ".html": "text/html",
    ".xml": "application/xml", ".sh": "text/plain", ".bash": "text/plain",
    ".zsh": "text/plain", ".c": "text/plain", ".h": "text/plain",
    ".cpp": "text/plain", ".hpp": "text/plain", ".java": "text/plain",
    ".rs": "text/plain", ".go": "text/plain", ".rb": "text/plain",
    ".r": "text/plain", ".tex": "text/plain",
}
_MAX_BYTES = 256 * 1024
_MAX_RUN_BYTES = 4 * 1024 * 1024
_MAX_RUN_COUNT = 40
_INDEX = "artifacts.json"
_TERMINAL_RUNS = {"done", "partial", "failed", "cancelled", "skipped", "rejected"}
_SENSITIVE_NAME = re.compile(
    r"(?:^|[-_.])(?:credentials?|secrets?|passwords?|tokens?|api[-_]?keys?|id_rsa|id_ed25519)(?:[-_.]|$)", re.I)
_SENSITIVE_CONTENT = re.compile(
    r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----|"
    r"(?im:^\s*(?:export\s+)?(?:[A-Z_]*(?:API_KEY|ACCESS_TOKEN|PRIVATE_KEY|PASSWORD|SECRET|GIT_TOKEN))\s*=\s*\S+)|"
    r'''["'](?:api_key|access_token|private_key|password|client_secret|git_token)["']\s*:\s*["'][^"']+["']''', re.I)


def _identifier(value, label):
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ArtifactError(f"{label} 必须是 1–96 位字母、数字、下划线或连字符。")
    return value


def _agent(value, agents=None):
    _identifier(value, "Agent ID")
    if value not in (AGENTS if agents is None else agents):
        raise ArtifactError("未知 Agent。")
    return value


def _authorizer(value):
    """Use one grammar when writing and restoring audited handoff identities."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_:-]{1,192}", value):
        raise ArtifactError("授权者 ID 必须是有界的 Agent 或 orchestrator:task-id 标识。")
    return value


def _task(value):
    if not isinstance(value, str) or not value or len(value) > 192 or any(ord(c) < 32 for c in value):
        raise ArtifactError("无效任务 ID。")
    return value


def _safe_parts(path):
    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise ArtifactError("无效产物路径。")
    parts = raw.split("/")
    if any(part.startswith(".") or part == "" for part in parts):
        raise ArtifactError("产物路径不能包含隐藏目录、隐藏文件、空段或上级目录。")
    if any(any(ord(c) < 32 or ord(c) == 127 for c in part) for part in parts):
        raise ArtifactError("产物路径不能包含控制字符。")
    return parts


def _regular_file(fd):
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ArtifactError("只允许普通文件；禁止符号链接、硬链接及特殊文件。")
    return info


def _read_file(fd, limit=None):
    before = _regular_file(fd)
    if limit is not None and before.st_size > limit:
        raise ArtifactError("单个产物不能超过 256 KiB。")
    chunks = []
    total = 0
    while True:
        chunk = os.read(fd, min(65536, limit + 1 - total) if limit is not None else 65536)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if limit is not None and total > limit:
            raise ArtifactError("单个产物不能超过 256 KiB。")
    after = _regular_file(fd)
    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ArtifactError("文件在读取期间发生变化，请重新注册。")
    return b"".join(chunks)


def _text(raw):
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ArtifactError("产物必须是 UTF-8 文本。") from exc
    if _CONTROL.search(text):
        raise ArtifactError("产物包含二进制或不支持的控制字符。")
    return text


class ArtifactStore:
    def __init__(self, data_dir: Path, agents=None):
        self.agents = copy.deepcopy(AGENTS if agents is None else agents)
        if not isinstance(self.agents, dict) or not self.agents:
            raise ArtifactError("需要非空的 Agent 注册表。")
        for agent_id in self.agents:
            _identifier(agent_id, "Agent ID")
        requested = Path(data_dir).absolute()
        if requested.is_symlink():
            raise ArtifactError("数据目录不能是符号链接。")
        self.data_dir = requested.resolve()
        self.data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self._artifacts = {}
        self._grants = []
        self._load()

    @contextmanager
    def _directory(self, parts=(), create=False):
        """Open only real directories below the configured, trusted data root."""
        fd = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
            fd = os.open(self.data_dir, flags)
            for part in parts:
                if create:
                    try:
                        os.mkdir(part, 0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                child = os.open(part, flags, dir_fd=fd)
                os.close(fd)
                fd = child
            yield fd
        except OSError as exc:
            raise ArtifactError("目录不存在、不安全或无法访问。") from exc
        finally:
            if fd is not None:
                os.close(fd)

    @staticmethod
    def _open_read(parent_fd, name):
        # NONBLOCK prevents a malicious FIFO from blocking before fstat rejects it.
        return os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)

    def _load(self):
        with self.lock, self._directory() as root_fd:
            try:
                fd = self._open_read(root_fd, _INDEX)
            except FileNotFoundError:
                return
            except OSError as exc:
                raise ArtifactError("无法安全读取产物索引。") from exc
            try:
                payload = json.loads(_read_file(fd).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ArtifactError("产物索引已损坏。") from exc
            finally:
                os.close(fd)
            if (not isinstance(payload, dict) or payload.get("version") != 1
                    or not isinstance(payload.get("artifacts"), list)
                    or not isinstance(payload.get("grants", []), list)):
                raise ArtifactError("产物索引格式无效。")
            run_totals = {}
            for item in payload["artifacts"]:
                if not isinstance(item, dict):
                    raise ArtifactError("产物索引记录无效。")
                artifact_id = _identifier(item.get("id"), "产物 ID")
                _agent(item.get("ownerAgentId"), self.agents)
                run_id = _identifier(item.get("runId"), "Run ID")
                _task(item.get("taskId"))
                parts = _safe_parts(item.get("path", ""))
                if item.get("name") != parts[-1] or Path(parts[-1]).suffix.lower() not in _TEXT_TYPES:
                    raise ArtifactError("产物索引包含无效文件名。")
                content = item.get("content")
                if not isinstance(content, str):
                    raise ArtifactError("产物索引缺少文本快照。")
                raw = content.encode("utf-8")
                if len(raw) > _MAX_BYTES or _CONTROL.search(content):
                    raise ArtifactError("产物索引包含无效文本快照。")
                if item.get("sha256") != hashlib.sha256(raw).hexdigest():
                    raise ArtifactError("产物快照校验失败。")
                if artifact_id in self._artifacts:
                    raise ArtifactError("产物 ID 重复。")
                item["size"] = len(raw)
                item["mimeType"] = _TEXT_TYPES[Path(parts[-1]).suffix.lower()]
                count, size = run_totals.get(run_id, (0, 0))
                run_totals[run_id] = (count + 1, size + len(raw))
                if count + 1 > _MAX_RUN_COUNT or size + len(raw) > _MAX_RUN_BYTES:
                    raise ArtifactError("产物索引超过每次运行的配额。")
                self._artifacts[artifact_id] = item
            for item in self._artifacts.values():
                if not item.get("carriedForward"):
                    continue
                source = self._artifacts.get(_identifier(item.get("sourceArtifactId"), "源产物 ID"))
                origin = self._artifacts.get(_identifier(item.get("originArtifactId"), "原始产物 ID"))
                _identifier(item.get("teamId"), "团队 ID")
                if (source is None or origin is None or source["id"] == item["id"]
                        or item.get("sourceRunId") != source["runId"] or source["runId"] == item["runId"]
                        or item.get("originRunId") != origin["runId"] or item.get("channelId") != "group"
                        or item.get("sourceTaskId") != source["taskId"]
                        or item.get("sourceSha256") != source["sha256"] or item["sha256"] != source["sha256"]
                        or item["sha256"] != origin["sha256"] or item["ownerAgentId"] != source["ownerAgentId"]
                        or item.get("inheritedReadOnly") is not True):
                    raise ArtifactError("继承产物的来源链或只读属性无效。")
            for grant in payload.get("grants", []):
                if not isinstance(grant, dict) or grant.get("id") not in self._artifacts:
                    raise ArtifactError("共享记录格式无效。")
                original = self._artifacts[grant["id"]]
                _agent(grant.get("toAgentId"), self.agents)
                _authorizer(grant.get("authorizedBy"))
                if (grant.get("runId") != original["runId"] or
                        grant.get("path") != original["id"] + "/" + original["name"]):
                    raise ArtifactError("共享记录越过运行边界。")
                self._grants.append(grant)

    def _save(self):
        payload = {"version": 1, "artifacts": list(self._artifacts.values()), "grants": self._grants}
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        temporary = ".artifacts-" + uuid.uuid4().hex + ".tmp"
        with self._directory() as root_fd:
            try:
                try:
                    info = os.stat(_INDEX, dir_fd=root_fd, follow_symlinks=False)
                except FileNotFoundError:
                    info = None
                if info and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
                    raise ArtifactError("产物索引不是安全的普通文件。")
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=root_fd)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, _INDEX, src_dir_fd=root_fd, dst_dir_fd=root_fd)
                    os.fsync(root_fd)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=root_fd)
                    except FileNotFoundError:
                        pass
            except OSError as exc:
                raise ArtifactError("无法持久化产物索引。") from exc

    def _relative_path(self, owner, run_id, path):
        try:
            raw = os.fspath(path)
        except TypeError as exc:
            raise ArtifactError("产物路径必须是字符串或 Path。") from exc
        if not isinstance(raw, str):
            raise ArtifactError("产物路径必须是文本。")
        candidate = Path(raw)
        if candidate.is_absolute():
            # Do not resolve here: resolving would hide symlinks and '..'.
            # The trusted data root may itself be hidden (for example .runtime),
            # but no component provided below the workspace root may be hidden.
            prefix = str(self.data_dir / "workspaces" / owner / run_id) + "/"
            if not raw.startswith(prefix):
                raise ArtifactError("绝对路径必须位于当前 Agent 的工作目录。")
            return _safe_parts(raw[len(prefix):])
        return _safe_parts(raw)

    def register(self, owner, run_id, path, task_id):
        owner = _agent(owner, self.agents)
        run_id = _identifier(run_id, "Run ID")
        task_id = _task(task_id)
        parts = self._relative_path(owner, run_id, path)
        suffix = Path(parts[-1]).suffix.lower()
        if suffix not in _TEXT_TYPES:
            raise ArtifactError("仅支持明确列出的文本文件扩展名。")
        with self.lock:
            with self._directory(["workspaces", owner, run_id, *parts[:-1]]) as directory_fd:
                try:
                    fd = self._open_read(directory_fd, parts[-1])
                except OSError as exc:
                    raise ArtifactError("产物不存在或不是安全文件。") from exc
                try:
                    raw = _read_file(fd, _MAX_BYTES)
                finally:
                    os.close(fd)
            content = _text(raw)
            records = [item for item in self._artifacts.values() if item["runId"] == run_id]
            if len(records) >= _MAX_RUN_COUNT or sum(item["size"] for item in records) + len(raw) > _MAX_RUN_BYTES:
                raise ArtifactError("每次运行最多 40 个产物，文本总计不能超过 4 MiB。")
            artifact_id = "artifact-" + uuid.uuid4().hex
            item = {"id": artifact_id, "name": parts[-1], "ownerAgentId": owner,
                    "runId": run_id, "taskId": task_id, "mimeType": _TEXT_TYPES[suffix],
                    "content": content, "sha256": hashlib.sha256(raw).hexdigest(),
                    "path": "/".join(parts), "size": len(raw),
                    "createdAt": int(time.time() * 1000)}
            self._artifacts[artifact_id] = item
            try:
                self._save()
            except Exception:
                del self._artifacts[artifact_id]
                raise
            return copy.deepcopy(item)

    def grant(self, artifact_id, to_agent, run_id, authorized_by):
        _identifier(artifact_id, "产物 ID")
        to_agent = _agent(to_agent, self.agents)
        run_id = _identifier(run_id, "Run ID")
        _authorizer(authorized_by)
        with self.lock:
            item = self._artifacts.get(artifact_id)
            if item is None:
                raise ArtifactError("找不到产物。")
            if item["runId"] != run_id:
                raise ArtifactError("禁止跨运行共享产物。")
            raw = item["content"].encode("utf-8")
            with self._directory(["imports", to_agent, run_id, artifact_id], create=True) as directory_fd:
                try:
                    fd = os.open(item["name"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                 0o600, dir_fd=directory_fd)
                except FileExistsError:
                    try:
                        fd = self._open_read(directory_fd, item["name"])
                    except OSError as exc:
                        raise ArtifactError("只读副本路径不安全。") from exc
                    try:
                        existing = _read_file(fd, _MAX_BYTES)
                        if hashlib.sha256(existing).hexdigest() != item["sha256"]:
                            raise ArtifactError("已有只读副本与原始快照不一致。")
                        os.fchmod(fd, 0o444)
                    finally:
                        os.close(fd)
                else:
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(raw)
                            stream.flush()
                            os.fchmod(stream.fileno(), 0o444)
                            os.fsync(stream.fileno())
                        os.fsync(directory_fd)
                    except Exception:
                        try:
                            os.unlink(item["name"], dir_fd=directory_fd)
                        except FileNotFoundError:
                            pass
                        raise
            relative = artifact_id + "/" + item["name"]
            metadata = {key: copy.deepcopy(value) for key, value in item.items() if key != "content"}
            metadata.update(toAgentId=to_agent, authorizedBy=authorized_by, path=relative,
                            mountPath="/imports/" + relative, readOnly=True,
                            grantedAt=int(time.time() * 1000))
            previous = next((grant for grant in self._grants
                             if grant["id"] == artifact_id and grant["toAgentId"] == to_agent), None)
            if previous:
                return copy.deepcopy(previous)
            self._grants.append(metadata)
            try:
                self._save()
            except Exception:
                self._grants.pop()
                raise
            return copy.deepcopy(metadata)

    def carry_forward(self, source_run, target_run, participants):
        """Inherit published snapshots for the next task in one trusted team/group.

        The runtime supplies its persisted run records, never model-supplied run
        metadata. Only the previous owner and existing explicit grantees retain
        access; a new DAG may grant the new immutable artifact to another member.
        No source workspace is read, and no file is copied into a writable tree.
        """
        if not isinstance(source_run, dict) or not isinstance(target_run, dict):
            raise ArtifactError("继承需要可信的前后两次运行记录。")
        source_id = _identifier(source_run.get("id"), "源 Run ID")
        target_id = _identifier(target_run.get("id"), "目标 Run ID")
        team = _identifier(source_run.get("teamId"), "团队 ID")
        if (source_id == target_id or target_run.get("previousRunId") != source_id
                or target_run.get("teamId") != team
                or source_run.get("channelId") != "group" or target_run.get("channelId") != "group"
                or source_run.get("mode") != "task" or target_run.get("mode") != "task"
                or source_run.get("status") not in _TERMINAL_RUNS):
            raise ArtifactError("只允许同团队群聊中已结束的上一任务继承已发布产物。")
        if not isinstance(participants, (list, tuple)) or not participants:
            raise ArtifactError("继承必须指定当前运行的参与者。")
        recipients = {_agent(agent, self.agents) for agent in participants}
        if len(recipients) != len(participants) or not isinstance(target_run.get("participants"), list) or set(target_run["participants"]) != recipients:
            raise ArtifactError("继承参与者必须与目标运行一致且不能重复。")
        previous_participants = source_run.get("participants")
        if not isinstance(previous_participants, list) or not previous_participants:
            raise ArtifactError("源运行缺少参与者记录。")
        for agent in previous_participants:
            _agent(agent, self.agents)
        published = source_run.get("artifacts", [])
        if not isinstance(published, list):
            raise ArtifactError("源运行产物记录格式无效。")
        result = {"artifacts": [], "grants": [], "skipped": []}
        with self.lock:
            candidates = []
            seen = set()
            for reference in published:
                if not isinstance(reference, dict):
                    raise ArtifactError("源运行产物记录格式无效。")
                artifact_id = _identifier(reference.get("id"), "源产物 ID")
                if artifact_id in seen:
                    continue
                seen.add(artifact_id)
                item = self._artifacts.get(artifact_id)
                if item is None or item["runId"] != source_id or item["ownerAgentId"] not in previous_participants:
                    raise ArtifactError("源产物不属于指定的上一运行及参与者。")
                if reference.get("sha256", item["sha256"]) != item["sha256"]:
                    raise ArtifactError("源运行产物证据与已发布快照不一致。")
                # Inheritance is deliberately more restrictive than publishing:
                # credential material must not propagate into another run.
                if (any(_SENSITIVE_NAME.search(part) for part in _safe_parts(item["path"]))
                        or _SENSITIVE_CONTENT.search(item["content"])):
                    result["skipped"].append({"id": artifact_id, "name": item["name"], "reason": "credential_material"})
                    continue
                allowed = {item["ownerAgentId"]}
                allowed.update(grant["toAgentId"] for grant in self._grants if grant["id"] == artifact_id)
                allowed &= recipients
                if not allowed:
                    continue
                inherited = next((entry for entry in self._artifacts.values()
                                  if entry["runId"] == target_id and entry.get("sourceArtifactId") == artifact_id
                                  and entry.get("sourceRunId") == source_id), None)
                candidates.append((item, inherited, sorted(allowed)))
            records = [item for item in self._artifacts.values() if item["runId"] == target_id]
            fresh = [item for item, inherited, _ in candidates if inherited is None]
            if len(records) + len(fresh) > _MAX_RUN_COUNT or sum(item["size"] for item in records + fresh) > _MAX_RUN_BYTES:
                raise ArtifactError("继承会超过目标运行的产物配额。")
            created_ids = []
            created_imports = []
            old_grants = copy.deepcopy(self._grants)
            try:
                for item, inherited, allowed in candidates:
                    if inherited is None:
                        inherited = copy.deepcopy(item)
                        inherited.update(id="artifact-" + uuid.uuid4().hex, runId=target_id,
                                         createdAt=int(time.time() * 1000), carriedForward=True,
                                         inheritedReadOnly=True, teamId=team, channelId="group",
                                         sourceArtifactId=item["id"], sourceRunId=source_id,
                                         sourceTaskId=item["taskId"], sourceSha256=item["sha256"],
                                         originArtifactId=item.get("originArtifactId", item["id"]),
                                         originRunId=item.get("originRunId", source_id))
                        self._artifacts[inherited["id"]] = inherited
                        created_ids.append(inherited["id"])
                    for recipient in allowed:
                        already_granted = any(grant["id"] == inherited["id"] and grant["toAgentId"] == recipient
                                              for grant in old_grants)
                        if not already_granted:
                            created_imports.append((recipient, inherited["id"], inherited["name"]))
                        result["grants"].append(self.grant(inherited["id"], recipient, target_id,
                                                          "orchestrator:carry-" + source_id))
                    result["artifacts"].append(copy.deepcopy(inherited))
                self._save()
            except Exception:
                for recipient, artifact_id, name in created_imports:
                    try:
                        with self._directory(["imports", recipient, target_id, artifact_id]) as directory_fd:
                            os.unlink(name, dir_fd=directory_fd)
                    except (ArtifactError, FileNotFoundError):
                        pass
                for artifact_id in created_ids:
                    self._artifacts.pop(artifact_id, None)
                self._grants = old_grants
                self._save()
                raise
        return result

    def can_access(self, artifact_id, agent_id, run_id):
        """Check published ownership/grants without exposing another workspace."""
        with self.lock:
            item = self._artifacts.get(artifact_id)
            return bool(item and item.get('runId') == run_id and
                        (item.get('ownerAgentId') == agent_id or
                         any(g.get('id') == artifact_id and g.get('toAgentId') == agent_id
                             and g.get('runId') == run_id for g in self._grants)))

    def list_for_run(self, run_id):
        run_id = _identifier(run_id, "Run ID")
        with self.lock:
            return copy.deepcopy([item for item in self._artifacts.values() if item["runId"] == run_id])

    def get(self, artifact_id):
        _identifier(artifact_id, "产物 ID")
        with self.lock:
            item = self._artifacts.get(artifact_id)
            if item is None:
                raise ArtifactError("找不到产物。")
            return copy.deepcopy(item)
