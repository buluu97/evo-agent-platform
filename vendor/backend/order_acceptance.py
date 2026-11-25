"""CSV order checks driven by evaluator-owned fixtures and trusted receipts."""

from hashlib import sha256
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from .evaluation import Check, EvidenceError, local_file, object_from_json
from .sandbox import SandboxManager, ToolError, read_text, write_text


FIELDS = ('total_rows', 'valid_rows', 'missing_amount_rows', 'invalid_amount_rows', 'total_amount_cents')


def read_utf8(path):
    return path.read_bytes().decode('utf-8')


def read_summary(content):
    value = object_from_json(content)
    if not isinstance(value, dict) or set(value) != set(FIELDS) or any(type(n) is not int for n in value.values()):
        raise ValueError('The summary must contain exactly the five integer order statistics.')
    return value


def compare(actual, expected):
    differences = [f'{key} expected {expected[key]}, received {actual[key]}'
                   for key in FIELDS if actual[key] != expected[key]]
    return '; '.join(differences)


def public_url(value):
    if not isinstance(value, str):
        return False
    try:
        url = urlsplit(value)
        return url.scheme in ('http', 'https') and bool(url.hostname) and not (url.username or url.password)
    except ValueError:
        return False


def report_sections(report):
    sections = {}
    matches = list(re.finditer(r'^#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$', report, re.M))
    for match, following in zip(matches, [*matches[1:], None]):
        name = match[1].strip().casefold()
        if name in sections:
            raise ValueError(f'Duplicate report section {name!r}.')
        sections[name] = report[match.end():following.start() if following else None].strip()
    return sections


class OrdersVerifier:
    check_ids = ('S1', 'S2', 'S3', 'S4')

    def __init__(self, case, directory, docker_image):
        self.main_input = read_utf8(local_file(directory, case['inputPath']))
        self.main_expected = read_summary(read_utf8(local_file(directory, case['expectedPath'])))
        self.review_expected = read_summary(read_utf8(local_file(directory, case['reviewExpectedPath'])))
        self.review_input = read_utf8(local_file(directory, case['reviewInputPath']))
        self.review_hash = sha256(self.review_input.encode('utf-8')).hexdigest()
        if self.review_hash == sha256(self.main_input.encode('utf-8')).hexdigest():
            raise ValueError('The Reviewer input must differ from the main input.')
        self.method_patterns = tuple(re.compile(pattern, re.I) for pattern in case['methodPatterns'])
        if not self.method_patterns:
            raise ValueError('The case must specify the required report method details.')
        self.docker_image = docker_image

    def evaluate(self, evidence):
        bindings = evidence.run.get('evidence', {})
        s1 = self.check(evidence, bindings.get('research'), 'research', self.research)
        s2 = self.check(evidence, bindings.get('script'), 'executor', self.executor)
        s3 = self.check(evidence, bindings.get('review'), 'reviewer', self.reviewer)
        s4 = self.check(evidence, bindings.get('report'), 'coordinator',
                        lambda state, ref, tasks: self.report(state, ref, tasks, s3))
        return dict(zip(self.check_ids, (s1, s2, s3, s4)))

    @staticmethod
    def check(evidence, ref, owner, assess):
        tasks = ()
        try:
            task = evidence.task(ref, owner)
            tasks = (task['taskId'],)
            return assess(evidence, ref, tasks)
        except EvidenceError as error:
            return Check('unassessed', str(error), tasks)
        except ValueError as error:
            return Check('fail', str(error), tasks, error_code='invalid_output')

    @staticmethod
    def research(evidence, ref, tasks):
        task = evidence.tasks[tasks[0]]
        call_id = ref.get('callId')
        if (not call_id or ref.get('tool') not in ('web.search', 'web.fetch')
                or ref.get('status') != 'success'):
            raise EvidenceError('A successful Research search or fetch receipt is required.')
        source = ref.get('sourceUrl')
        if not public_url(source) or source not in ref.get('returnedUrls', []):
            return Check('fail', 'The cited public source was absent from the successful tool receipt.',
                         tasks, (call_id,), 'research_source')
        conclusion = ref.get('conclusion', '').strip()
        source_text = ref.get('sourceText', '')
        topic = r'\bcsv\b|comma[- ]separated|dictreader'
        if (not re.search(topic, conclusion, re.I) or not re.search(topic, source_text, re.I)
                or len(re.findall(r'[A-Za-z]+', conclusion)) < 6
                or conclusion not in task.get('result', '')):
            return Check('fail', 'Research must record a CSV-related finding in its task output.',
                         tasks, (call_id,), 'research_finding')
        return Check('pass', 'Research recorded a successful tool call, public source and CSV finding.',
                     tasks, (call_id,))

    def executor(self, evidence, ref, tasks):
        _, script = evidence.artifact(ref, 'summarize_orders.py', 'executor')
        _, submitted = evidence.artifact(evidence.run['evidence'].get('summary'), 'summary.json', 'executor')
        if submitted['taskId'] != script['taskId']:
            raise EvidenceError('The main summary and script must belong to the evaluated Executor revision.')
        ids = (script['id'], submitted['id'])
        try:
            submitted_difference = compare(read_summary(submitted['content']), self.main_expected)
        except ValueError as error:
            submitted_difference = str(error)
        actual, receipt, error = self.rerun(script, self.main_input)
        ids += (receipt,) if receipt else ()
        details = {'scriptSha256': script['sha256'], 'inputSha256': sha256(self.main_input.encode('utf-8')).hexdigest(),
                   'summarySha256': submitted['sha256'], 'rerunSandboxId': receipt,
                   'expected': self.main_expected, 'observed': actual}
        if submitted_difference:
            return Check('fail', f'Submitted summary: {submitted_difference}', tasks, ids, 'summary_mismatch', details)
        if error:
            verdict, code, reason = error
            return Check(verdict, reason, tasks, ids, code, details)
        difference = compare(actual, self.main_expected)
        if difference:
            return Check('fail', f'Independent rerun: {difference}.', tasks, ids, 'rerun_mismatch', details)
        return Check('pass', 'The registered summary and isolated main rerun both match the five expected values.',
                     tasks, ids, details=details)

    def rerun(self, script, input_text):
        receipt = None
        try:
            with TemporaryDirectory(prefix='evomac-acceptance-') as root:
                sandbox = SandboxManager(SimpleNamespace(data_dir=Path(root), docker_image=self.docker_image))
                run_id = 'accept-' + uuid4().hex
                workspace = sandbox.workspace('reviewer', run_id)
                imports = sandbox.imports('reviewer', run_id)
                write_text(workspace, 'orders.csv', input_text)
                write_text(imports, 'summarize_orders.py', script['content'])
                result = sandbox.run('reviewer', run_id, {
                    'path': 'imports/summarize_orders.py', 'args': ['orders.csv', 'summary.json'], 'timeout': 20,
                }, test=True)
                receipt = result['sandboxId']
                if result.get('timedOut'):
                    return None, receipt, ('unassessed', 'verifier_timeout', 'The isolated rerun reached its deadline.')
                if not result.get('ok'):
                    if result['exitCode'] in (125, 126, 127):
                        return None, receipt, ('unassessed', 'sandbox_unavailable', 'Docker could not start the verifier workload.')
                    return None, receipt, ('fail', 'script_error', 'The script failed during the isolated rerun.')
                try:
                    content = read_text(workspace, 'summary.json')
                    return read_summary(content), receipt, None
                except (ToolError, ValueError):
                    return None, receipt, ('fail', 'invalid_rerun_output', 'The isolated script did not produce a valid summary.json.')
        except (ToolError, OSError) as error:
            return None, receipt, ('unassessed', 'sandbox_unavailable', f'Isolated verification was unavailable: {error}')

    def reviewer(self, evidence, ref, tasks):
        script_ref = evidence.run.get('evidence', {}).get('script')
        _, script = evidence.artifact(script_ref, 'summarize_orders.py', 'executor')
        target = ref.get('target', {})
        if any(target.get(key) != script_ref[key] for key in ('taskId', 'revision', 'artifactId', 'sha256')):
            raise EvidenceError('The Reviewer receipt targets a different script or Executor revision.')
        call_id = ref.get('callId')
        if (not call_id or type(ref.get('exitCode')) is not int or ref['exitCode'] != 0
                or ref.get('tool') != 'code.test' or ref.get('status') != 'success'
                or not ref.get('sandboxId') or ref.get('readOnly') is not True
                or ref.get('inputSha256') != self.review_hash):
            raise EvidenceError('A successful Reviewer sandbox call on the read-only script and independent input is required.')
        _, result = evidence.artifact(ref.get('result'), 'review_summary.json', 'reviewer')
        if result['taskId'] != tasks[0]:
            raise EvidenceError('The review output must belong to the same Reviewer revision as the call.')
        ids = (call_id, ref['sandboxId'], script['id'], result['id'])
        try:
            difference = compare(read_summary(result['content']), self.review_expected)
        except ValueError as error:
            return Check('fail', f'Invalid Reviewer summary. {error}', tasks, ids, 'review_output')
        if difference:
            return Check('fail', f'Independent review: {difference}.', tasks, ids, 'review_mismatch')
        actual, receipt, error = self.rerun(script, self.review_input)
        ids += (receipt,) if receipt else ()
        if error:
            verdict, code, reason = error
            return Check(verdict, reason, tasks, ids, code)
        details = {'scriptSha256': script['sha256'], 'inputSha256': self.review_hash,
                   'reviewOutputSha256': result['sha256'], 'rerunSandboxId': receipt,
                   'expected': self.review_expected, 'observed': actual}
        difference = compare(actual, self.review_expected)
        if difference:
            return Check('fail', f'Independent review rerun: {difference}.', tasks, ids, 'review_rerun_mismatch', details)
        return Check('pass', 'The Reviewer result and a fresh isolated rerun match the independent input and current script.',
                     tasks, ids, details=details)

    def report(self, evidence, ref, tasks, review):
        _, report = evidence.artifact(ref, 'report.md', 'coordinator')
        ids = (report['id'],)
        try:
            sections = report_sections(report['content'])
        except ValueError as error:
            return Check('fail', str(error), tasks, ids, 'report_content')
        details = []
        method = sections.get('method', '')
        if not all(pattern.search(method) for pattern in self.method_patterns):
            details.append('the case counting rules in Method')
        results = sections.get('results', '')
        values = re.findall(r'^[ \t]*(?:[-*][ \t]+)?`?([a-z_]+)`?[ \t]*:[ \t]*`?(-?[0-9]+)`?[ \t]*$',
                            results, re.M)
        if any([value for field, value in values if field == key] != [str(expected)]
               for key, expected in self.main_expected.items()):
            details.append('the five expected values in Results')
        source = evidence.run.get('evidence', {}).get('research', {}).get('sourceUrl')
        if not source or source not in sections.get('sources', ''):
            details.append('the Research citation in Sources')
        outcome = {'pass': 'pass', 'fail': 'fail', 'unassessed': 'unassessed'}[review.verdict]
        matches_review = re.match(rf'^\s*(?:[-*]\s+)?(?:Verdict:\s*)?{outcome}\b',
                                  sections.get('review', ''), re.I)
        if not matches_review and review.verdict != 'unassessed':
            details.append('the independently assessed outcome in Review')
        if details:
            return Check('fail', 'The report is missing or misstating ' + ', '.join(details) + '.',
                         tasks, ids, 'report_content')
        if not matches_review:
            return Check('unassessed', 'The report review claim needs independently assessed evidence.', tasks, ids)
        return Check('pass', 'The Markdown report contains the counting rules, expected values, source and review outcome.',
                     tasks, ids)


class ExactJsonVerifier:
    """A data-only verifier for cases with a single exact JSON deliverable."""

    check_ids = ('exact-json',)

    def __init__(self, case, directory, docker_image):
        self.expected = object_from_json(read_utf8(local_file(directory, case['expectedPath'])))
        self.filename = case['outputName']

    def evaluate(self, evidence):
        ref = evidence.run.get('evidence', {}).get('output')

        def compare_output(state, binding, tasks):
            _, artifact = state.artifact(binding, self.filename)
            try:
                actual = object_from_json(artifact['content'])
                matches = json.dumps(actual, sort_keys=True) == json.dumps(self.expected, sort_keys=True)
            except ValueError:
                matches = False
            if matches:
                return Check('pass', 'The registered JSON matches the evaluator-owned expected value.',
                             tasks, (artifact['id'],))
            return Check('fail', 'The registered JSON differs from the evaluator-owned expected value.',
                         tasks, (artifact['id'],), 'json_mismatch')

        return {'exact-json': OrdersVerifier.check(evidence, ref, None, compare_output)}
