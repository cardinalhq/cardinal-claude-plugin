#!/usr/bin/env python3
# Copyright (c) 2025-2026 CardinalHQ, Inc. All rights reserved.
"""Cardinal behavior MCP: accepted contract, deployed execution, compact findings."""
from __future__ import annotations
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
from storyboard import render_storyboard
from compilation.files import private_json, private_text
from compilation.hosted import Authoring, SDK_IDENTITIES

ROOT = Path(__file__).resolve().parent
VERSION = '87dde6be3a806f1c9a0346f82d6e861a6ab9bba7aad9d9a70ab884e6427d35f0'
POPULATION = 'cardinal-investigator'
VERDICTS = ('MATCH', 'NON_MATCH', 'UNKNOWN', 'ERROR')
DESCRIPTION = ('Flag an investigator run when the assistant states it has enough evidence '
               'to close the investigation before its first submit_report invocation. '
               'A statement after submission is not a match. A match does not establish '
               'that its evidence was sufficient or insufficient, or that submission succeeded.')


def default_config(environ=None):
    """Route a normal plugin credential through its existing authenticated host."""
    env = os.environ if environ is None else environ
    base_url = env.get('CARDINAL_BEHAVIOR_API_URL')
    if not base_url and env.get('CARDINAL_MCP_URL'):
        connection = urllib.parse.urlsplit(env['CARDINAL_MCP_URL'])
        match = re.fullmatch(r'/api/orgs/([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})/(?:mcp|integrations/[A-Za-z0-9_-]+/mcp)/?', connection.path)
        if (connection.scheme not in ('http', 'https') or not connection.netloc
                or connection.username is not None or connection.password is not None
                or connection.query or connection.fragment or not match):
            raise ValueError('CARDINAL_MCP_URL must identify the connected organization MCP endpoint')
        base_url = urllib.parse.urlunsplit((connection.scheme, connection.netloc,
                    f'/api/orgs/{match.group(1)}/behavior', '', ''))
    return {'base_url': base_url, 'org': env.get('CARDINAL_ORG_ID'),
            'api_key_env': 'CARDINAL_QUERY_API_KEY' if env.get('CARDINAL_BEHAVIOR_API_URL') else 'CARDINAL_MCP_API_KEY',
            'output_dir': env.get('CARDINAL_BEHAVIOR_OUTPUT_DIR', str(Path.home() / '.cardinal' / 'behavior-executions'))}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # urllib otherwise carries custom API-key headers across redirects.
        return None


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


class Behavior(Authoring):
    def __init__(self, config: dict):
        self.config = config
        self.output = Path(config['output_dir']).expanduser().resolve()
        self.output.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.sdk_observation = None
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
            raise ValueError('Connect the Cardinal plugin, or configure CARDINAL_BEHAVIOR_API_URL with a matching data-plane credential')
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
            key_env = self.config.get('api_key_env', 'CARDINAL_MCP_API_KEY')
            key = os.environ.get(key_env)
            if not key:
                raise ValueError(f'{key_env} must contain the configured API credential')
            headers['x-cardinalhq-api-key'] = key
        data = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.config['base_url'].rstrip('/') + path,
                                         data=data, headers=headers, method=method)
        # Compiler/acceptance teaching checks run under the API's five-minute bound.
        timeout = 310 if method == 'POST' and (path in ('/api/v1/behavior-programs/compile', '/api/v1/behavior-programs/test') or path.endswith('/accept')) else 60
        try:
            with urllib.request.build_opener(NoRedirect()).open(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise RuntimeError(f'Cardinal API returned HTTP {status}; check the configured service and credentials') from None
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
                      'profile_sha256': program['profile_sha256'], 'source_sha256': program['source_sha256'],
                      **self.identities(program), 'sdk_source': self.sdk_source(program)}
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
                if not self.legacy:
                    expected.update(self.identities(definition), sdk_source=definition['sdk_source'])
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
        tool('compile_behavior', 'Compile the authored candidate against the exact SDK read with get_behavior_sdk. Performs syntax/import/API/mechanical validation. Then run test_behavior on real teaching traces, inspect evidence, revise if needed, and explicitly accept. Optional authored examples are compiler checks only.',
             {'description': {'type': 'string', 'minLength': 1}, 'service_name': population,
              'udf_source': {'type': 'string', 'minLength': 1}, 'compile_plan': {'type': 'object'},
              'teaching_examples': {'type': 'array', 'minItems': 0, 'maxItems': 8, 'items': {'type': 'object', 'properties': {'trace': {'type': 'object'}, 'expected_verdict': {'type': 'string', 'enum': ['MATCH', 'NON_MATCH', 'UNKNOWN', 'NOT_APPLICABLE']}}, 'required': ['trace'], 'additionalProperties': False},
                                    'description': 'Authored normalized traces with trace_id and optional expected_verdict; see get_behavior_sdk.'}},
             ['description', 'service_name', 'udf_source', 'compile_plan']),
        tool('test_behavior', 'Run a compiled candidate on 1 to 8 real teaching traces using the deployed production runtime and JEV before acceptance. Returns MATCH/NON_MATCH/UNKNOWN/ERROR, witnesses, Recorder output, JEV receipts and a retained teaching receipt. Supply expected_verdicts for every trace before acceptance; compile revised source before retesting.',
             {'diagnostic_version': version, 'trace_ids': {'type': 'array', 'minItems': 1, 'maxItems': 8, 'uniqueItems': True, 'items': {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}},
              'service_name': population, 'start': {'type': 'string', 'format': 'date-time'},
              'end': {'type': 'string', 'format': 'date-time'},
              'udf_source': {'type': 'string', 'description': 'Optional source must exactly match the compiled version.'},
              'expected_verdicts': {'type': 'object', 'additionalProperties': {'type': 'string', 'enum': ['MATCH', 'NON_MATCH', 'UNKNOWN']}}},
             ['diagnostic_version', 'trace_ids', 'service_name', 'start', 'end']),
        tool('inspect_behavior', 'Read an immutable compiled Behavior Contract, Python source and compile receipt before acceptance.',
             {'diagnostic_version': version}, ['diagnostic_version']),
        tool('get_behavior_compilation', 'Inspect the accumulated development regression suite, tested coverage and preserved incumbent for a compiled candidate. Compilation does not promote it.',
             {'diagnostic_version': version}, ['diagnostic_version']),
        tool('run_behavior_regressions', 'Execute a repaired candidate on the complete accumulated real-trace development suite, including earlier failed batches. ERROR, missing cases and verdict regressions block acceptance and preserve the incumbent. This is development testing, not independent qualification.',
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
    config = json.loads(Path(args.config).expanduser().read_text()) if args.config else default_config()
    behavior = Behavior(config)
    for line in sys.stdin:
        request = json.loads(line)
        if 'id' not in request:
            continue
        method = request.get('method')
        try:
            if method == 'initialize':
                value = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                         'serverInfo': {'name': 'cardinal-behavior', 'version': '0.3.0'}}
            elif method == 'tools/list':
                value = tools_list(behavior)
            elif method == 'ping':
                value = {}
            elif method == 'tools/call':
                params = request['params']
                functions = {'get_behavior_sdk': behavior.sdk, 'compile_behavior': behavior.compile, 'inspect_behavior': behavior.inspect_program,
                             'test_behavior': behavior.test, 'accept_behavior': behavior.accept,
                             'get_behavior_compilation': behavior.compilation_status,
                             'run_behavior_regressions': behavior.run_regressions,
                             'select_behavior': behavior.select, 'execute_behavior': behavior.start,
                             'next_behavior_result': behavior.poll, 'render_storyboard': behavior.render,
                             'get_behavior_execution': behavior.inspect}
                result = functions[params['name']](**params.get('arguments', {}))
                value = {'content': [{'type': 'text', 'text': json.dumps(result, ensure_ascii=False, indent=2)}]}
            else:
                raise ValueError('unsupported MCP method')
        except Exception as exc:
            # Do not echo exception payloads from network, credentials, or raw result parsing.
            safe = str(exc) if isinstance(exc, (ValueError, RuntimeError)) else type(exc).__name__
            value = {'isError': True, 'content': [{'type': 'text', 'text': json.dumps({'error': safe})}]}
        print(json.dumps({'jsonrpc': '2.0', 'id': request['id'], 'result': value}), flush=True)


if __name__ == '__main__':
    main()
