"""Evidence-gated strategy memory; working context stays in the agent runtime.

confirm/share are trusted engine or human operations, never model tools. The
caller binds their receipt ids to actual results/approvals. Retrieval alone does
not count as usage; feedback records one real outcome per subsequent task.
"""
import copy
import datetime as dt
import hashlib
import math
import sqlite3
import time
import uuid
from .persistence import tokens, _scoped, _verified


class MemoryService:
    def __init__(self, repo, *, clock=None, capacity=200, episodic_capacity=300,
                 archive_after_days=180, half_life_days=90, min_confidence=.25):
        self.repo = repo
        self.clock = clock or time.time
        self.capacity = max(1, int(capacity))
        self.episodic_capacity = max(1, int(episodic_capacity))
        self.archive_after_days = max(1, float(archive_after_days))
        self.half_life_days = max(1, float(half_life_days))
        self.min_confidence = max(0, min(1, float(min_confidence)))

    def _now(self):
        value = self.clock()
        if isinstance(value, dt.datetime):
            value = value.replace(tzinfo=value.tzinfo or dt.timezone.utc).timestamp()
        return float(value)

    @staticmethod
    def _iso(value):
        return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat().replace('+00:00', 'Z')

    @staticmethod
    def _epoch(value, default):
        try:
            if isinstance(value, (int, float)):
                return float(value) / 1000 if value > 100_000_000_000 else float(value)
            parsed = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
            return parsed.replace(tzinfo=parsed.tzinfo or dt.timezone.utc).timestamp()
        except (AttributeError, TypeError, ValueError, OverflowError):
            return default

    @staticmethod
    def _owner(value):
        if not isinstance(value, str) or not value.strip() or len(value) > 120:
            raise ValueError('A memory owner is required.')
        return value

    @staticmethod
    def _text(value, field, required=False, limit=12000):
        if not isinstance(value, str) or (required and not value.strip()):
            raise ValueError(f'{field} must be a {"nonempty " if required else ""}string.')
        return value.strip()[:limit]

    @staticmethod
    def _receipt(reason):
        if isinstance(reason, str) and ':' in reason:
            kind, identifier = reason.split(':', 1)
            reason = {'type': kind, 'id': identifier}
        if (not isinstance(reason, dict) or reason.get('type') not in ('human', 'tool', 'independent', 'result')
                or not isinstance(reason.get('id'), str) or not reason['id'].strip()):
            raise ValueError('Confirmation needs an actual human/tool/independent/result receipt id; model self-reports are insufficient.')
        return {'type': reason['type'], 'id': reason['id'].strip()[:300]}

    def _base(self, owner, kind, channel_id):
        owner = self._owner(owner)
        if not isinstance(channel_id, str) or not channel_id:
            raise ValueError('Memory context channel is required.')
        now = self._iso(self._now())
        return {'id': 'memory-' + uuid.uuid4().hex, 'memorySchema': 2, 'owner': owner, 'ownerAgentId': owner,
                'kind': kind, 'layer': kind, 'type': kind,
                'visibility': 'group' if channel_id == 'group' else 'private', 'channelId': channel_id,
                'createdAt': now, 'updatedAt': now, 'confidenceAt': now, 'lastActivityAt': now,
                'lastUsedAt': None, 'confidence': .35, 'success_count': 0, 'failure_count': 0,
                'uses': 0, 'feedback_count': 0, 'consecutive_failures': 0,
                'status': 'candidate', 'verified': False, 'confirmation': None, 'supersedes': []}

    def _get(self, memory_id, owner):
        memory = self.repo.get_memory(memory_id, self._owner(owner))
        if memory is None:
            raise KeyError('Memory not found for this owner.')
        return memory

    def list(self, agent_id):
        return self.repo.memories(self._owner(agent_id))

    @staticmethod
    def _retrieval_key(item):
        # Retrieval deduplication never edits records or merges provenance. A
        # shared strategy and a private one (or different owners) stay distinct.
        # Conditions and identifier case can change the meaning of a strategy.
        # Only trim outer whitespace; never normalize code/path spelling or
        # collapse distinct applicability rules into one retrieved entry.
        lesson = str(item.get('lesson', '')).strip()
        return (item.get('owner'), item.get('visibility'), item.get('channelId'), item.get('kind'),
                str(item.get('title', '')).strip(),
                lesson or item.get('id'), str(item.get('trigger', '')).strip(),
                str(item.get('conditions', '')).strip())

    def recall(self, agent_id, query, channel_id='group', limit=4, *, include_shared=False):
        owner = self._owner(agent_id)
        query_terms = tokens(query)
        if not query_terms:
            return []
        candidates = self.repo.memories(owner)
        # Cross-owner retrieval requires an explicit, audited sharing operation.
        if include_shared is True:
            candidates += [item for item in self.repo.all_memories()
                           if item.get('owner') != owner and item.get('visibility') == 'shared' and item.get('shareConfirmation')]
        # Filter before computing corpus statistics: private/unverified records
        # must not influence another context's ranking, even indirectly.
        eligible = [item for item in candidates
                    if _verified(item) and _scoped(item, channel_id)
                    and float(item.get('confidence', 0)) >= self.min_confidence]
        if not eligible:
            return []
        # Lightweight BM25F-style lexical retrieval, not an embedding model.
        # Fixed conventional k1/b avoid tuning against the regression fixture;
        # applicability fields carry more weight than a lesson mentioning a
        # different failure as a counterexample. Existing CJK tokens are kept.
        weights = {'trigger': 3.0, 'conditions': 2.0, 'title': 1.0, 'lesson': 1.0}
        fields = [{field: tokens(item.get(field, '')) for field in weights} for item in eligible]
        # Repeated receipts must not change either IDF or length normalization.
        # Count the same complete strategy once in both corpus statistics.
        unique_fields = {}
        for item, record in zip(eligible, fields):
            unique_fields.setdefault(self._retrieval_key(item), record)
        averages = {field: max(1, sum(len(record[field]) for record in unique_fields.values()) / len(unique_fields))
                    for field in weights}
        documents = {key: set().union(*record.values()) for key, record in unique_fields.items()}
        document_count = len(documents)
        idf = {term: math.log(1 + (document_count - frequency + .5) / (frequency + .5))
               for term in query_terms
               for frequency in [sum(term in document for document in documents.values())]}
        ranked = []
        now = self._now()
        for item, record in zip(eligible, fields):
            score = 0
            for term in query_terms:
                frequency = sum(weight / (.25 + .75 * len(record[field]) / averages[field])
                                for field, weight in weights.items() if term in record[field])
                if frequency:
                    score += idf[term] * 2.2 * frequency / (1.2 + frequency)
            if score:
                age = max(0, now - self._epoch(item.get('lastActivityAt'), now)) / 86400
                score *= float(item.get('confidence', 0)) * (.5 + .5 / (1 + age / 30))
                ranked.append((score, item))
        selected, seen = [], set()
        maximum = max(0, min(int(limit), 100))
        for _, item in sorted(ranked, key=lambda pair: pair[0], reverse=True):
            key = self._retrieval_key(item)
            if key not in seen and len(selected) < maximum:
                selected.append(copy.deepcopy(item))
                seen.add(key)
        return selected

    def record_episode(self, agent_id, run, task, success, reason, call_ids, channel_id):
        if not isinstance(run, dict) or not run.get('id') or type(success) is not bool:
            raise ValueError('Episode requires a run id and a boolean outcome.')
        if run.get('participants') is not None and agent_id not in run['participants']:
            raise ValueError('Only participating agents can own this episode.')
        task_id = task.get('id') if isinstance(task, dict) else task
        if not isinstance(task_id, str) or not task_id:
            raise ValueError('Episode source task id is required.')
        if isinstance(task, dict) and task.get('agentId', agent_id) != agent_id:
            raise ValueError('Episode task must belong to this agent.')
        memory = self._base(agent_id, 'episodic', channel_id)
        revision = task.get('revision', 0) if isinstance(task, dict) else 0
        fingerprint = '\0'.join((agent_id, channel_id, run['id'], task_id, str(revision)))
        memory['id'] = 'episode-' + hashlib.sha256(fingerprint.encode()).hexdigest()[:32]
        memory.update(title=(task.get('title', task_id) if isinstance(task, dict) else task_id),
                      lesson='', trigger='', conditions='', source_task=task_id, sourceTaskId=task_id,
                      source_run=run['id'], sourceRunId=run['id'], source_revision=revision, success=success,
                      reason=self._text(reason, 'reason'), call_ids=list(call_ids or []),
                      evidence=[{'type': 'call', 'id': item} for item in (call_ids or [])],
                      status='episodic', confidence=1.0)
        try:
            self.repo.add_memory(agent_id, memory)
        except sqlite3.IntegrityError:
            return self._get(memory['id'], agent_id)
        return copy.deepcopy(memory)

    def propose(self, agent_id, lesson, trigger, source_task, source_run, evidence,
                conditions='', kind='procedural', channel_id='group', supersedes=None,
                signature=None):
        if isinstance(lesson, dict):
            specification = lesson
            lesson = specification.get('lesson', '')
            trigger = specification.get('trigger', trigger)
            conditions = specification.get('conditions', conditions)
            supersedes = specification.get('supersedes', supersedes)
            signature = specification.get('signature', signature)
        if kind not in ('procedural', 'semantic'):
            raise ValueError('Only procedural/semantic strategies can be proposed; episodes use record_episode.')
        memory = self._base(agent_id, kind, channel_id)
        lesson = self._text(lesson, 'lesson', True)
        source_task = self._text(source_task, 'source_task', True, 300)
        source_run = self._text(source_run, 'source_run', True, 300)
        replacements = [supersedes] if isinstance(supersedes, str) else list(supersedes or [])
        for previous_id in replacements:
            previous = self._get(previous_id, agent_id)
            if previous.get('kind') != kind or previous.get('channelId') != channel_id or previous.get('visibility') == 'shared':
                raise ValueError('Supersession must stay within the same owner, kind and context; shared knowledge needs explicit review.')
        memory.update(title=lesson[:100], lesson=lesson, trigger=self._text(trigger, 'trigger', True),
                      conditions=self._text(conditions, 'conditions'), source_task=source_task, sourceTaskId=source_task,
                      source_run=source_run, sourceRunId=source_run, evidence=copy.deepcopy(evidence if isinstance(evidence, list) else [evidence]) if evidence else [],
                      supersedes=list(dict.fromkeys(replacements)))
        if isinstance(signature, str) and signature.strip():
            memory['sourceSignature'] = signature.strip()[:200]
        self.repo.add_memory(agent_id, memory)
        return copy.deepcopy(memory)

    def find_by_signature(self, agent_id, signature, channel_id='group', kind='procedural'):
        """Return this agent's current memory for one task signature, if any."""
        if not isinstance(signature, str) or not signature.strip():
            return None
        target = signature.strip()[:200]
        for memory in self.repo.memories(self._owner(agent_id)):
            if (memory.get('sourceSignature') == target
                    and memory.get('kind') == kind
                    and memory.get('channelId') == channel_id
                    and memory.get('status') not in ('archived', 'superseded', 'rejected')):
                return copy.deepcopy(memory)
        return None

    def record_success_streak(self, memory_id, owner, source_task, source_run,
                              *, threshold=2, reason=None):
        """Count distinct successful tasks. Two promote a candidate via a result receipt."""
        if not isinstance(threshold, int) or threshold < 2:
            raise ValueError('Auto-confirm needs at least two distinct successes.')
        task_id = self._text(source_task, 'source_task', True, 300)
        run_id = self._text(source_run, 'source_run', True, 300)
        with self.repo.lock:
            memory = self._get(memory_id, owner)
            if memory.get('kind') not in ('procedural', 'semantic'):
                raise ValueError('Only procedural/semantic strategies accept success streaks.')
            if memory.get('status') in ('archived', 'superseded', 'rejected'):
                return {'applied': False, 'memory': copy.deepcopy(memory),
                        'reason': 'non_active_status'}
            witnesses = list(memory.get('successWitnesses') or [])
            if task_id in witnesses:
                return {'applied': False, 'memory': copy.deepcopy(memory),
                        'reason': 'duplicate_witness'}
            witnesses.append(task_id)
            memory['successWitnesses'] = witnesses[-10:]
            memory['successStreak'] = len(memory['successWitnesses'])
            now = self._iso(self._now())
            memory.update(lastActivityAt=now, updatedAt=now)
            self.repo.update_memory(owner, memory)
        promoted = False
        if (memory['successStreak'] >= threshold
                and memory.get('status') == 'candidate'
                and not _verified(memory)):
            receipt_id = (reason if isinstance(reason, str) and reason.startswith('result:')
                          else 'result:' + run_id + ':' + task_id)
            memory = self.confirm(memory_id, owner, True, receipt_id)
            promoted = True
        return {'applied': True, 'memory': memory, 'promoted': promoted,
                'streak': memory.get('successStreak', 0)}

    def confirm(self, memory_id, owner, confirmed: bool, reason=''):
        if type(confirmed) is not bool:
            raise ValueError('Confirmation must be an explicit boolean.')
        with self.repo.lock:
            memory = self._get(memory_id, owner)
            if memory.get('kind') not in ('procedural', 'semantic') or memory.get('status') in ('archived', 'superseded'):
                raise ValueError('Only a current strategy candidate can be confirmed; episodes are not policies.')
            now = self._iso(self._now())
            memory.update(updatedAt=now, lastActivityAt=now, confidenceAt=now)
            updates = [memory]
            if not confirmed:
                memory.update(status='rejected', verified=False, rejectionReason=copy.deepcopy(reason), confidence=.1)
            else:
                receipt = {**self._receipt(reason), 'confirmedAt': now}
                previous_receipt = memory.get('confirmation') or {}
                if (_verified(memory) and previous_receipt.get('type') == receipt['type']
                        and previous_receipt.get('id') == receipt['id']):
                    return memory
                memory.update(status='verified', verified=True, confirmation=receipt,
                              needsReconfirmation=False, confidence=max(.65, float(memory.get('confidence', 0))))
                memory.setdefault('evidence', []).append(copy.deepcopy(receipt))
                for previous_id in memory.get('supersedes', []):
                    previous = self._get(previous_id, owner)
                    if (previous.get('kind') != memory['kind'] or previous.get('channelId') != memory['channelId']
                            or previous.get('visibility') == 'shared'):
                        raise ValueError('Cross-context supersession is not allowed.')
                    previous.update(status='superseded', verified=False, supersededBy=memory_id,
                                    confidence=round(float(previous.get('confidence', .35)) * .5, 4), updatedAt=now)
                    updates.append(previous)
            self.repo.update_memories(owner, updates)
            return copy.deepcopy(memory)

    def share(self, memory_id, owner, reason):
        """Explicitly publish verified knowledge; caller binds a human approval."""
        with self.repo.lock:
            memory = self._get(memory_id, owner)
            receipt = self._receipt(reason)
            if receipt['type'] != 'human' or not _verified(memory):
                raise ValueError('Sharing requires a verified strategy and explicit human approval.')
            memory.update(visibility='shared', shareConfirmation={**receipt, 'confirmedAt': self._iso(self._now())})
            return self.repo.update_memory(owner, memory)

    def feedback(self, agent_id, memory_id, success, source_task):
        if type(success) is not bool:
            raise ValueError('Outcome must be a boolean backed by the task result.')
        task_id = self._text(source_task, 'source_task', True, 300)
        now = self._iso(self._now())
        def update(memory):
            if not _verified(memory):
                raise ValueError('Unconfirmed or archived memory cannot gain trusted feedback.')
            if task_id == memory.get('source_task', memory.get('sourceTaskId')):
                raise ValueError('Feedback must come from a subsequent task.')
            field = 'success_count' if success else 'failure_count'
            memory[field] = int(memory.get(field, 0)) + 1
            memory['uses'] = int(memory.get('uses', 0)) + 1
            memory['feedback_count'] = int(memory.get('feedback_count', 0)) + 1
            memory['consecutive_failures'] = 0 if success else int(memory.get('consecutive_failures', 0)) + 1
            memory['confidence'] = round((3 + memory.get('success_count', 0)) / (5 + memory.get('success_count', 0) + memory.get('failure_count', 0)), 4)
            memory.update(lastUsedAt=now, lastActivityAt=now, updatedAt=now, confidenceAt=now)
            if memory['consecutive_failures'] >= 3:
                memory.update(status='candidate', verified=False, needsReconfirmation=True,
                              confidence=min(.3, memory['confidence']))
            return memory
        memory, applied = self.repo.apply_memory_feedback(agent_id, memory_id, task_id, success, update)
        return {**memory, 'feedbackApplied': applied}

    def maintenance(self):
        now = self._now()
        instant = self._iso(now)
        counts = {'decayed': 0, 'archived': 0, 'byOwner': {}}
        with self.repo.lock:
            records = self.repo.all_memories()
            changed = {}
            def archive(memory, reason):
                if memory.get('status') == 'archived':
                    return
                memory.update(status='archived', verified=False, archivedAt=instant, archivedReason=reason)
                counts['archived'] += 1
                changed[memory['id']] = memory
            for memory in records:
                owner = memory['owner']
                counts['byOwner'][owner] = counts['byOwner'].get(owner, 0) + 1
                if memory.get('status') in ('archived', 'superseded', 'rejected'):
                    continue
                created = self._epoch(memory.get('createdAt', memory.get('migratedAt')), now)
                activity = self._epoch(memory.get('lastActivityAt'), created)
                age_days = max(0, now - activity) / 86400
                confidence_at = self._epoch(memory.get('confidenceAt'), created)
                decay_days = max(0, now - confidence_at) / 86400
                if memory.get('kind') != 'episodic' and decay_days >= 1:
                    memory['confidence'] = round(float(memory.get('confidence', .35)) * .5 ** (decay_days / self.half_life_days), 6)
                    memory['confidenceAt'] = instant
                    counts['decayed'] += 1
                    changed[memory['id']] = memory
                if age_days >= self.archive_after_days:
                    archive(memory, 'inactive-age-limit')
                elif memory.get('kind') != 'episodic' and float(memory.get('confidence', .35)) < self.min_confidence:
                    archive(memory, 'confidence-below-threshold')
            for owner in counts['byOwner']:
                own = [memory for memory in records if memory['owner'] == owner and memory.get('status') not in ('archived', 'superseded', 'rejected')]
                for episodic, capacity in ((False, self.capacity), (True, self.episodic_capacity)):
                    layer = [memory for memory in own if (memory.get('kind') == 'episodic') == episodic]
                    layer.sort(key=lambda memory: (_verified(memory), float(memory.get('confidence', 0)), self._epoch(memory.get('lastActivityAt'), 0)), reverse=True)
                    for memory in layer[capacity:]:
                        archive(memory, 'owner-capacity-limit')
                updates = [memory for memory in changed.values() if memory['owner'] == owner]
                if updates:
                    self.repo.update_memories(owner, updates)
        return {**counts, 'total': len(records)}
