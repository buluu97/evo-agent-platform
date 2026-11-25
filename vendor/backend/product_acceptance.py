"""Evaluator-owned Mid-Test product rubric. Presence/HTTP/model claims never prove business quality.

External assessments are trusted only as independent-operator attestations with exact
export/task/file bindings and hashed supporting files outside agent workspaces. They
are not platform Reviewer calls. SHA256 checks integrity, not assessor identity;
the operator must control the evidence directory. No generated code is executed here.
"""
import datetime as dt
import hashlib
import io
import json
import math
from pathlib import Path
import stat
import zipfile

from .delivery import validate_delivery, delivery_zip
from .evaluation import Check, EvidenceError

RUBRIC_VERSION = 'mid-test-products-v1.3'


def rule(check_id, label, method, observations, *, published=False, html=False, delivery=False):
    return {'id': check_id, 'label': label, 'method': method,
            'observations': observations.split(), 'published': published, 'html': html, 'delivery': delivery}


RUBRICS = {
 'software-1': [
  rule('S1.delivery', 'Current downloadable runnable product and use instructions', 'structural', '', delivery=True),
  rule('S1.booking', 'Actual booking and conflict rules in the confirmed single-browser scope', 'docker_browser', 'availability booking_created blank_name_rejected duplicate_slot_rejected'),
  rule('S1.cancel', 'Current-name ownership, cancellation state after refresh and rebooking', 'docker_browser', 'other_name_rejected owner_can_cancel panel_name_change cancel_frees_slot cancellation_state_persists rebook_after_cancel'),
  rule('S1.versioned_qa', 'Independent acceptance bound to the current source revision', 'docker_browser', 'current_files_tested no_page_errors known_limits_reported')],
 'software-2': [
  rule('S2.continuity', 'Continue Task 1 and use its existing booking records', 'docker_browser', 'same_booking_store prior_bookings_preserved task1_workflow_still_operates'),
  rule('S2.metrics', 'Independent recomputation of counts/hours/peaks/cancellation', 'browser_calculation', 'counts_hours peaks cancellation_separate date_boundaries'),
  rule('S2.interaction', 'Selected date range, responsive charts and empty-data handling', 'docker_browser', 'selected_date_range charts_update empty_data'),
  rule('S2.delivery_qa', 'Current dashboard product, use instructions and real browser QA', 'docker_browser', 'current_files_tested usable_dashboard known_limits_reported', delivery=True)],
 'newsroom-1': [
  rule('N1.story', 'Specific recent Clear Water Bay HKUST AI event and audience relevance', 'manual_editorial', 'specific_event event_date_recent campus_relevance audience_relevance'),
  rule('N1.facts', 'Independently checked sources and material factual claims', 'manual_source_audit', 'sources_opened claim_source_mapping dates_names_quotes_checked unsupported_claims_flagged'),
  rule('N1.channels', 'Usable neutral web story and social post in confirmed scope', 'manual_editorial', 'web_story social_post approximately_800_words neutral_tone publication_readiness'),
  rule('N1.review', 'Independent fact-check and editorial conclusion on current files', 'manual_source_audit', 'current_files_reviewed material_facts_checked uncertainty_explained')],
 'newsroom-2': [
  rule('N2.article', 'Fact-check article addresses the exact reliability claim for students/faculty', 'manual_editorial', 'exact_claim interpretation audience_article'),
  rule('N2.evidence', 'Empirical detector evidence and study/source validity independently checked', 'manual_source_audit', 'sources_opened study_methods population_ground_truth evidence_limitations'),
  rule('N2.errors', 'False positives/negatives, applicability and uncertainty assessed', 'manual_source_audit', 'false_positives false_negatives university_applicability uncertainty'),
  rule('N2.verdict', 'Verdict proportional to evidence, with remaining uncertainty', 'manual_editorial', 'evidence_supports_verdict uncertainty_remaining responsible_use')],
 'fund-1': [
  rule('F1.assumptions', 'Age/capital and explicit authorized simulation assumptions', 'manual_editorial', 'age_25 capital_hkd_1000000 us_stocks simulation_label objectives_horizon_risk_assumptions'),
  rule('F1.allocation', 'Allocation, currency conversions and capital independently reconciled', 'independent_calculation', 'allocation_matches_report currency_fx_asof cash_fees_assumptions totals_reconciled'),
  rule('F1.research_risk', 'Research basis and material investment risks checked independently', 'manual_source_audit', 'sources_opened allocation_rationale concentration_currency_market_risks no_return_guarantee'),
  rule('F1.introduction', 'Understandable simulated proposal and explicit limitations', 'manual_editorial', 'plain_language allocation_explained risks_explained assumptions_limits no_real_trades')],
 'fund-2': [
  rule('F2.slides', 'Actual HTML slides open and present slide by slide', 'docker_browser', 'opens_as_html multiple_slides next_previous_navigation readable_layout', published=True, html=True),
  rule('F2.time_sources', 'End-2026 viewpoint, dated sources and facts/forecasts separated', 'manual_source_audit', 'end_2026_viewpoint source_asof_checked facts_vs_forecasts future_uncertainty'),
  rule('F2.analysis', 'Researched semiconductor judgments, investment implications and risks', 'manual_source_audit', 'industry_evidence judgments_supported investment_implications key_risks'),
  rule('F2.delivery_qa', 'Current presentation files, use instructions and browser acceptance', 'docker_browser', 'current_files_tested presentation_use_steps known_limits_reported', published=True, html=True)]}


def evidence_file(root, reference, limit=8 * 1024 * 1024):
    """Only evaluator-owned bounded regular files; never agent workspace evidence by reference."""
    if not isinstance(reference, dict) or not isinstance(reference.get('path'), str):
        raise EvidenceError('A hashed evaluator-owned supporting file is required.')
    relative = Path(reference['path'])
    root = Path(root).resolve()
    if relative.is_absolute() or '..' in relative.parts or any(part.startswith('.') for part in relative.parts):
        raise EvidenceError('Evidence file paths must be visible relative paths.')
    path = root / relative
    if any(part in ('workspaces', 'imports') for part in path.parts):
        raise EvidenceError('Copy independent evidence outside agent-controlled workspaces/imports.')
    cursor = root
    for part in relative.parts:
        cursor /= part
        if cursor.is_symlink(): raise EvidenceError('Symlinked evidence is forbidden.')
    if not path.resolve().is_relative_to(root): raise EvidenceError('Evidence path escapes its directory.')
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > limit:
            raise EvidenceError('Evidence must be a bounded, non-linked regular file.')
        raw = path.read_bytes()
    except OSError as error: raise EvidenceError('Supporting evidence file is unavailable.') from error
    if len(raw) > limit or hashlib.sha256(raw).hexdigest() != reference.get('sha256'):
        raise EvidenceError('Supporting evidence SHA256 mismatch.')
    return raw


def usage_metrics(bookings, start, end, rooms=None):
    """Confirmed inclusive booking-date rule, active usage, separate cancellations; 1-hour slots."""
    first, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if first > last: raise ValueError('Reversed date range.')
    if not isinstance(bookings, list) or len(bookings) > 10000: raise ValueError('Expected bounded independent booking records.')
    if rooms is not None and (not isinstance(rooms, list) or any(not isinstance(room, str) for room in rooms) or len(set(rooms)) != len(rooms)):
        raise ValueError('Expected distinct room identifiers.')
    per_room, dates, weekdays, hours, seen = {str(room): 0 for room in (rooms or [])}, {}, {}, {}, set()
    cancelled = active = 0
    for record in bookings:
        if not isinstance(record, dict) or not isinstance(record.get('id'), str) or record['id'] in seen:
            raise ValueError('Independent booking input needs unique record IDs.')
        seen.add(record['id'])
        date = dt.date.fromisoformat(record['date'])
        if not first <= date <= last: continue
        if record.get('status') == 'cancelled': cancelled += 1; continue
        if record.get('status') != 'active': raise ValueError('Unknown booking status.')
        slot = record.get('slot')
        hour = slot + 8 if type(slot) is int else int(str(slot).split(':')[0])
        if not 8 <= hour < 20: raise ValueError('Invalid hourly slot.')
        room = str(record['roomId']); active += 1
        per_room[room] = per_room.get(room, 0) + 1
        dates[record['date']] = dates.get(record['date'], 0) + 1
        weekdays[str(date.weekday())] = weekdays.get(str(date.weekday()), 0) + 1
        hours[str(hour)] = hours.get(str(hour), 0) + 1
    def peaks(values):
        return sorted(key for key, count in values.items() if count == max(values.values())) if values else []
    return {'activeCount': active, 'totalHours': active, 'perRoom': per_room,
            'cancelledCount': cancelled, 'cancellationRate': cancelled / (active + cancelled) if active + cancelled else None,
            'busiestDates': peaks(dates), 'busiestWeekdays': peaks(weekdays), 'busiestHours': peaks(hours)}


def allocation_issues(calculation):
    def number(value): return type(value) in (int, float) and math.isfinite(value)
    rows = calculation.get('allocations')
    if calculation.get('capitalHKD') != 1000000 or not isinstance(rows, list) or not rows:
        return ['Expected HKD 1,000,000 and explicit allocations including cash.']
    issues = []
    for row in rows:
        if not isinstance(row, dict) or not number(row.get('weightPct')) or not number(row.get('amountHKD')) or row['weightPct'] < 0 or row['amountHKD'] < 0:
            return ['Allocation weights and HKD amounts must be finite nonnegative numbers.']
        if abs(row['amountHKD'] - 1000000 * row['weightPct'] / 100) > 1:
            issues.append('Allocation amount differs from its stated percentage.')
        if 'amountUSD' in row:
            fx = calculation.get('fxHKDPerUSD')
            if not number(fx) or fx <= 0 or not number(row['amountUSD']) or abs(row['amountUSD'] * fx - row['amountHKD']) > 1:
                issues.append('USD/HKD conversion does not reconcile.')
    if abs(sum(row['weightPct'] for row in rows) - 100) > .01: issues.append('Weights do not sum to 100%.')
    if abs(sum(row['amountHKD'] for row in rows) - 1000000) > 1: issues.append('Allocations do not reconcile to capital.')
    return issues


class ProductVerifier:
    def __init__(self, case, proof=None, proof_root=None):
        if proof is not None and not isinstance(proof, dict): raise ValueError('Independent proof must be a JSON object.')
        self.case, self.proof = case, proof or {}
        self.proof_root = Path(proof_root or '.')
        self.rules = RUBRICS[case['caseId']]
        self.check_ids = tuple(row['id'] for row in self.rules)
        self.operator_assisted = False

    def _bindings(self, evidence, row, rule):
        refs = row.get('bindings')
        if not isinstance(refs, list) or not refs: raise EvidenceError('Assessment requires exact current task/file bindings.')
        tasks, ids, hashes, bound = set(), [], set(), []
        for ref in refs:
            task = evidence.task(ref)
            if ref.get('kind') == 'task_result':
                if rule['published'] or rule['delivery'] or rule['method'] in ('docker_browser', 'browser_calculation'):
                    raise EvidenceError('A prose task result cannot substitute for actual software/HTML source files.')
                text = task.get('result')
                if not isinstance(text, str) or not text.strip(): raise EvidenceError('The frozen task has no actual result text.')
                digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
                if ref.get('sha256') != digest: raise EvidenceError('Frozen task result SHA256 mismatch.')
                ids.append('task-result:' + task['taskId'] + ':' + str(task['revision']) + ':' + digest)
                hashes.add(digest)
            elif ref.get('artifactId'):
                item = evidence.load_artifact(ref['artifactId'])
                if not item: raise EvidenceError('Bound artifact is unavailable.')
                _, item = evidence.artifact(ref, item['name'])
                if (rule['published'] or rule['delivery']) and item.get('sourceRunId'):
                    raise EvidenceError('This deliverable check needs its own current new published files, not only inherited snapshots.')
                bound.append(item)
                ids.append(item['id']); hashes.add(item['sha256'])
            else:
                if rule['published'] or rule['delivery']:
                    raise EvidenceError('An unpublished prototype cannot satisfy this published-deliverable check.')
                raw = evidence_file(self.proof_root, ref.get('snapshot'))
                digest = hashlib.sha256(raw).hexdigest()
                if digest != ref.get('sha256'): raise EvidenceError('Prototype snapshot hash mismatch.')
                history = [call for call in evidence.run.get('rawRun', {}).get('calls', [])
                    if call.get('agentId') == task.get('ownerAgentId') and call.get('status') == 'success'
                    and (call.get('args') or {}).get('path') == ref.get('path')]
                mutations = [call for call in history if call.get('tool') in ('file.write', 'file.patch', 'file.delete')]
                if mutations and (mutations[-1].get('tool') == 'file.delete'
                        or mutations[-1].get('taskId') != task['taskId'] or mutations[-1].get('revision') != task['revision']
                        or (mutations[-1].get('result') or {}).get('sha256') != digest):
                    raise EvidenceError('Prototype was deleted, superseded, or changed by a later task/revision.')
                calls = [call for call in history if call.get('taskId') == task['taskId']
                    and type(call.get('revision')) is int and call['revision'] == task['revision']
                    and call.get('tool') in ('file.write', 'file.patch', 'file.read')]
                if not calls or (calls[-1].get('result') or {}).get('sha256') != digest:
                    raise EvidenceError('Unpublished prototype lacks a matching latest successful file call/hash.')
                ids.append('prototype:' + ref['path']); hashes.add(digest)
            tasks.add(task['taskId'])
        if rule['html'] and not any(str(item.get('name', '')).lower().endswith('.html') for item in bound):
            raise EvidenceError('The assessed bindings must include the actual published HTML entry.')
        return tuple(sorted(tasks)), tuple(ids), hashes, bound

    def _delivery(self, evidence):
        raw = evidence.run.get('rawRun', {})
        class Store:
            def get(self, artifact_id): return evidence.load_artifact(artifact_id)
        errors = []
        for delivery in reversed(raw.get('deliveries', [])):
            if delivery.get('status') == 'candidate':
                continue
            try:
                validate_delivery(delivery, raw, Store(), current=True)
                archive = delivery_zip(delivery, raw, Store())
                with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
                    if zipped.testzip() or not {'RUN.txt', 'DELIVERY.json', delivery['entrypoint']}.issubset(zipped.namelist()):
                        raise ValueError('Invalid product archive.')
                task_id = delivery['taskId']
                tasks = (task_id,) if task_id in evidence.tasks else ()
                return Check('pass', 'Current immutable ZIP, entrypoint, file bytes/hashes and use instructions reconcile; this check is structural only.',
                             tasks, (delivery['id'],), details={'reconstructedZipSha256': hashlib.sha256(archive).hexdigest(),
                                 'files': len(delivery['files']), 'artifactHashes': {item['artifactId']: item['sha256'] for item in delivery['files']},
                                 'entrypointSha256': next(item['sha256'] for item in delivery['files'] if item['path'] == delivery['entrypoint'])})
            except (ValueError, KeyError, TypeError, EvidenceError) as error: errors.append(str(error))
        verdict = 'fail' if evidence.run.get('status') in ('done', 'partial', 'failed', 'crashed', 'timeout', 'cancelled') else 'unassessed'
        return Check(verdict, 'No valid current published product package. ' + '; '.join(errors), error_code='missing_or_invalid_delivery')

    def _assessment(self, evidence, rule):
        proof = self.proof
        if (proof.get('schemaVersion') != 1 or proof.get('provenance') != 'independent_evaluator'
                or not isinstance(proof.get('assessor'), str) or not proof['assessor'].strip()
                or proof.get('caseId') != self.case['caseId'] or proof.get('runId') != evidence.run['runId']
                or proof.get('exportSha256') != evidence.run.get('exportSha256')):
            raise EvidenceError('No independent evaluator assessment bound to this exact frozen export.')
        row = proof.get('checks', {}).get(rule['id'])
        if not isinstance(row, dict) or row.get('method') != rule['method']:
            raise EvidenceError('Missing independent method-specific assessment; model prose/HTTP success are not acceptance.')
        tasks, ids, hashes, bound = self._bindings(evidence, row, rule)
        supporting = row.get('evidenceFiles')
        if not isinstance(supporting, list) or not supporting: raise EvidenceError('Hashed supporting evidence files are required.')
        for ref in supporting: evidence_file(self.proof_root, ref)
        observations = row.get('observations', {})
        selected = []
        required_observations = list(rule['observations'])
        if rule['id'] == 'S2.interaction' and any(item.get('deliveredToRun') == evidence.run['runId'] for item in self.case.get('verifiedRequirements', [])):
            required_observations += ['default_30_days', 'presets_7_30_90', 'custom_range', 'offline_charts']
        for name in required_observations:
            observation = observations.get(name)
            if not isinstance(observation, dict) or observation.get('verdict') not in ('pass', 'fail', 'unassessed') or not isinstance(observation.get('detail'), str) or not observation['detail'].strip():
                raise EvidenceError('Missing independently explained observation: ' + name)
            selected.append(observation)
        verdict = 'fail' if any(item['verdict'] == 'fail' for item in selected) else 'pass' if all(item['verdict'] == 'pass' for item in selected) else 'unassessed'
        if rule['method'] in ('docker_browser', 'browser_calculation'):
            browser = row.get('browser')
            if not isinstance(browser, dict) or browser.get('engine') != 'chromium' or browser.get('executed') is not True:
                raise EvidenceError('Actual isolated Chromium execution evidence is required.')
            if not hashes.issubset(set(browser.get('testedFileHashes', []))):
                raise EvidenceError('Browser did not test all bound source hashes.')
            if rule['html'] and (browser.get('entry') or {}).get('sha256') not in {item['sha256'] for item in bound if str(item.get('name', '')).lower().endswith('.html')}:
                raise EvidenceError('Browser entry is not the bound current HTML deliverable.')
            if rule['delivery']:
                package = self._delivery(evidence)
                packaged_hashes = (package.details or {}).get('artifactHashes', {})
                if package.verdict != 'pass' or any(packaged_hashes.get(item['id']) != item['sha256'] for item in bound):
                    raise EvidenceError('Browser QA files do not match the current packaged sources.')
                if (browser.get('entry') or {}).get('sha256') != package.details['entrypointSha256']:
                    raise EvidenceError('Browser QA did not open the current package entrypoint.')
            if (not isinstance(browser.get('assertions'), list) or not browser['assertions']
                    or any(not isinstance(item, dict) or type(item.get('passed')) is not bool for item in browser['assertions'])):
                raise EvidenceError('Browser execution needs actual assertion details.')
            if any(item['passed'] is not True for item in browser['assertions']) or browser.get('pageErrors') or browser.get('httpErrors') or browser.get('blockedRequests'):
                verdict = 'fail'
        if rule['id'] == 'S2.continuity':
            raw = evidence.run.get('rawRun', {})
            if not self.case.get('previousRunId') or raw.get('previousRunId') != self.case['previousRunId']:
                return Check('fail', 'Task 2 is not bound to the observed Task 1 run in the same group.', tasks, ids, 'continuity')
        if rule['id'] == 'S2.metrics':
            calc = row.get('calculation', {})
            try: expected = usage_metrics(calc['bookings'], calc['start'], calc['end'], calc.get('rooms'))
            except (KeyError, TypeError, ValueError): raise EvidenceError('Independent booking/date calculation input is missing or invalid.')
            if calc.get('observed') != expected: verdict = 'fail'
        if rule['id'] == 'F1.allocation':
            calc = row.get('calculation')
            if not isinstance(calc, dict): raise EvidenceError('Independent allocation calculation is required.')
            if allocation_issues(calc): verdict = 'fail'
        self.operator_assisted = True
        # An operator's product audit never counts as the platform QA Agent completing its task.
        reviewer = evidence.run.get('roleBindings', {}).get('reviewer')
        tasks = tuple(task_id for task_id in tasks if evidence.tasks[task_id].get('ownerAgentId') != reviewer)
        return Check(verdict, 'Independent evaluator: ' + '; '.join(item['detail'] for item in selected), tasks,
                     ids + tuple('evidence:' + ref['sha256'] for ref in supporting),
                     None if verdict != 'fail' else 'product_acceptance', {'operatorAssisted': True, 'method': rule['method'],
                         'evidenceFiles': supporting, 'observations': {name: observations[name] for name in required_observations},
                         'diagnosticFindings': row.get('diagnosticFindings', [])})

    def evaluate(self, evidence):
        results = {}
        for rule in self.rules:
            if rule['id'] == 'S1.delivery': results[rule['id']] = self._delivery(evidence); continue
            if rule['delivery']:
                packaged = self._delivery(evidence)
                if packaged.verdict != 'pass': results[rule['id']] = packaged; continue
            if rule['published']:
                artifacts = evidence.run.get('rawRun', {}).get('artifacts', [])
                candidates = [item for item in artifacts if not item.get('sourceRunId') and (not rule['html'] or str(item.get('name', '')).lower().endswith('.html'))]
                if not candidates:
                    results[rule['id']] = Check('fail', 'The terminal run has not published the required current deliverable.', error_code='missing_deliverable'); continue
            try: results[rule['id']] = self._assessment(evidence, rule)
            except (EvidenceError, OSError, ValueError, TypeError, KeyError) as error:
                results[rule['id']] = Check('unassessed', str(error))
        return results


def platform_qa_evidence(run):
    """Only observed platform review calls; independent operator files never enter this list."""
    raw, result = run.get('rawRun', {}), []
    reviewer = run.get('roleBindings', {}).get('reviewer')
    for call in raw.get('calls', []):
        receipt = call.get('result')
        if call.get('agentId') != reviewer or call.get('tool') != 'code.test' or call.get('status') != 'success' or not isinstance(receipt, dict): continue
        if receipt.get('runtime') == 'browser' and receipt.get('ok') is True and (receipt.get('browser') or {}).get('executed') is True:
            result.append({'callId': call['id'], 'taskId': call.get('taskId'), 'revision': call.get('revision'),
                           'reviewTargets': call.get('reviewTargets', {}), 'kind': 'observed_browser_call_not_automatic_semantic_pass'})
    return result
