"""Bounded, owner-only learning records. This does not call a model or run tools.

Episodes are facts, candidates are untrusted experiment suggestions, and verified
strategies stay a separate layer. A narrow read-loop experiment can gain limited
workflow evidence; neither a run's ``done`` nor exit(0) proves a business goal or
causality. The caller is trusted engine code, never a model-visible confirm tool.
"""
import copy
import hashlib
import json
import re

from .persistence import tokens, _verified


_SHA = re.compile(r'^[a-f0-9]{64}$')
_RULE = 'avoid_redundant_reads'
_WRITE_RULE = 'write_churn_without_test'


def _fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class LearningService:
    def __init__(self, memory, team_id='default'):
        self.memory, self.repo = memory, memory.repo
        self.team_id = memory._text(team_id, 'team_id', True, 120)

    def _bound(self, owner, run, task, channel):
        self.memory._owner(owner)
        if not isinstance(channel, str) or not channel:
            raise ValueError('A learning channel is required.')
        if (not isinstance(run, dict) or not isinstance(run.get('id'), str)
                or run.get('teamId') != self.team_id or run.get('channelId') != channel
                or owner not in run.get('participants', [])):
            raise ValueError('Learning run must belong to this team and include its owner.')
        revision = task.get('revision') if isinstance(task, dict) else None
        if (not isinstance(task, dict) or not isinstance(task.get('id'), str) or task.get('agentId') != owner
                or type(revision) is not int or revision < 0):
            raise ValueError('Learning requires the real owner/task and an explicit revision.')
        current = [item for item in run.get('steps', []) if isinstance(item, dict) and item.get('id') == task['id']]
        if len(current) != 1 or current[0].get('revision') != revision or current[0].get('agentId') != owner or current[0] != task:
            raise ValueError('Learning task is not the current run revision.')
        return {'teamId': self.team_id, 'ownerAgentId': owner, 'channelId': channel,
                'runId': run['id'], 'taskId': task['id'], 'revision': revision}

    def _scoped(self, memory, owner, channel):
        # Historical human-shared entries remain stored, but never cross this
        # owner boundary; an old private channel is not made public implicitly.
        return (memory.get('ownerAgentId', memory.get('owner')) == owner
                and memory.get('teamId', self.team_id) == self.team_id
                and (memory.get('channelId') == 'group' or memory.get('channelId') == channel))

    @staticmethod
    def _own_calls(run, task):
        return [call for call in run.get('calls', []) if isinstance(call, dict)
                and call.get('agentId') == task['agentId'] and call.get('taskId') == task['id']
                and type(call.get('revision')) is int and call['revision'] == task['revision']
                and call.get('tool') != 'llm.chat']

    @staticmethod
    def _browser_observations(result):
        """Actual browser attempt, including failed assertions; not arbitrary exit(0)."""
        browser = result.get('browser') if isinstance(result.get('browser'), dict) else {}
        assertions = browser.get('assertions')
        if (result.get('runtime') != 'browser' or browser.get('executed') is not True
                or not isinstance(assertions, list) or not assertions
                or type(browser.get('assertionCount')) is not int or browser['assertionCount'] != len(assertions)
                or any(not isinstance(item, dict) or type(item.get('passed')) is not bool
                       or not str(item.get('action', '')).startswith('assert_') for item in assertions)):
            return set()
        def pairs(items):
            if not isinstance(items, list):
                return set()
            return {(item['path'], item['sha256']) for item in items if isinstance(item, dict)
                    and isinstance(item.get('path'), str) and item['path'] and _SHA.fullmatch(str(item.get('sha256', '')))}
        entry = browser.get('entry')
        observed = pairs(browser.get('testedFiles'))
        manifest = pairs(browser.get('sourceManifest'))
        if not isinstance(entry, dict) or (entry.get('path'), entry.get('sha256')) not in observed & manifest:
            return set()
        return observed & manifest

    @staticmethod
    def _facts(calls):
        groups, valid_reads, mutations, tests, failures, peak_repeats = {}, [], [], [], [], 0
        versions, interval_versions, peak_untested_versions = {}, {}, 0
        latest_mutations, tested_mutations, actual_resource_paths = {}, [], set()
        for call in calls:
            tool, result = call.get('tool'), call.get('result')
            result = result if isinstance(result, dict) else {}
            args = call.get('args') if isinstance(call.get('args'), dict) else {}
            path = result.get('path', args.get('path'))
            ok = call.get('status') == 'success' and result.get('ok') is True
            if tool in ('file.write', 'file.patch', 'file.delete') and ok:
                mutations.append({'callId': call.get('id'), 'tool': tool, 'path': path, 'sha256': result.get('sha256')})
                groups = {key: value for key, value in groups.items() if key[0] != path}
                if tool == 'file.delete':
                    latest_mutations.pop(path, None)
                if isinstance(path, str) and tool != 'file.delete' and _SHA.fullmatch(str(result.get('sha256', ''))):
                    latest_mutations[path] = mutations[-1]
                    versions.setdefault(path, set()).add(result['sha256'])
                    interval_versions.setdefault(path, set()).add(result['sha256'])
                    peak_untested_versions = max(peak_untested_versions, len(interval_versions[path]))
            observed = LearningService._browser_observations(result) if tool == 'code.test' and call.get('status') in ('success', 'failed') else set()
            if observed:
                actual_resource_paths.update(path for path, _ in observed)
                tests.append(call.get('id'))
                for changed, mutation in latest_mutations.items():
                    if (changed, mutation.get('sha256')) in observed:
                        interval_versions.pop(changed, None)
                        tested_mutations.append({'path': changed, 'sha256': mutation['sha256'], 'callId': call.get('id')})
            if tool == 'file.read' and ok and isinstance(path, str) and _SHA.fullmatch(str(result.get('sha256', ''))):
                mode = result.get('mode', 'full')
                if mode == 'full' and isinstance(result.get('content'), str) and result['content']:
                    if hashlib.sha256(result['content'].encode('utf-8')).hexdigest() != result['sha256']:
                        continue
                    coverage = ('full',)
                elif mode == 'lines' and isinstance(result.get('content'), str) and result['content'] and type(result.get('startLine')) is int and type(result.get('endLine')) is int:
                    coverage = ('lines', result['startLine'], result['endLine'], result.get('startColumn'), result.get('endColumn'))
                elif mode == 'find' and isinstance(result.get('matches'), list) and result['matches']:
                    matches = result['matches']
                    if any(not isinstance(item, dict) or not item.get('content') or type(item.get('startLine')) is not int or type(item.get('endLine')) is not int for item in matches):
                        continue
                    coverage = ('find', tuple((item['startLine'], item['endLine'], item.get('startColumn'), item.get('endColumn')) for item in matches))
                else:
                    continue
                key = (path, result['sha256'], coverage)
                groups[key] = groups.get(key, 0) + 1
                peak_repeats = max(peak_repeats, groups[key])
                valid_reads.append({'callId': call.get('id'), 'path': path, 'sha256': result['sha256'], 'coverage': list(coverage)})
            if call.get('status') in ('failed', 'denied', 'unknown', 'interrupted') or result.get('ok') is False and not result.get('needsApproval'):
                error = call.get('error') or result.get('error') or {}
                failures.append({'callId': call.get('id'), 'tool': tool,
                    'code': error.get('code', '') if isinstance(error, dict) else '', 'status': call.get('status')})
        return {'readCount': len(valid_reads), 'maxRepeatedReadCount': peak_repeats,
                'readPaths': sorted({item['path'] for item in valid_reads}), 'reads': valid_reads[-12:],
                'mutations': mutations[-12:], 'testCallIds': tests[-8:], 'failures': failures[-8:],
                'mutationCount': sum(item['tool'] != 'file.delete' for item in mutations),
                'latestMutations': list(latest_mutations.values())[-12:], 'testedMutationVersions': tested_mutations[-12:],
                'latestMutationsOmitted': max(0, len(latest_mutations) - 12), 'actualResourcePaths': sorted(actual_resource_paths)[-40:],
                'actualResourcePathsOmitted': max(0, len(actual_resource_paths) - 40),
                'maxFileVersions': max((len(values) for values in versions.values()), default=0),
                'maxVersionsBetweenTests': peak_untested_versions,
                'bodyAvailabilityAtDecision': 'unknown; historical calls do not prove a body survived context compression'}

    def observe_failure(self, agent_id, run, task, channel_id='group'):
        binding = self._bound(agent_id, run, task, channel_id)
        if task.get('status') not in ('failed', 'partial', 'rejected'):
            raise ValueError('Failure learning cannot be inferred from a successful or running task.')
        calls = self._own_calls(run, task)
        facts = self._facts(calls)
        error = task.get('error') if isinstance(task.get('error'), dict) else {}
        reason = f"Observed task status={task['status']}; errorCode={str(error.get('code', ''))[:120]}; reads={facts['readCount']}; maxSameRead={facts['maxRepeatedReadCount']}."
        with self.repo.lock:
            episode = self.memory.record_episode(agent_id, run, task, False, reason, [call['id'] for call in calls if call.get('id')], channel_id)
            episode.update(teamId=self.team_id, learningFacts=facts, learningBinding=binding)
            self.repo.update_memory(agent_id, episode)
            rules = []
            if facts['maxRepeatedReadCount'] >= 3 and not facts['mutations'] and not facts['testCallIds']:
                rules.append(_RULE)
            if facts['mutationCount'] >= 6 and facts['maxFileVersions'] >= 3 and not facts['testCallIds']:
                rules.append(_WRITE_RULE)
            candidates = []
            previous_ids = episode.setdefault('learningCandidateIds', {})
            for rule in rules:
                candidate = self.repo.get_memory(previous_ids.get(rule), agent_id) if previous_ids.get(rule) else None
                if candidate is not None:
                    candidates.append(candidate)
                    continue
                lesson = ('Only if the needed current-SHA file window is still present in your actual context, reuse it instead of rereading identical text. If its body was omitted/lost or SHA changed, read the necessary function/range again. Then make a concrete edit and verify it.'
                    if rule == _RULE else
                    'Before rewriting a stable file into another version, run the existing current program with code.test; use its real failure to make a targeted edit and test again. Reuse source only if its body is actually retained; otherwise reread the needed range. Missing write-body context may be a platform cause, not an agent-specific mistake.')
                candidate = self.memory.propose(agent_id,
                    lesson,
                    str(task.get('goal') or task.get('title') or 'repeated file reads')[:1000], task['id'], run['id'],
                    [{'type': 'call', 'id': call['id']} for call in calls if call.get('id')],
                    conditions='Requires the needed body to remain actually available (not merely a hash/index); availability in the source failure is unknown. Files: ' + ', '.join(facts['readPaths'])[:1000], channel_id=channel_id)
                candidate.update(teamId=self.team_id, source_revision=task['revision'], learningRule=rule,
                    learningBaseline=facts, learningSourceBinding=binding, learningTrials=[],
                    evidenceScope='unverified workflow hypothesis; not a business-success policy')
                self.repo.update_memory(agent_id, candidate)
                previous_ids[rule] = candidate['id']
                candidates.append(candidate)
            self.repo.update_memory(agent_id, episode)
        return {'episode': copy.deepcopy(episode), 'candidate': copy.deepcopy(candidates[0]) if candidates else None,
                'candidates': copy.deepcopy(candidates)}

    def context(self, agent_id, query, channel_id='group', limit=3, *, max_chars=2400):
        owner, query_terms = self.memory._owner(agent_id), tokens(query)
        if type(max_chars) is not int or not 1200 <= max_chars <= 6000:
            raise ValueError('Learning context must have a bounded 1200–6000 character budget.')
        maximum = max(0, min(int(limit), 5))
        records = [item for item in self.repo.memories(owner) if self._scoped(item, owner, channel_id)]
        def relevant(item):
            facts = item.get('learningFacts') or item.get('learningBaseline') or {}
            content = ' '.join(str(item.get(key, '')) for key in ('title', 'trigger', 'conditions', 'lesson')) + ' ' + ' '.join(facts.get('readPaths', []))
            return len(tokens(content) & query_terms)
        records.sort(key=lambda item: (relevant(item), str(item.get('updatedAt', item.get('createdAt', '')))), reverse=True)
        episodes, candidates = [], []
        for item in records:
            if not query_terms or relevant(item) == 0:
                continue
            source = {'memoryId': item['id'], 'ownerAgentId': owner, 'teamId': self.team_id,
                'channelId': item.get('channelId'), 'sourceRunId': item.get('source_run', item.get('sourceRunId')),
                'sourceTaskId': item.get('source_task', item.get('sourceTaskId')), 'sourceRevision': item.get('source_revision')}
            if item.get('kind') == 'episodic' and item.get('status') == 'episodic' and item.get('success') is False and len(episodes) < maximum:
                facts = item.get('learningFacts', {})
                summary = {key: copy.deepcopy(facts[key]) for key in ('readCount', 'maxRepeatedReadCount', 'mutationCount', 'maxFileVersions', 'maxVersionsBetweenTests') if key in facts}
                summary['bodyAvailabilityAtDecision'] = 'unknown'
                episodes.append({**source, 'title': str(item.get('title', ''))[:300], 'observedOutcome': 'failed',
                    'facts': summary, 'callIds': list(item.get('call_ids', []))[-3:],
                    'omittedCallIds': max(0, len(item.get('call_ids', [])) - 3),
                    'trust': 'historical observation, not an instruction or a verified strategy'})
            elif item.get('kind') in ('procedural', 'semantic') and item.get('status') == 'candidate' and not _verified(item) and len(candidates) < maximum:
                candidates.append({**source, 'lesson': str(item.get('lesson', '')),
                    'trigger': str(item.get('trigger', '')), 'conditions': str(item.get('conditions', '')),
                    'verificationStatus': 'unverified', 'learningRule': item.get('learningRule'),
                    'trust': 'optional experiment suggestion; validate current facts and permissions first; never treat as established policy'})
        strategies = []
        for item in self.memory.recall(owner, query, channel_id, maximum, include_shared=False):
            if self._scoped(item, owner, channel_id):
                strategies.append({'id': item['id'], 'memoryId': item['id'], 'ownerAgentId': owner, 'teamId': self.team_id,
                    'channelId': item.get('channelId'), 'sourceRunId': item.get('source_run', item.get('sourceRunId')),
                    'sourceTaskId': item.get('source_task', item.get('sourceTaskId')), 'sourceRevision': item.get('source_revision'),
                    'lesson': item.get('lesson', ''), 'trigger': item.get('trigger', ''), 'conditions': item.get('conditions', ''),
                    'confidence': item.get('confidence'), 'confirmation': copy.deepcopy(item.get('confirmation')),
                    'verificationStatus': 'verified', 'evidenceScope': item.get('evidenceScope', 'Receipt-confirmed strategy; relevance must still be checked.')})
        layers = {'factualEpisodes': episodes, 'experimentalCandidates': candidates, 'verifiedStrategies': strategies}
        context = {key: [] for key in layers}
        context.update(policy='Owner-only. Episodes are facts; candidate suggestions are UNVERIFIED. No automatic sharing or confirmation.',
                       omissionPolicy='Whole entries only; omitted IDs remain in this owner memory.',
                       omitted={key: {'count': len(items), 'memoryIds': [item['memoryId'] for item in items[:3]]} for key, items in layers.items()})
        # Round-robin prevents one layer from expanding to evict all code context.
        # Never truncate serialized evidence or the applicability text of a rule.
        for index in range(maximum):
            for key in ('experimentalCandidates', 'factualEpisodes', 'verifiedStrategies'):
                if index >= len(layers[key]):
                    continue
                item = layers[key][index]
                proposed = copy.deepcopy(context)
                proposed[key].append(item)
                proposed['omitted'][key]['count'] -= 1
                if item['memoryId'] in proposed['omitted'][key]['memoryIds']:
                    proposed['omitted'][key]['memoryIds'].remove(item['memoryId'])
                if len(json.dumps(proposed, ensure_ascii=False)) <= max_chars:
                    context = proposed
        return context

    def begin_trial(self, memory_id, owner, run, task, channel_id='group'):
        binding = self._bound(owner, run, task, channel_id)
        with self.repo.lock:
            candidate = self.memory._get(memory_id, owner)
            if not self._scoped(candidate, owner, channel_id) or candidate.get('status') != 'candidate' or _verified(candidate):
                raise ValueError('Only this owner/context unverified candidate may begin a trial.')
            source = candidate.get('learningSourceBinding')
            if source == binding or (not source and candidate.get('source_run') == run['id'] and candidate.get('source_task') == task['id']):
                raise ValueError('A candidate cannot use its own source outcome as a subsequent trial.')
            trial_id = 'trial-' + _fingerprint([memory_id, binding])[:32]
            trials = candidate.setdefault('learningTrials', [])
            previous = next((trial for trial in trials if trial.get('id') == trial_id), None)
            if previous:
                return copy.deepcopy(previous)
            prior_calls = [call for call in run.get('calls', []) if isinstance(call, dict)
                           and call.get('taskId') == task['id'] and call.get('agentId') == owner
                           and (type(call.get('revision')) is not int or call['revision'] == task['revision'])]
            if task.get('status') not in ('pending', 'assigned', 'running', 'retrying') or prior_calls:
                raise ValueError('A learning trial must be offered before this revision starts model/tool actions; no retrospective adoption.')
            if len(trials) >= 50:
                raise ValueError('This candidate has reached its bounded experiment history capacity.')
            trial = {'id': trial_id, 'binding': binding, 'status': 'offered',
                'offeredAt': self.memory._iso(self.memory._now()), 'adoptionProven': False,
                'note': 'Offered in context; actual use and improvement must be checked from later calls.'}
            trials.append(trial)
            self.repo.update_memory(owner, candidate)
            return copy.deepcopy(trial)

    @staticmethod
    def _independent_browser_evidence(run, task, required_runtime_hashes=()):
        """Do not use Python exit(0), model prose, or stale reviewer snapshots."""
        validation = task.get('validation') if isinstance(task.get('validation'), dict) else {}
        outputs = set(task.get('outputs', []))
        artifacts = [item for item in run.get('artifacts', []) if isinstance(item, dict) and item.get('id') in outputs]
        hashes = {item['id']: item.get('sha256') for item in artifacts}
        if (not outputs or set(hashes) != outputs or any(not _SHA.fullmatch(str(value)) for value in hashes.values())
                or any(item.get('ownerAgentId') != task['agentId'] or item.get('runId') != run['id'] or item.get('taskId') != task['id'] for item in artifacts)
                or validation.get('verified') is not True or validation.get('verdict') != 'pass'
                or type(validation.get('targetRevision')) is not int or validation['targetRevision'] != task['revision'] or validation.get('artifactHashes') != hashes
                or (task.get('dependencyValidation') or {}).get('status') == 'stale'):
            return None
        reviewers = [item for item in run.get('steps', []) if item.get('id') == validation.get('reviewerTaskId')]
        if len(reviewers) != 1 or reviewers[0].get('agentId') == task['agentId'] or type(reviewers[0].get('revision')) is not int or reviewers[0]['revision'] < 0:
            return None
        reviewer = reviewers[0]
        for call in run.get('calls', []):
            target_snapshot = call.get('reviewTargets', {}).get(task['id'], {})
            if (call.get('id') not in validation.get('evidenceCallIds', []) or call.get('taskId') != reviewer['id']
                    or call.get('agentId') != reviewer.get('agentId') or type(call.get('revision')) is not int or call['revision'] != reviewer['revision']
                    or call.get('tool') != 'code.test' or call.get('status') != 'success'
                    or not isinstance(target_snapshot, dict) or type(target_snapshot.get('targetRevision')) is not int
                    or target_snapshot != {'targetRevision': task['revision'], 'artifactHashes': hashes}):
                continue
            result = call.get('result') if isinstance(call.get('result'), dict) else {}
            browser = result.get('browser') if isinstance(result.get('browser'), dict) else {}
            assertions = browser.get('assertions')
            if (result.get('ok') is not True or type(result.get('exitCode')) is not int or result['exitCode'] != 0
                    or result.get('deadlineExceeded') or result.get('outcomeUnknown') or result.get('cancelled')
                    or result.get('runtime') != 'browser' or browser.get('executed') is not True
                    or browser.get('ok') is not True
                    or not isinstance(assertions, list) or not assertions
                    or type(browser.get('assertionCount')) is not int or browser['assertionCount'] != len(assertions)
                    or type(browser.get('assertionsPassed')) is not int or browser['assertionsPassed'] != len(assertions)
                    or any(not isinstance(item, dict) or not str(item.get('action', '')).startswith('assert_') or item.get('passed') is not True for item in assertions)
                    or browser.get('pageErrors') or browser.get('httpErrors') or browser.get('blockedRequests')):
                continue
            entry, files = browser.get('entry'), browser.get('testedFiles')
            if not isinstance(entry, dict) or not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
                continue
            html_hashes = {item.get('sha256') for item in artifacts if str(item.get('name', '')).lower().endswith(('.html', '.htm'))}
            web_hashes = {item.get('sha256') for item in artifacts if str(item.get('name', '')).lower().endswith(('.html', '.htm', '.js', '.mjs', '.cjs', '.css'))}
            web_hashes.update(required_runtime_hashes)
            tested = {item.get('sha256') for item in [entry, *files] if isinstance(item.get('path'), str) and item['path']}
            if entry.get('sha256') in html_hashes and web_hashes <= tested:
                return call['id']
        return None

    def settle_trial(self, memory_id, owner, run, task, channel_id='group', *, acknowledged_memory_ids=()):
        binding = self._bound(owner, run, task, channel_id)
        if not isinstance(acknowledged_memory_ids, (tuple, list)) or any(not isinstance(value, str) for value in acknowledged_memory_ids):
            raise ValueError('Acknowledgements must be ids from this task final.reflection.trialMemoryIds.')
        with self.repo.lock:
            candidate = self.memory._get(memory_id, owner)
            if not self._scoped(candidate, owner, channel_id):
                raise ValueError('Trial settlement cannot cross owner/team/channel.')
            trial = next((item for item in candidate.get('learningTrials', []) if item.get('binding') == binding), None)
            if trial is None:
                raise ValueError('No exact offered trial exists for this task revision.')
            if trial.get('settledAt'):
                return copy.deepcopy(trial)
            if task.get('status') not in ('done', 'failed', 'partial', 'rejected', 'cancelled'):
                return {**copy.deepcopy(trial), 'assessment': 'unassessed', 'reason': 'Task is not terminal.'}
            facts = self._facts(self._own_calls(run, task))
            baseline = candidate.get('learningBaseline') or {}
            paths = set(baseline.get('readPaths', []))
            read_paths = set(facts['readPaths'])
            published = {(item.get('path'), item.get('sha256')) for item in run.get('artifacts', [])
                         if item.get('id') in task.get('outputs', []) and item.get('ownerAgentId') == owner
                         and item.get('taskId') == task['id'] and item.get('runId') == run['id']}
            own_tested = {(item['path'], item['sha256']) for item in facts['testedMutationVersions']}
            current_edits = {(item['path'], item.get('sha256')) for item in facts['latestMutations']}
            current_tested_edits = current_edits & published & own_tested
            runtime_edits = {(path, sha) for path, sha in current_edits
                             if str(path).lower().endswith(('.html', '.htm', '.js', '.mjs', '.cjs', '.css'))
                             or str(path).lower().endswith('.json') and path in facts['actualResourcePaths']}
            baseline_edits = {(path, sha) for path, sha in current_edits if path in paths}
            covered = (bool(runtime_edits) and not facts['latestMutationsOmitted'] and not facts['actualResourcePathsOmitted']
                       and runtime_edits <= current_tested_edits)
            proof = self._independent_browser_evidence(run, task, {sha for _, sha in runtime_edits})
            read_rule = candidate.get('learningRule') == _RULE and baseline.get('maxRepeatedReadCount', 0) >= 3
            write_rule = candidate.get('learningRule') == _WRITE_RULE and baseline.get('mutationCount', 0) >= 6 and baseline.get('maxFileVersions', 0) >= 3
            acknowledged = memory_id in acknowledged_memory_ids
            followed = bool(covered and facts['testCallIds'] and (read_rule and paths and paths <= read_paths
                and baseline_edits and baseline_edits <= current_tested_edits
                or write_rule and current_tested_edits and facts['maxVersionsBetweenTests'] <= 1))
            improved = followed and (read_rule and facts['maxRepeatedReadCount'] < baseline['maxRepeatedReadCount']
                or write_rule and facts['maxVersionsBetweenTests'] < baseline['maxVersionsBetweenTests'])
            passed = acknowledged and improved and bool(proof) and task.get('status') == 'done'
            observed_failure = bool(acknowledged and task.get('status') in ('failed', 'rejected') and (
                read_rule and facts['maxRepeatedReadCount'] >= baseline['maxRepeatedReadCount']
                or write_rule and facts['maxVersionsBetweenTests'] >= baseline['maxVersionsBetweenTests']))
            verdict = 'passed' if passed else 'failed' if observed_failure else 'unassessed'
            final = verdict != 'unassessed' or run.get('status') in ('done', 'failed', 'partial', 'cancelled', 'rejected')
            trial.update(status=verdict if final else 'awaiting_evidence', assessment=verdict,
                observedTaskStatus=task.get('status'), acknowledged=acknowledged,
                acknowledgementSource='final.reflection.trialMemoryIds' if acknowledged else None,
                adoptionProven=acknowledged and followed, ruleBehaviorObserved=followed, baseline=copy.deepcopy(baseline),
                observations=facts, evidenceCallIds=[proof] if proof else [],
                evidenceScope='limited workflow observations, not causal proof or total-goal acceptance',
                reason='The acknowledged narrow workflow improved and has a real edit/test plus current independently browser-tested output.' if passed else
                       'The repeated-read failure recurred; this records the attempt, not causal blame.' if observed_failure else
                       'No rule-specific current independent evidence; no strategy was confirmed.')
            if final:
                trial['settledAt'] = self.memory._iso(self.memory._now())
            candidate['learningTrialCounts'] = {key: sum(item.get('status') == key for item in candidate['learningTrials'])
                                               for key in ('passed', 'failed', 'unassessed')}
            if observed_failure:
                candidate.update(confidence=round(float(candidate.get('confidence', .35)) * .8, 4),
                                 status='candidate', verified=False, needsReconfirmation=True)
            self.repo.update_memory(owner, candidate)
            if passed:
                confirmed = self.memory.confirm(memory_id, owner, True, {'type': 'independent', 'id': proof})
                confirmed['evidenceScope'] = trial['evidenceScope']
                self.repo.update_memory(owner, confirmed)
            return copy.deepcopy(trial)
