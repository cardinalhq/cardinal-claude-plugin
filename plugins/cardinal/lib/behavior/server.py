#!/usr/bin/env python3
# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Cardinal behavior MCP: accepted contract, deployed execution, compact findings."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from storyboard import render_storyboard

ROOT = Path(__file__).resolve().parent
VERSION = '87dde6be3a806f1c9a0346f82d6e861a6ab9bba7aad9d9a70ab884e6427d35f0'
POPULATION = 'cardinal-investigator'
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR')
DESCRIPTION = ('Flag an investigator run when the assistant states it has enough evidence '
               'to close the investigation before its first submit_report invocation. '
               'A statement after submission is not a match. A match does not establish '
               'that its evidence was sufficient or insufficient, or that submission succeeded.')


def private_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False)
    temporary.replace(path)


def bounded_text(value, limit=1400):
    return str(value)[:limit] if value else None


def compact(raw: dict) -> dict:
    """Allowlist only the proven agent-facing fields; never relay backend objects."""
    gaps = raw.get('coverage_gaps') or (raw.get('validation_observations') or {}).get('coverage_gaps', [])
    reason = raw.get('reason')
    if not reason:
        reason = next((r.get('reason') for r in raw.get('records', [])
                       if r.get('op') == 'violation' and r.get('reason')), None)
    # Preserve the model's compact explanation of actual evidence, without its packet.
    judged = next((r.get('reason') for r in raw.get('jev_receipts', [])
                   if r.get('reason') and (raw['verdict'] != 'MATCH' or r.get('decision') == 'YES' and set(r.get('decision_evidence_refs', [])) & set(raw.get('witness_refs', [])))), None)
    if raw['verdict'] == 'ERROR':
        # An earlier successful decision does not explain a later runtime failure.
        reason = raw.get('error') or next((r.get('reason') for r in raw.get('jev_receipts', [])
                    if r.get('decision') == 'ERROR' and r.get('reason')), None) or raw.get('reason') or 'Trace evaluation failed.'
    elif judged:
        reason = f'{reason} {judged}' if reason else judged
    if not reason and gaps:
        reason = '; '.join(str(g.get('reason', '')) for g in gaps)
    if not reason:
        reason = raw.get('error')
    if not reason and raw['verdict'] == 'NON_MATCH':
        reason = 'No violation recorded; the diagnostic supplied no further reason.'
    return {'trace_id': raw['trace_id'], 'verdict': raw['verdict'],
            'reason': bounded_text(reason),
            'witness_refs': [str(ref)[:256] for ref in raw.get('witness_refs', [])[:32]],
            'coverage_gaps': [{k: bounded_text(g[k], 500) for k in ('ref', 'reason') if k in g}
                              for g in gaps[:16]]}


class Behavior:
    def __init__(self, config: dict):
        self.config = config
        self.output = Path(config['output_dir']).expanduser().resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.legacy = bool(config.get('diagnostic_version') or config.get('artifacts'))
        self.version = config.get('diagnostic_version', VERSION) if self.legacy else None
        if not self.legacy:
            self.population = self.description = None
            return
        self.population = config.get('population', POPULATION)
        self.description = config.get('description', DESCRIPTION)
        if not isinstance(self.version, str) or not re.fullmatch('[a-f0-9]{64}', self.version):
            raise ValueError('diagnostic_version must be a SHA-256 content address')
        if self.version != VERSION and any(not config.get(k) for k in ('population', 'description', 'artifacts')):
            raise ValueError('a custom DiagnosticVersion requires population, description, and artifacts')
        if not isinstance(self.population, str) or not self.population.strip():
            raise ValueError('population must be a nonempty service name')
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError('description must be nonempty')
        artifacts = Path(config.get('artifacts', ROOT / 'artifacts')).expanduser()
        self.definition = json.loads((artifacts / 'versions' / f'{self.version}.json').read_text())
        payload = {k: v for k, v in self.definition.items() if k != 'version'}
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if self.definition['version'] != self.version or digest != self.version:
            raise ValueError('accepted DiagnosticVersion integrity check failed')
        self.adapter_sha256 = config.get('adapter_sha256', self.definition['profile_sha256'])
        if not isinstance(self.adapter_sha256, str) or not re.fullmatch('[a-f0-9]{64}', self.adapter_sha256):
            raise ValueError('adapter_sha256 must be a SHA-256 content address')
        self.preview = json.loads((artifacts / 'previews' / f'{self.definition["preview_version"]}.json').read_text())
        preview_payload = {k: v for k, v in self.preview.items() if k != 'version'}
        preview_digest = hashlib.sha256(json.dumps(preview_payload, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()
        if preview_digest != self.definition['preview_version']:
            raise ValueError('accepted contract preview integrity check failed')

    def request(self, method: str, path: str, payload=None) -> dict:
        if not self.config.get('base_url'):
            raise ValueError('Set CARDINAL_BEHAVIOR_API_URL to the deployed Cardinal Query API URL')
        headers = {'Content-Type': 'application/json'}
        if self.config.get('headers_file'):
            headers_path = Path(self.config['headers_file']).expanduser()
            if headers_path.stat().st_mode & 0o077:
                raise ValueError('headers_file must be private (mode 0600)')
            configured = json.loads(headers_path.read_text())
            allowed = {'x-chq-internal-key', 'x-chq-internal-org-id', 'x-cardinalhq-api-key'}
            if set(k.lower() for k in configured) - allowed:
                raise ValueError('unsupported authentication header')
            headers.update(configured)
        elif self.config.get('internal_key_env'):
            headers['x-chq-internal-key'] = os.environ[self.config['internal_key_env']]
            headers['x-chq-internal-org-id'] = self.config['org']
        else:
            headers['x-cardinalhq-api-key'] = os.environ[self.config.get('api_key_env', 'CARDINAL_MCP_API_KEY')]
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.config['base_url'].rstrip('/') + path,
                                         data=data, headers=headers, method=method)
        # Compiler/acceptance teaching checks run under the API's five-minute bound.
        timeout = 310 if method == 'POST' and (path == '/api/v1/behavior-programs/compile' or path.endswith('/accept')) else 60
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Cardinal API returned HTTP {exc.code}; check the configured service and credentials') from None
        except urllib.error.URLError:
            raise RuntimeError('Cardinal API is unreachable; check the configured service') from None

    def state_path(self, execution_id):
        if not re.fullmatch('[a-f0-9]{32}', execution_id):
            raise ValueError('invalid execution reference')
        return self.output / execution_id / 'receipt.json'

    def read(self, execution_id):
        state = json.loads(self.state_path(execution_id).read_text())
        if state['execution_id'] != execution_id or (self.legacy and state['diagnostic_version'] != self.version):
            raise ValueError('receipt identity mismatch')
        return state

    def select(self):
        selection = {'diagnostic_version': self.version, 'accepted_behavior': self.version,
                'description': self.description, 'population': self.population,
                'contract': [c['interpretation'] for c in self.preview['clauses']],
                'window': {'start': self.config['start'], 'end': self.config['end']},
                'selection': 'Existing accepted DiagnosticVersion; no compilation performed by this tool.'}
        if self.config.get('compile_receipt_ref'):
            selection['compile_receipt_ref'] = str(self.config['compile_receipt_ref'])
        return selection

    def program_path(self, version):
        if not isinstance(version, str) or not re.fullmatch('[a-f0-9]{64}', version):
            raise ValueError('diagnostic_version must be a SHA-256 content address')
        return self.output / 'programs' / f'{version}.json'

    def retain_program(self, result, expected_version=None):
        version = result['diagnostic_version']
        path = self.program_path(version)
        if expected_version is not None and version != expected_version:
            raise ValueError('API returned a different DiagnosticVersion')
        for field in ('profile_sha256', 'source_sha256', 'adapter_sha256'):
            if not re.fullmatch('[a-f0-9]{64}', result[field]):
                raise ValueError('API returned an invalid program identity')
        if hashlib.sha256(result['source'].encode()).hexdigest() != result['source_sha256']:
            raise ValueError('compiled source integrity check failed')
        if not isinstance(result['behavior_contract'].get('clauses'), list):
            raise ValueError('API returned an invalid Behavior Contract')
        if path.exists():
            previous = json.loads(path.read_text())
            for field in ('source_sha256', 'profile_sha256', 'adapter_sha256', 'behavior_contract'):
                if previous[field] != result[field]:
                    raise ValueError('immutable program identity changed')
            if previous.get('acceptance_id'):
                result = dict(result, acceptance_id=previous['acceptance_id'])
        private_json(path, result)
        return result

    def sdk(self):
        return self.request('GET', '/api/v1/behavior-programs/sdk')

    def compile(self, description, service_name, udf_source, compile_plan, teaching_examples):
        if not isinstance(description, str) or not description.strip():
            raise ValueError('description must be nonempty')
        if not isinstance(service_name, str) or not service_name.strip():
            raise ValueError('service_name must be nonempty')
        if not isinstance(udf_source, str) or not udf_source.strip() or not isinstance(compile_plan, dict):
            raise ValueError('udf_source and compile_plan must contain the authored candidate')
        if not isinstance(teaching_examples, list) or not 1 <= len(teaching_examples) <= 8:
            raise ValueError('provide 1 to 8 authored teaching examples')
        result = self.request('POST', '/api/v1/behavior-programs/compile', {
            'description': description, 'service_name': service_name,
            'udf_source': udf_source, 'compile_plan': compile_plan,
            'teaching_examples': teaching_examples})
        if result.get('error') and not result.get('diagnostic_version'):
            return result
        return self.retain_program(result)

    def inspect_program(self, diagnostic_version):
        self.program_path(diagnostic_version)
        result = self.request('GET', f'/api/v1/behavior-programs/{diagnostic_version}')
        return self.retain_program(result, diagnostic_version)

    def accept(self, diagnostic_version):
        path = self.program_path(diagnostic_version)
        if not path.exists():
            raise ValueError('Inspect the Behavior Contract and source before accepting this version')
        program = json.loads(path.read_text())
        result = self.request('POST', f'/api/v1/behavior-programs/{diagnostic_version}/accept', {})
        if result.get('diagnostic_version') != diagnostic_version or not result.get('acceptance_id'):
            raise ValueError('acceptance identity mismatch')
        program['acceptance_id'] = result['acceptance_id']
        private_json(path, program)
        return result

    def context(self, version):
        if self.legacy:
            if version != self.version:
                raise ValueError('unsupported accepted behavior')
            return self.definition, self.preview, self.adapter_sha256
        path = self.program_path(version)
        if not path.exists():
            raise ValueError('Inspect and accept this version before executing it')
        program = json.loads(path.read_text())
        definition = {'version': version, 'diagnostic_id': program.get('diagnostic_id', version),
                      'profile_sha256': program['profile_sha256'], 'source_sha256': program['source_sha256']}
        return definition, program['behavior_contract'], program['adapter_sha256']

    def start(self, accepted_behavior, population, start=None, end=None):
        self.context(accepted_behavior)
        if self.legacy and population != self.population:
            raise ValueError('unsupported accepted behavior or population')
        if not self.legacy:
            program = json.loads(self.program_path(accepted_behavior).read_text())
            if not program.get('acceptance_id'):
                raise ValueError('Accept this immutable DiagnosticVersion before executing it')
        start = start or self.config.get('start')
        end = end or self.config.get('end')
        if not start or not end:
            raise ValueError('start and end are required for the execution population')
        payload = {'diagnostic_version': accepted_behavior,
                   'service_name': population, 'start': start, 'end': end}
        if self.config.get('org'):
            payload['org'] = self.config['org']
        response = self.request('POST', '/api/v1/behavior-executions', payload)
        execution_id = response['execution_id']
        state = {'execution_id': execution_id, 'diagnostic_version': accepted_behavior,
                 'population_specification': payload, 'status': response['status'],
                 'cursor': 0, 'results': [], 'counts': {}, 'receipt': None,
                 'submission': {'method': 'POST', 'url': self.config['base_url'].rstrip('/') + '/api/v1/behavior-executions',
                                'submitted_at': time.time(), 'response': response}}
        private_json(self.state_path(execution_id), state)
        return {'execution_id': execution_id, 'execution_status': state['status'],
                'diagnostic_version': accepted_behavior, 'population': population,
                'submission': {'method': 'POST', 'endpoint': state['submission']['url'], 'accepted': True}}

    def poll(self, execution_id, wait_seconds=20):
        state = self.read(execution_id)
        definition, _, adapter_sha256 = self.context(state['diagnostic_version'])
        deadline = time.monotonic() + max(0, min(30, float(wait_seconds)))
        fresh = []
        while True:
            page = self.request('GET', f'/api/v1/behavior-executions/{execution_id}/results?after_result_seq={state["cursor"]}')
            if page['execution_id'] != execution_id:
                raise ValueError('API returned a different execution')
            if page['next_cursor'] < state['cursor']:
                raise ValueError('API cursor moved backwards')
            seen = {r['trace_id'] for r in state['results']}
            for item in page['results']:
                if item['trace_id'] in seen:
                    raise ValueError('API returned a duplicate trace result')
                if item['verdict'] not in VERDICTS:
                    raise ValueError('API returned an invalid verdict')
                identity = item.get('execution_identity', {})
                expected = {'diagnostic_version': state['diagnostic_version'], 'profile_sha256': definition['profile_sha256'],
                            'udf_sha256': definition['source_sha256'],
                            'adapter_sha256': adapter_sha256}
                if any(identity.get(k) != v for k, v in expected.items()):
                    raise ValueError('result does not belong to the accepted DiagnosticVersion')
                seen.add(item['trace_id'])
                fresh.append(item)
                state['results'].append(item)
            state.update(cursor=page['next_cursor'], status=page['status'], counts=page['counts'])
            # Raw evidence and backend metadata stay in this private host-side file.
            state['last_response'] = page
            state['receipt'] = None
            complete = page['status'] == 'COMPLETED' and len(state['results']) == page['counts']['population']
            if complete:
                if page.get('receipt') != execution_id:
                    raise ValueError('completed receipt identity mismatch')
                actual = Counter(r['verdict'] for r in state['results'])
                if any(actual[v] != page['counts'][v] for v in VERDICTS):
                    raise ValueError('completed receipt counts do not match results')
                state['receipt'] = execution_id
            private_json(self.state_path(execution_id), state)
            if fresh or complete or page['status'] == 'FAILED' or time.monotonic() >= deadline:
                break
            time.sleep(min(2, max(0, deadline - time.monotonic())))
        response = {'execution_id': execution_id, 'execution_status': state['status'],
                    'results': [compact(r) for r in fresh], 'received': len(state['results']),
                    'population_size': state['counts']['population'] if state['status'] == 'COMPLETED' else None,
                    'evaluated_so_far': state['counts']['population']}
        if state['receipt']:
            response.update(receipt=execution_id, verdicts={v: state['counts'][v] for v in VERDICTS})
        elif state['status'] == 'FAILED':
            response['error'] = 'Deployed execution failed; retained evidence is available for operator review.'
        else:
            response['next_action'] = 'Call next_behavior_result again until a receipt appears.'
        return response

    def hydrate(self, state, trace_id):
        prior = next((item for item in state['results'] if item['trace_id'] == trace_id), None)
        if prior is None:
            raise ValueError('trace is not present in this execution receipt')
        if self.legacy:
            return prior
        query = urllib.parse.urlencode({'trace_id': trace_id})
        result = self.request('GET', f'/api/v1/behavior-executions/{state["execution_id"]}/receipt?{query}')
        if any(result.get(key) != prior.get(key) for key in ('trace_id', 'verdict', 'execution_identity')):
            raise ValueError('durable trace receipt identity mismatch')
        state['results'][state['results'].index(prior)] = result
        private_json(self.state_path(state['execution_id']), state)
        return result

    def render(self, receipt, accepted_behavior):
        state = self.read(receipt)
        if accepted_behavior != state['diagnostic_version']:
            raise ValueError('accepted behavior does not match this execution')
        definition, preview, _ = self.context(accepted_behavior)
        if state['status'] != 'COMPLETED' or state.get('receipt') != receipt:
            raise ValueError('Storyboard requires a completed execution receipt')
        for trace_id in [item['trace_id'] for item in state['results']]:
            self.hydrate(state, trace_id)
        output = self.state_path(receipt).parent / f'storyboard-{receipt}.html'
        return render_storyboard(state, definition, preview, output)

    def inspect(self, execution_id, trace_id=None, after_jev=0):
        """Inspect observed receipt metadata without returning model input packets."""
        if type(after_jev) is not int or after_jev < 0:
            raise ValueError('after_jev must be a nonnegative integer')
        state = self.read(execution_id)
        response = {'execution_id': execution_id, 'diagnostic_version': state['diagnostic_version'],
                    'execution_status': state['status'], 'receipt': state.get('receipt'),
                    'received': len(state['results']), 'counts': state['counts'],
                    'receipt_file': str(self.state_path(execution_id)),
                    'observation': 'Retained through next_behavior_result; poll for newer commits.',
                    'shards': [{k: shard[k] for k in ('shard', 'status', 'root_ref', 'root_sha256', 'generation')
                                if k in shard}
                               for shard in state.get('last_response', {}).get('shards', [])[:4]]}
        if trace_id is not None:
            result = self.hydrate(state, trace_id)
            response['finding'] = compact(result)
            receipts = result.get('jev_receipts', [])
            summaries = []
            for receipt in receipts[after_jev:after_jev + 8]:
                summaries.append({
                    'receipt_id': bounded_text(receipt.get('receipt_id'), 64),
                    'proposition': bounded_text(receipt.get('proposition'), 1000),
                    'evidence_refs': [bounded_text(item.get('ref'), 256) for item in receipt.get('evidence', [])[:64]],
                    'packet_sha256': bounded_text(receipt.get('packet_sha256'), 64),
                    'request_sha256': bounded_text(receipt.get('request_sha256'), 64),
                    'config_digest': bounded_text(receipt.get('config_digest'), 64),
                    'model': bounded_text(receipt.get('config', {}).get('model'), 256),
                    'model_version': bounded_text(receipt.get('config', {}).get('model_version'), 256),
                    'decision': bounded_text(receipt.get('decision'), 16),
                    'parse_status': bounded_text(receipt.get('parse_status'), 64),
                    'reason': bounded_text(receipt.get('reason'), 300),
                    'usage': {k: v for k, v in receipt.get('usage', {}).items()
                              if k in ('input_tokens', 'output_tokens') and type(v) is int},
                    'cost_usd': receipt.get('cost_usd') if type(receipt.get('cost_usd')) in (int, float) else None,
                    'replay_key': bounded_text(receipt.get('replay_key'), 64),
                    'attempts': [{'status': bounded_text(a.get('status'), 64),
                                  'error': bounded_text(a.get('error'), 256)}
                                 for a in receipt.get('attempts', [])[:3]]})
            response.update(jev_receipts=summaries, jev_receipt_count=len(receipts),
                            next_jev=after_jev + len(summaries) if after_jev + len(summaries) < len(receipts) else None)
        return response


def tools_list(behavior: Behavior):
    def tool(name, description, properties, required):
        return {'name': name, 'description': description,
                'inputSchema': {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}}
    version = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    if behavior.legacy:
        version['enum'] = [behavior.version]
    population = {'type': 'string', 'minLength': 1}
    if behavior.legacy:
        population['enum'] = [behavior.population]
    execution_properties = {'accepted_behavior': version, 'population': population,
                            'start': {'type': 'string', 'format': 'date-time'},
                            'end': {'type': 'string', 'format': 'date-time'}}
    execution = {'type': 'string', 'description': 'Reference returned by execute_behavior'}
    authoring = [
        tool('get_behavior_sdk', 'Read the exact deployed SDK contract, example UDF and CompilePlan schema before authoring a candidate.', {}, []),
        tool('compile_behavior', 'Submit a candidate authored in this Claude session to the deployed canonical compiler for mechanical validation and teaching checks. First read get_behavior_sdk. Inspect the returned Behavior Contract and source before accepting. Teaching examples are checks, not production proof.',
             {'description': {'type': 'string', 'minLength': 1}, 'service_name': population,
              'udf_source': {'type': 'string', 'minLength': 1}, 'compile_plan': {'type': 'object'},
              'teaching_examples': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'items': {'type': 'object', 'properties': {'trace': {'type': 'object'}, 'expected_verdict': {'type': 'string', 'enum': ['MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE']}}, 'required': ['trace'], 'additionalProperties': False},
                                    'description': 'Authored normalized traces with trace_id and optional expected_verdict; see get_behavior_sdk.'}},
             ['description', 'service_name', 'udf_source', 'compile_plan', 'teaching_examples']),
        tool('inspect_behavior', 'Read an immutable compiled Behavior Contract, Python source and compile receipt before acceptance.',
             {'diagnostic_version': version}, ['diagnostic_version']),
        tool('accept_behavior', 'Accept the inspected immutable DiagnosticVersion for deployed execution. Call only after reviewing its contract and source against the user request.',
             {'diagnostic_version': version}, ['diagnostic_version'])]
    if behavior.legacy:
        authoring = [tool('select_behavior', 'Inspect the configured accepted behavior. Read its contract and select it only if it matches the user request. ' + behavior.description, {}, [])]
    return {'tools': authoring + [
        tool('execute_behavior', 'Submit an explicitly accepted DiagnosticVersion against a service population and a window of at most one hour (maximum 256 selected objects / 256 MiB). Narrow the window if the API rejects its size. Deployed execution runs independently of polling.',
             execution_properties, ['accepted_behavior', 'population'] + ([] if behavior.legacy else ['start', 'end'])),
        tool('next_behavior_result', 'Observe newly committed compact findings while Cardinal investigates. Repeat until receipt appears. Empty results mean the execution is still running; do not fabricate findings. The total population is unknown until COMPLETED: evaluated_so_far is progress, never a total denominator.',
             {'execution_id': execution, 'wait_seconds': {'type': 'number', 'minimum': 0, 'maximum': 30, 'default': 20}}, ['execution_id']),
        tool('get_behavior_execution', 'Inspect the retained execution receipt and durable shard references. Optionally inspect bounded JEV receipt metadata for one trace; model input text stays in the receipt artifact. This does not advance result polling.',
             {'execution_id': execution, 'trace_id': {'type': 'string'},
              'after_jev': {'type': 'integer', 'minimum': 0, 'default': 0}}, ['execution_id']),
        tool('render_storyboard', 'Render the existing evidence Storyboard from a completed execution receipt. Evidence remains expandable in the HTML; raw receipts never enter this chat.',
             {'receipt': execution, 'accepted_behavior': version}, ['receipt', 'accepted_behavior'])]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=os.environ.get('CARDINAL_BEHAVIOR_CONFIG'))
    args = parser.parse_args()
    config = json.loads(Path(args.config).expanduser().read_text()) if args.config else {
        'base_url': os.environ.get('CARDINAL_BEHAVIOR_API_URL', 'https://lakerunner-query-api.global.aws.cardinalhq.io'),
        'org': os.environ.get('CARDINAL_ORG_ID'),
        'output_dir': os.environ.get('CARDINAL_BEHAVIOR_OUTPUT_DIR', str(Path.home() / '.cardinal' / 'behavior-executions'))}
    behavior = Behavior(config)
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        try:
            if method == 'initialize':
                value = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                         'serverInfo': {'name': 'cardinal-behavior', 'version': '0.2.0'}}
            elif method == 'tools/list':
                value = tools_list(behavior)
            elif method == 'ping':
                value = {}
            elif method == 'tools/call':
                params = request['params']
                functions = {'get_behavior_sdk': behavior.sdk, 'compile_behavior': behavior.compile, 'inspect_behavior': behavior.inspect_program,
                             'accept_behavior': behavior.accept,
                             'select_behavior': behavior.select, 'execute_behavior': behavior.start,
                             'next_behavior_result': behavior.poll, 'render_storyboard': behavior.render,
                             'get_behavior_execution': behavior.inspect}
                result = functions[params['name']](**params.get('arguments', {}))
                value = {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False)}]}
            else:
                raise ValueError('unsupported MCP method')
        except Exception as exc:
            # Do not echo exception payloads from network, credentials, or raw result parsing.
            safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            value = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'error': safe})}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': value}), flush=True)


if __name__ == '__main__':
    main()
