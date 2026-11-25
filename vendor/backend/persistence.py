"""Durable state, owner-scoped memories, checkpoints and idempotency audit.

Migrations preserve existing conversations and runs. A reserved effect after a
crash is uncertain and must be reconciled; it must never be blindly replayed.
"""
from pathlib import Path
import copy
import datetime as dt
import json
import re
import sqlite3
import threading
import uuid


def timestamp():
    return dt.datetime.now(dt.timezone.utc).isoformat().replace('+00:00', 'Z')


def tokens(text):
    lowered = str(text).lower()
    words = set(re.findall(r'[a-z0-9_]{2,}', lowered))
    for phrase in re.findall(r'[\u4e00-\u9fff]+', lowered):
        words.update(phrase[i:i+2] for i in range(max(1, len(phrase)-1)))
    return words


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def _scoped(memory, channel_id):
    visibility = memory.get('visibility', 'private')
    if visibility == 'shared':
        return bool(memory.get('shareConfirmation'))
    if visibility == 'group':
        return True  # Still owner scoped: not shared with other agents.
    return channel_id != 'group' and memory.get('channelId') == channel_id


def _verified(memory):
    receipt = memory.get('confirmation')
    return (memory.get('kind') in ('procedural', 'semantic')
            and memory.get('status') in ('verified', 'active')
            and isinstance(receipt, dict) and bool(receipt.get('id'))
            and receipt.get('type') in ('human', 'tool', 'independent', 'result')
            and not memory.get('needsReconfirmation', False))


class Repository:
    SCHEMA_VERSION = 2

    def __init__(self, data_dir: Path):
        data_dir = Path(data_dir)
        data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = data_dir / 'platform.sqlite'
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self.path.chmod(0o600)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id=1), payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, channel TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memories (id TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS evaluations (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS memories_by_owner ON memories(owner);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
                event_id TEXT NOT NULL, payload TEXT NOT NULL, UNIQUE(run_id,event_id));
            CREATE INDEX IF NOT EXISTS events_by_run ON events(run_id,seq);
            CREATE TABLE IF NOT EXISTS checkpoints (
                run_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS effects (
                effect_key TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS memory_feedback (
                memory_id TEXT NOT NULL, owner TEXT NOT NULL, source_task TEXT NOT NULL,
                success INTEGER NOT NULL, created_at TEXT NOT NULL,
                PRIMARY KEY(memory_id,owner,source_task));
            CREATE TABLE IF NOT EXISTS handoffs (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL);
        ''')
        with self.lock, self.db:
            for memory_id, owner, payload in self.db.execute('SELECT id,owner,payload FROM memories').fetchall():
                memory = json.loads(payload)
                if memory.get('memorySchema') == 2:
                    continue
                # Old model-generated verified flags are not confirmation evidence.
                memory['legacyStatus'] = memory.get('status')
                memory['legacyRecord'] = copy.deepcopy(memory)
                memory.update(memorySchema=2, owner=owner, ownerAgentId=owner,
                              kind=memory.get('kind') if memory.get('kind') in ('episodic', 'procedural', 'semantic') else 'procedural',
                              status='candidate', verified=False, confirmation=None,
                              visibility='private', channelId=memory.get('channelId', owner),
                              migratedAt=timestamp())
                self.db.execute('UPDATE memories SET payload=? WHERE id=?', (_dump(memory), memory_id))
            if self.db.execute('PRAGMA user_version').fetchone()[0] < self.SCHEMA_VERSION:
                self.db.execute(f'PRAGMA user_version={self.SCHEMA_VERSION}')

    def close(self):
        with self.lock:
            self.db.close()

    def load_state(self):
        with self.lock:
            row = self.db.execute('SELECT payload FROM state WHERE id=1').fetchone()
            return json.loads(row[0]) if row else None

    def save_handoff(self, record):
        """Keep only the latest group-task card in this team's database."""
        if not isinstance(record, dict) or not isinstance(record.get('runId'), str) or not record['runId']:
            raise ValueError('Handoff requires a run id.')
        payload = json.dumps(record, ensure_ascii=False, sort_keys=True)
        with self.lock, self.db:
            self.db.execute(
                'INSERT INTO handoffs(id,payload) VALUES(1,?) '
                'ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                (payload,))

    def load_handoff(self):
        with self.lock:
            row = self.db.execute('SELECT payload FROM handoffs WHERE id=1').fetchone()
            return json.loads(row[0]) if row else None

    def save_state(self, state):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO state VALUES(1,?)', (_dump(state),))

    def save_run(self, run, channel_id):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO runs VALUES(?,?,?)', (run['id'], channel_id, _dump(run)))

    def get_run(self, run_id):
        with self.lock:
            row = self.db.execute('SELECT payload FROM runs WHERE id=?', (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def add_memory(self, agent_id, record):
        memory = copy.deepcopy(record)
        if memory.get('owner', agent_id) != agent_id or memory.get('ownerAgentId', agent_id) != agent_id:
            raise ValueError('Memory owner cannot be changed.')
        memory.update(owner=agent_id, ownerAgentId=agent_id)
        with self.lock, self.db:
            self.db.execute('INSERT INTO memories VALUES(?,?,?)', (memory['id'], agent_id, _dump(memory)))

    def get_memory(self, memory_id, owner):
        with self.lock:
            row = self.db.execute('SELECT payload FROM memories WHERE id=? AND owner=?', (memory_id, owner)).fetchone()
            return json.loads(row[0]) if row else None

    def memories(self, agent_id):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM memories WHERE owner=? ORDER BY rowid DESC', (agent_id,))]

    def all_memories(self):
        """Administrative maintenance only; never inject this list into a prompt."""
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM memories ORDER BY rowid')]

    def update_memories(self, owner, records):
        with self.lock, self.db:
            for memory in records:
                if memory.get('owner') != owner or memory.get('ownerAgentId', owner) != owner:
                    raise ValueError('Memory owner cannot be changed.')
                cursor = self.db.execute('UPDATE memories SET payload=? WHERE id=? AND owner=?', (_dump(memory), memory['id'], owner))
                if cursor.rowcount != 1:
                    raise KeyError('Memory not found for this owner.')

    def update_memory(self, owner, record):
        self.update_memories(owner, [record])
        return copy.deepcopy(record)

    def search_memories(self, agent_id, query, channel_id='group', limit=4):
        query_tokens = tokens(query)
        ranked = []
        for memory in self.memories(agent_id):
            if not _verified(memory) or not _scoped(memory, channel_id):
                continue
            overlap = len(tokens(' '.join(str(memory.get(key, '')) for key in ('title', 'lesson', 'trigger', 'conditions'))) & query_tokens)
            if overlap:
                ranked.append((overlap * float(memory.get('confidence', .5)), memory))
        return [item for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True)[:max(0, min(int(limit), 100))]]

    def mark_memory_used(self, agent_id, memory_id):
        with self.lock, self.db:
            memory = self.get_memory(memory_id, agent_id)
            if memory:
                memory['uses'] = int(memory.get('uses', 0)) + 1
                memory['lastUsedAt'] = timestamp()
                self.db.execute('UPDATE memories SET payload=? WHERE id=? AND owner=?', (_dump(memory), memory_id, agent_id))

    def apply_memory_feedback(self, owner, memory_id, source_task, success, updater):
        """Outcome and counters commit together, exactly once per source task."""
        with self.lock, self.db:
            inserted = self.db.execute('INSERT OR IGNORE INTO memory_feedback VALUES(?,?,?,?,?)',
                                       (memory_id, owner, source_task, int(success), timestamp())).rowcount
            memory = self.get_memory(memory_id, owner)
            if memory is None:
                raise KeyError('Memory not found for this owner.')
            if not inserted:
                return memory, False
            updated = updater(copy.deepcopy(memory))
            self.db.execute('UPDATE memories SET payload=? WHERE id=? AND owner=?', (_dump(updated), memory_id, owner))
            return updated, True

    def append_event(self, run_id, event):
        item = copy.deepcopy(event)
        item.setdefault('id', 'event-' + uuid.uuid4().hex)
        item.setdefault('time', timestamp())
        with self.lock, self.db:
            row = self.db.execute('SELECT payload FROM events WHERE run_id=? AND event_id=?', (run_id, item['id'])).fetchone()
            if row:
                previous = json.loads(row[0])
                compared = dict(item)
                if 'time' not in event:
                    compared['time'] = previous.get('time')
                if compared != previous:
                    raise ValueError('Event id already belongs to different content.')
                return previous
            self.db.execute('INSERT INTO events(run_id,event_id,payload) VALUES(?,?,?)', (run_id, item['id'], _dump(item)))
        return item

    def events(self, run_id):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM events WHERE run_id=? ORDER BY seq', (run_id,))]

    def save_checkpoint(self, run_id, payload):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?)', (run_id, _dump(payload), timestamp()))

    def checkpoint(self, run_id):
        with self.lock:
            row = self.db.execute('SELECT payload FROM checkpoints WHERE run_id=?', (run_id,)).fetchone()
            return json.loads(row[0]) if row else None

    @staticmethod
    def _effect_record(row):
        if row is None:
            return None
        return {'key': row[0], 'status': row[1], 'payload': json.loads(row[2]), 'result': json.loads(row[3]) if row[3] is not None else None, 'createdAt': row[4], 'updatedAt': row[5]}

    def effect(self, key):
        with self.lock:
            return self._effect_record(self.db.execute('SELECT * FROM effects WHERE effect_key=?', (key,)).fetchone())

    def reserve_effect(self, key, payload):
        if not isinstance(key, str) or not key:
            raise ValueError('Effect key must be a nonempty string.')
        serialized = _dump(payload)
        with self.lock, self.db:
            now = timestamp()
            inserted = self.db.execute('INSERT OR IGNORE INTO effects VALUES(?,?,?,?,?,?)',
                                       (key, 'reserved', serialized, None, now, now)).rowcount
            current = self.effect(key)
            if _dump(current['payload']) != serialized:
                raise ValueError('Effect key cannot be reused for different arguments.')
            return {**current, 'new': bool(inserted)}

    def complete_effect(self, key, result):
        serialized = _dump(result)
        with self.lock, self.db:
            current = self.effect(key)
            if current is None:
                raise KeyError('Effect was not reserved.')
            if current['status'] == 'completed':
                if _dump(current['result']) != serialized:
                    raise ValueError('Completed effect result cannot be replaced.')
                return current
            self.db.execute('UPDATE effects SET status=?,result=?,updated_at=? WHERE effect_key=? AND status=?',
                            ('completed', serialized, timestamp(), key, 'reserved'))
            completed = self.effect(key)
            if _dump(completed['result']) != serialized:
                raise ValueError('Completed effect result cannot be replaced.')
            return completed

    def save_evaluation(self, record):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO evaluations VALUES(?,?)', (record['id'], _dump(record)))

    def evaluation_runs(self):
        with self.lock:
            assessed = [json.loads(row[0]) for row in self.db.execute('SELECT payload FROM evaluations ORDER BY rowid DESC LIMIT 200')]
            assessed_ids = {item.get('runId') for item in assessed}
            normal = []
            for row in self.db.execute('SELECT payload FROM runs ORDER BY rowid DESC LIMIT 200'):
                run = json.loads(row[0])
                if run['id'] in assessed_ids or not run.get('finishedAt'):
                    continue
                normal.append({'id': run['id'], 'runId': run['id'], 'taskId': run['id'], 'task': run.get('title', ''), 'category': '用户任务', 'status': 'unassessed', 'verified': False, 'verifier': None, 'executionStatus': run['status'], 'memoryEnabled': run.get('memoryEnabled', True), 'elapsedSec': max(0, (run['finishedAt']-run['startedAt'])/1000)})
            return assessed + normal
