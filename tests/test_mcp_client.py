"""Offline protocol and boundary tests; run with unittest, no third-party packages."""
from contextlib import redirect_stdout
import hashlib
import http.client
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock
import urllib.error

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('study_mcp_client', ROOT / 'scripts' / 'mcp_client.py')
mcp = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mcp)


class Response:
    def __init__(self, body=b'', status=200, media='application/json', chunks=None, headers=None):
        self.status = status
        self.headers = {'Content-Type': media, **(headers or {})}
        self.chunks = list(chunks) if chunks is not None else [body]
        self.closed = False

    def read1(self, size):
        return self.chunks.pop(0) if self.chunks else b''

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


def rpc_response(request_id, result, **kwargs):
    return Response(json.dumps({'jsonrpc': '2.0', 'id': request_id, 'result': result}).encode(), **kwargs)


def tool(data, structured=True, is_error=False):
    result = {'isError': is_error}
    result['structuredContent' if structured else 'content'] = data if structured else [
        {'type': 'text', 'text': json.dumps(data)}]
    return result


def challenge(**changes):
    return {'algo': mcp.ALGO, 'session_id': 'test_session', 'challenge_id': 'test_challenge',
            'difficulty': 4, 'expires_at': int(time.time()) + 60, **changes}


class Opener:
    def __init__(self, results=None, version='2025-06-18', pages=None, fail=None, session=None):
        self.results = list(results or [])
        self.version, self.pages, self.fail, self.session = version, list(pages or []), fail, session
        self.history = []

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.history.append((request, payload, timeout))
        if self.fail:
            raise self.fail
        method, request_id = payload['method'], payload.get('id')
        if method == 'initialize':
            return rpc_response(request_id, {'protocolVersion': self.version, 'capabilities': {'tools': {}},
                'serverInfo': {'name': 'offline', 'version': '1'}},
                headers={'Mcp-Session-Id': self.session} if self.session else {})
        if method == 'notifications/initialized':
            return Response(status=202)
        if method == 'tools/list':
            return rpc_response(request_id, self.pages.pop(0) if self.pages else {'tools': [{'name': 'convert_gpa'}]})
        if method == 'tools/call':
            result = self.results.pop(0)
            if isinstance(result, Exception):
                raise result
            if isinstance(result, Response):
                return result
            return rpc_response(request_id, result)
        raise AssertionError(method)


class MCPClientTests(unittest.TestCase):
    def setUp(self):
        # Even an accidental default-opener path cannot contact a real service.
        self.network = mock.patch('urllib.request.OpenerDirector.open', side_effect=AssertionError('network forbidden'))
        self.network.start()
        self.addCleanup(self.network.stop)

    def assert_error(self, code, fn, *args, **kwargs):
        with self.assertRaises(mcp.ClientError) as raised:
            fn(*args, **kwargs)
        self.assertEqual(raised.exception.code, code)
        return raised.exception

    def test_handshake_versions_headers_and_session(self):
        for version in mcp.VERSIONS:
            with self.subTest(version=version):
                opener = Opener(version=version, session='opaque-session')
                client = mcp.MCPClient(opener=opener)
                self.assertEqual(client.list_tools(), [{'name': 'convert_gpa'}])
                self.assertEqual(client.requests, 3)
                self.assertEqual([p['method'] for _, p, _ in opener.history],
                                 ['initialize', 'notifications/initialized', 'tools/list'])
                first = opener.history[0][0]
                self.assertEqual(first.get_header('Accept'), 'application/json, text/event-stream')
                self.assertIsNone(first.get_header('Authorization'))
                for request, _, _ in opener.history[1:]:
                    self.assertEqual(request.get_header('Mcp-protocol-version'), version)
                    self.assertEqual(request.get_header('Mcp-session-id'), 'opaque-session')

    def test_unsupported_protocol_stops_before_notification(self):
        opener = Opener(version='2099-01-01')
        self.assert_error('unsupported_protocol', mcp.MCPClient(opener=opener).initialize)
        self.assertEqual(len(opener.history), 1)

    def test_assistant_authorization_fixed_destination(self):
        opener = Opener()
        mcp.MCPClient(server='assist', api_key='synthetic-key', opener=opener).list_tools()
        for request, _, _ in opener.history:
            self.assertEqual(request.full_url, mcp.ENDPOINTS['assist'])
            self.assertEqual(request.get_header('Authorization'), 'Bearer synthetic-key')

    def test_query_ignores_environment_and_rejects_explicit_key(self):
        self.assertIsNone(mcp.private_key('query', environ={'LIUXUE_API_KEY': 'synthetic'}))
        self.assert_error('query_is_anonymous', mcp.private_key, 'query', '/never-read')
        self.assert_error('query_is_anonymous', mcp.MCPClient, api_key='synthetic')
        self.assert_error('api_key_required', mcp.private_key, 'assist', environ={})

    def test_private_key_file_permissions_and_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'key'
            path.write_text('synthetic-key\n', encoding='utf-8')
            path.chmod(0o600)
            self.assertEqual(mcp.private_key('assist', path, {}), 'synthetic-key')
            if os.name == 'posix':
                path.chmod(0o644)
                self.assert_error('key_file_unavailable', mcp.private_key, 'assist', path, {})
                path.chmod(0o600)
                link = Path(directory) / 'link'
                link.symlink_to(path)
                self.assert_error('key_file_unavailable', mcp.private_key, 'assist', link, {})

    def test_http_401_429_and_redirect_no_retry_or_body_disclosure(self):
        for status in (401, 429, 302):
            with self.subTest(status=status):
                failure = urllib.error.HTTPError('https://invalid.example/private', status, 'synthetic-secret',
                    {'Retry-After': '12'}, io.BytesIO(b'synthetic-secret'))
                opener = Opener(fail=failure)
                error = self.assert_error('redirect_blocked' if status == 302 else 'http_error',
                                         mcp.MCPClient(opener=opener).list_tools)
                self.assertEqual(error.details['http_status'], status)
                self.assertEqual(error.details['retry_after_seconds'], 12)
                self.assertNotIn('synthetic-secret', json.dumps(error.as_dict()))
                self.assertEqual(len(opener.history), 1)
        self.assertIsNone(mcp.NoRedirect().redirect_request(None, None, 302, '', {}, 'https://other.example'))

    def test_timeout_disconnect_and_bad_json(self):
        for failure, code in [(socket.timeout(), 'request_timeout'),
                              (urllib.error.URLError(socket.timeout()), 'request_timeout'),
                              (http.client.IncompleteRead(b'partial'), 'connection_error')]:
            with self.subTest(code=code, failure=type(failure).__name__):
                opener = Opener(fail=failure)
                self.assert_error(code, mcp.MCPClient(opener=opener).initialize)
                self.assertEqual(len(opener.history), 1)
        opener = mock.Mock()
        opener.open.return_value = Response(b'{broken')
        self.assert_error('invalid_response_encoding', mcp.MCPClient(opener=opener).initialize)

    def test_json_and_sse_chunking_notifications_utf8(self):
        client = mcp.MCPClient(opener=Opener())
        payload = {'jsonrpc': '2.0', 'id': 9, 'result': {'中文': '可用'}}
        data = (': ping\r\ndata: ' + json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress'})
                + '\r\n\r\nevent: message\r\ndata: ' + json.dumps(payload, ensure_ascii=False) + '\r\n\r\n').encode()
        response = Response(media='text/event-stream; charset=utf-8', chunks=[data[i:i+3] for i in range(0, len(data), 3)])
        self.assertEqual(client._read_result(response, 9, time.monotonic() + 1), {'中文': '可用'})
        self.assertEqual(client._read_result(rpc_response(9, {'ok': 1}), 9, time.monotonic() + 1), {'ok': 1})

    def test_sse_incomplete_id_mismatch_and_server_request(self):
        client = mcp.MCPClient(opener=Opener())
        self.assert_error('incomplete_sse', client._read_result,
                          Response(b': ping\n\n', media='text/event-stream'), 1, time.monotonic() + 1)
        self.assert_error('rpc_id_mismatch', client._read_result, rpc_response('1', {}), 1, time.monotonic() + 1)
        self.assert_error('unsupported_server_request', client._message,
                          {'jsonrpc': '2.0', 'id': 1, 'method': 'sampling/createMessage'}, 1)
        self.assert_error('rpc_error', client._message,
                          {'jsonrpc': '2.0', 'id': 1, 'error': {'code': -32602, 'message': 'not echoed'}}, 1)

    def test_response_size_and_deadline(self):
        client = mcp.MCPClient(opener=Opener())
        self.assert_error('response_too_large', client._read_result,
                          Response(b'x' * (mcp.MAX_RESPONSE + 1)), 1, time.monotonic() + 1)
        self.assert_error('request_timeout', client._read_result, Response(), 1, time.monotonic() - 1)

    def test_structured_content_text_json_and_error(self):
        result = tool({'source': 'structured'})
        result['content'] = [{'type': 'text', 'text': '{"source":"text"}'}]
        self.assertEqual(mcp.decode_tool_result(result), ({'source': 'structured'}, False))
        self.assertEqual(mcp.decode_tool_result(tool({'ok': 1}, structured=False)), ({'ok': 1}, False))
        self.assertEqual(mcp.decode_tool_result({'content': [{'type': 'text', 'text': 'plain'}], 'isError': True}),
                         ({'text': 'plain'}, True))
        self.assert_error('invalid_tool_result', mcp.decode_tool_result, {'content': None})

    def test_pagination_and_request_budget(self):
        opener = Opener(pages=[{'tools': [{'name': 'a'}], 'nextCursor': 'next'}, {'tools': [{'name': 'b'}]}])
        self.assertEqual(len(mcp.MCPClient(opener=opener).list_tools()), 2)
        self.assertEqual(opener.history[-1][1]['params'], {'cursor': 'next'})
        opener = Opener(pages=[{'tools': [], 'nextCursor': 'same'}, {'tools': [], 'nextCursor': 'same'}])
        self.assert_error('invalid_tool_cursor', mcp.MCPClient(opener=opener).list_tools)
        opener = Opener()
        self.assert_error('request_budget_exceeded', mcp.MCPClient(opener=opener, max_requests=2).list_tools)
        self.assertEqual(len(opener.history), 2)

    def test_pow_required_invalid_single_retry_and_real_solution(self):
        for error in ('pow_required', 'pow_invalid'):
            with self.subTest(error=error):
                ch = challenge()
                opener = Opener(results=[tool({'error': error, 'pow': ch}), tool({'value': 3.7})])
                client = mcp.MCPClient(opener=opener)
                result = client.call_tool('convert_gpa', {'score': 85})
                self.assertEqual(result, {'result': {'value': 3.7}, 'is_error': False, 'pow_retried': True})
                args = opener.history[-1][1]['params']['arguments']
                digest = hashlib.sha256(f"{ch['challenge_id']}:{args['pow_nonce']}:{ch['session_id']}".encode()).digest()
                self.assertGreaterEqual(int.from_bytes(digest, 'big').bit_length() <= 256 - ch['difficulty'], True)
                self.assertEqual(args['score'], 85)
                self.assertEqual(client.requests, 4)

    def test_second_pow_error_does_not_third_call_or_solve(self):
        opener = Opener(results=[tool({'error': 'pow_required', 'pow': challenge()}),
                                tool({'error': 'pow_invalid', 'pow': challenge(challenge_id='second')})])
        client = mcp.MCPClient(opener=opener)
        with mock.patch.object(mcp.PowPipeline, 'handle_response', wraps=client._pow.handle_response) as handle:
            result = client.call_tool('convert_gpa', {'score': 85})
        self.assertTrue(result['is_error'])
        self.assertEqual(handle.call_count, 1)
        self.assertEqual(client.requests, 4)

    def test_bad_challenges_not_computed(self):
        for change, code in [({'algo': 'other'}, 'invalid_pow_challenge'),
                             ({'difficulty': 25}, 'pow_difficulty_limit'),
                             ({'difficulty': True}, 'pow_difficulty_limit'),
                             ({'expires_at': time.time() - 1}, 'pow_expired_or_invalid_ttl'),
                             ({'expires_at': time.time() + 301}, 'pow_expired_or_invalid_ttl'),
                             ({'expires_at': '2099-01-01'}, 'pow_expired_or_invalid_ttl'),
                             ({'challenge_id': 'x' * 129}, 'invalid_pow_challenge')]:
            with self.subTest(change=change):
                opener = Opener(results=[tool({'error': 'pow_required', 'pow': challenge(**change)})])
                client = mcp.MCPClient(opener=opener)
                with mock.patch.object(client._pow, 'handle_response') as handle:
                    self.assert_error(code, client.call_tool, 'convert_gpa', {'score': 85})
                    handle.assert_not_called()
                self.assertEqual(client.requests, 3)

    def test_solver_limit_no_business_retry(self):
        opener = Opener(results=[tool({'error': 'pow_required', 'pow': challenge()})])
        client = mcp.MCPClient(opener=opener)
        with mock.patch.object(client._pow, 'handle_response', side_effect=TimeoutError()):
            self.assert_error('pow_solve_limit', client.call_tool, 'convert_gpa', {'score': 85})
        self.assertEqual(client.requests, 3)

    def test_prefetch_is_consumed_on_next_call(self):
        ch = challenge()
        opener = Opener(results=[tool({'value': 1, 'pow': ch}), tool({'value': 2})])
        client = mcp.MCPClient(opener=opener)
        client.call_tool('convert_gpa', {'score': 85})
        self.assertTrue(client._pow.wait_prefetch(1))
        result = client.call_tool('convert_gpa', {'score': 86})
        self.assertFalse(result['pow_retried'])
        self.assertEqual(opener.history[-1][1]['params']['arguments']['pow_challenge_id'], ch['challenge_id'])
        self.assertNotIn('pow_challenge_id', client._pow.attach_args({}))

    def test_invalid_next_challenge_keeps_success_and_does_not_compute(self):
        opener = Opener(results=[tool({'value': 3.7, 'pow': challenge(difficulty=99)})])
        client = mcp.MCPClient(opener=opener)
        with mock.patch.object(client._pow, 'handle_response') as handle:
            result = client.call_tool('convert_gpa', {'score': 85})
            handle.assert_not_called()
        self.assertFalse(result['is_error'])
        self.assertEqual(result['result']['value'], 3.7)
        self.assertEqual(result['warnings'][0]['error'], 'pow_difficulty_limit')

    def test_write_gate_before_network(self):
        opener = Opener()
        client = mcp.MCPClient(server='assist', api_key='synthetic-key', opener=opener)
        self.assert_error('write_not_allowed', client.call_tool, 'submit_to_hri', {'confirmed': False})
        self.assert_error('confirmation_required', client.call_tool, 'submit_to_hri', {}, allow_write=True)
        self.assert_error('confirmation_required', client.call_tool, 'submit_to_hri',
                          {'confirmed': True, 'summary': {}, 'confirm_code': 'synthetic'}, allow_write=True)
        self.assertEqual(opener.history, [])

    def test_write_preview_result_not_claimed_completed(self):
        opener = Opener(results=[tool({'status': 'needs_confirmation', 'confirm_code': 'synthetic'})])
        client = mcp.MCPClient(server='assist', api_key='synthetic-key', opener=opener)
        result = client.call_tool('submit_to_hri', {'confirmed': False, 'summary': {}}, allow_write=True)
        self.assertEqual(result['submission_status'], 'not_verified_by_client')
        self.assertFalse(result['is_error'])

    def test_write_timeout_outcome_unknown_no_resubmit(self):
        opener = Opener(results=[socket.timeout()])
        client = mcp.MCPClient(server='assist', api_key='synthetic-key', opener=opener)
        error = self.assert_error('request_timeout', client.call_tool, 'submit_to_hri',
            {'confirmed': True, 'summary': {}, 'confirm_code': 'synthetic'}, allow_write=True, confirm_write=True)
        self.assertTrue(error.details['outcome_unknown'])
        self.assertEqual(client.requests, 3)

    def test_doctor_only_bounded_query_tools(self):
        opener = self.doctor_opener()
        client = mcp.MCPClient(opener=opener)
        result = client.doctor(probe=True)
        self.assertTrue(result['ok'])
        self.assertEqual(result['requests'], 5)
        calls = [p['params'] for _, p, _ in opener.history if p['method'] == 'tools/call']
        self.assertEqual([p['name'] for p in calls], ['convert_gpa', 'search_programs'])
        self.assertEqual(calls[1]['arguments']['limit'], 1)
        assist = mcp.MCPClient(server='assist', api_key='synthetic', opener=Opener())
        self.assert_error('probe_query_only', assist.doctor, probe=True)
        self.assertEqual(assist.requests, 0)

    def doctor_opener(self, gpa=None, search=None):
        gpa = {'input': 85, 'from': 'percent', 'results': [{'method': 'synthetic', 'result': 3.7}]} if gpa is None else gpa
        search = {'results': [], 'count': 0, 'offset': 0, 'meta': {}} if search is None else search
        return Opener(pages=[{'tools': [{'name': name} for name in mcp.READ_TOOLS['query']]}],
                      results=[tool(gpa), tool(search)])

    def test_doctor_missing_tools_stops_before_probes(self):
        opener = Opener(results=[tool({}), tool({})])
        result = mcp.MCPClient(opener=opener).doctor(probe=True)
        self.assertFalse(result['ok'])
        self.assertFalse(result['checks']['required_tools'])
        self.assertIn('search_programs', result['missing_tools'])
        self.assertNotIn('convert_gpa', result['checks'])
        self.assertEqual(len(opener.history), 3)

    def test_doctor_rejects_empty_and_invalid_gpa_results(self):
        for result in ({}, {'results': []},
                       {'input': 85, 'from': 'percent', 'results': [{'method': 'bad', 'result': '3.7'}]},
                       {'input': 85, 'from': 'percent', 'results': [{'method': 'bad', 'result': 99}]},
                       {'input': 85, 'from': 'percent', 'results': [{'method': 'ok', 'result': 3.7}], 'ok': False}):
            with self.subTest(result=result):
                opener = self.doctor_opener(gpa=result)
                outcome = mcp.MCPClient(opener=opener).doctor(probe=True)
                self.assertFalse(outcome['ok'])
                self.assertFalse(outcome['checks']['convert_gpa'])
                self.assertEqual(outcome['failed_probe'], 'convert_gpa')
                self.assertEqual(len(opener.history), 4)

    def test_doctor_rejects_invalid_search_and_tool_errors(self):
        for result in ({}, {'results': [], 'count': 1, 'offset': 0, 'meta': {}},
                       {'results': [{}], 'count': 1, 'offset': 0, 'meta': {}},
                       {'results': [], 'count': 0, 'offset': 0, 'meta': {}, 'error': ''},
                       {'results': [], 'count': 0, 'offset': 0, 'meta': {}, 'ok': False}):
            with self.subTest(result=result):
                outcome = mcp.MCPClient(opener=self.doctor_opener(search=result)).doctor(probe=True)
                self.assertFalse(outcome['ok'])
                self.assertTrue(outcome['checks']['convert_gpa'])
                self.assertFalse(outcome['checks']['search_programs'])
        opener = self.doctor_opener()
        opener.results[0]['isError'] = True
        outcome = mcp.MCPClient(opener=opener).doctor(probe=True)
        self.assertEqual(outcome['error'], 'probe_tool_error')

    def test_doctor_accepts_bounded_program_shape(self):
        result = {'results': [{'id': 'synthetic-1', 'name': 'Synthetic program', 'country': 'SG'}],
                  'count': 1, 'offset': 0, 'meta': {}}
        self.assertTrue(mcp.MCPClient(opener=self.doctor_opener(search=result)).doctor(probe=True)['ok'])

    def test_deep_json_and_sse_write_responses_preserve_unknown_outcome(self):
        for depth, media, text_content in ((80, 'application/json', False),
                                           (1200, 'application/json', False),
                                           (1200, 'text/event-stream', False),
                                           (1200, 'application/json', True)):
            with self.subTest(depth=depth, media=media, text_content=text_content):
                nested = '[' * depth + '0' + ']' * depth
                if text_content:
                    body = json.dumps({'jsonrpc': '2.0', 'id': 2,
                                       'result': {'content': [{'type': 'text', 'text': nested}]}}).encode()
                else:
                    body = b'{"jsonrpc":"2.0","id":2,"result":{"structuredContent":{"nested":' + nested.encode() + b'}}}'
                if media == 'text/event-stream':
                    body = b'data: ' + body + b'\n\n'
                opener = Opener(results=[Response(body, media=media)])
                client = mcp.MCPClient(server='assist', api_key='synthetic-key', opener=opener)
                error = self.assert_error('json_depth_limit', client.call_tool, 'submit_to_hri',
                    {'confirmed': True, 'summary': {}, 'confirm_code': 'synthetic'}, allow_write=True, confirm_write=True)
                self.assertTrue(error.details['outcome_unknown'])
                self.assertEqual(client.requests, 3)

    def test_invalid_expiry_types_and_huge_integer_are_bounded(self):
        for expires in (10 ** 10000, None, [], {}, True, float('inf'), float('nan')):
            self.assert_error('pow_expired_or_invalid_ttl', mcp.validate_challenge, challenge(expires_at=expires))
        opener = Opener(results=[tool({'error': 'pow_required', 'pow': challenge(expires_at=10 ** 400)})])
        client = mcp.MCPClient(opener=opener)
        with mock.patch.object(client._pow, 'handle_response') as handle:
            self.assert_error('pow_expired_or_invalid_ttl', client.call_tool, 'convert_gpa', {'score': 85})
            handle.assert_not_called()
        self.assertEqual(client.requests, 3)

    def test_deep_arguments_and_nonfinite_json_rejected(self):
        nested = '[' * 80 + '0' + ']' * 80
        self.assert_error('json_depth_limit', mcp.parse_json, nested)
        self.assert_error('invalid_response_encoding', mcp.parse_json, '1e400')
        arguments = {'score': json.loads(nested)}
        client = mcp.MCPClient(opener=Opener())
        self.assert_error('json_depth_limit', client.call_tool, 'convert_gpa', arguments)
        self.assertEqual(client.requests, 0)

    def test_argument_restrictions_before_network(self):
        client = mcp.MCPClient(opener=Opener())
        for arguments, error in [([], 'invalid_arguments'), ({'score': float('nan')}, 'invalid_arguments'),
                                 ({'pow_nonce': '0'}, 'reserved_pow_arguments'),
                                 ({'score': 'x' * mcp.MAX_ARGUMENTS}, 'arguments_too_large')]:
            self.assert_error(error, client.call_tool, 'convert_gpa', arguments)
        self.assert_error('unsupported_tool', client.call_tool, 'future_write_tool', {})
        self.assertEqual(client.requests, 0)

    def test_cli_args_file_and_key_echo_redaction(self):
        fake = mock.Mock(requests=3)
        fake.call_tool.return_value = {'result': {'echo': 'synthetic-key'}, 'is_error': False, 'pow_retried': False}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'args.json'
            path.write_text('{"school":"example"}', encoding='utf-8')
            out = io.StringIO()
            with mock.patch.object(mcp, 'MCPClient', return_value=fake), \
                    mock.patch.dict(os.environ, {'LIUXUE_API_KEY': 'synthetic-key'}, clear=True), redirect_stdout(out):
                code = mcp.main(['call', '--server', 'assist', '--tool', 'material_checklist', '--args-file', str(path)])
            self.assertEqual(code, 0)
            self.assertNotIn('synthetic-key', out.getvalue())
            self.assertEqual(json.loads(out.getvalue())['result']['echo'], '[REDACTED]')
            fake.call_tool.assert_called_once_with('material_checklist', {'school': 'example'}, False, False)

    def test_cli_oversized_args_file_never_calls_service(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'args.json'
            path.write_bytes(b'x' * (mcp.MAX_ARGUMENTS + 20))
            out = io.StringIO()
            with redirect_stdout(out):
                code = mcp.main(['call', '--tool', 'convert_gpa', '--args-file', str(path)])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(out.getvalue())['error'], 'arguments_too_large')


if __name__ == '__main__':
    unittest.main()
