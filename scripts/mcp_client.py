#!/usr/bin/env python3
"""Small, bounded Streamable HTTP client for the two study-abroad MCPs."""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pow_client import ALGO, PowPipeline  # noqa: E402

ENDPOINTS = {
    'query': 'https://hi.xwoork.cn/info/mcp',
    'assist': 'https://hi.xwoork.cn/assist/mcp',
}
VERSIONS = ('2025-06-18', '2025-03-26')
CORE_READ_TOOLS = {
    'query': frozenset(('search_programs', 'get_program', 'compare_programs',
        'match_cases', 'recommend_tiers', 'assess_schools', 'shortlist_programs',
        'rankings', 'scholarships', 'estimate_cost', 'visa_checklist', 'convert_gpa')),
    'assist': frozenset(('material_checklist', 'essay_assist', 'plan_timeline', 'open_advisor')),
}
KNOWLEDGE_TOOLS = frozenset(('search_knowledge', 'get_knowledge'))
INTAKE_TOOLS = frozenset(('start_intake', 'get_intake_status', 'upload_intake_file'))
READ_TOOLS = {
    'query': CORE_READ_TOOLS['query'] | KNOWLEDGE_TOOLS,
    'assist': CORE_READ_TOOLS['assist'] | frozenset(('get_intake_status',)),
}
WRITE_TOOL = 'submit_to_hri'
MAX_RESPONSE = 2 * 1024 * 1024
MAX_ARGUMENTS = 48 * 1024
MAX_JSON_DEPTH = 64


class ClientError(Exception):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def as_dict(self):
        return {'ok': False, 'error': self.code, 'message': self.message, **self.details}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward Authorization, session IDs or business arguments elsewhere.
        return None


def parse_json(value):
    def reject_constant(_):
        raise ValueError('non-finite JSON number')
    result = json.loads(value, parse_constant=reject_constant)
    pending = [(result, 0)]
    while pending:
        item, depth = pending.pop()
        if depth > MAX_JSON_DEPTH:
            raise ClientError('json_depth_limit', 'JSON 嵌套超过 64 层；停止处理。')
        if isinstance(item, float) and not math.isfinite(item):
            raise ClientError('invalid_response_encoding', 'JSON 不支持无穷大或 NaN。')
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return result


def private_key(server, key_file=None, environ=None):
    if server == 'query':
        if key_file:
            raise ClientError('query_is_anonymous', '查询模式不读取助手密钥文件。')
        return None
    environ = os.environ if environ is None else environ
    if key_file:
        try:
            flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
            with os.fdopen(os.open(Path(key_file).expanduser(), flags), 'r', encoding='utf-8') as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode):
                    raise OSError('not a regular file')
                if os.name == 'posix' and (info.st_uid != os.getuid() or info.st_mode & 0o077):
                    raise OSError('private ownership/permissions required')
                value = handle.read(513).strip()
        except (OSError, UnicodeError):
            raise ClientError('key_file_unavailable', '密钥文件须为本人可读的私密普通文件；POSIX 权限使用 600。') from None
    else:
        value = environ.get('LIUXUE_API_KEY', '').strip()
    if not value:
        return None  # student tools on the assistant are anonymous; a key is only for operator scopes

    if len(value) > 512 or any(ord(c) <= 32 or ord(c) >= 127 for c in value):
        raise ClientError('invalid_key_format', '个人密钥格式不正确。')
    return value


def redact(value, key):
    if not key:
        return value
    if isinstance(value, str):
        return value.replace(key, '[REDACTED]')
    if isinstance(value, list):
        return [redact(item, key) for item in value]
    if isinstance(value, dict):
        return {redact(k, key): redact(v, key) for k, v in value.items()}
    return value


def decode_tool_result(result):
    if not isinstance(result, dict):
        raise ClientError('invalid_tool_result', 'MCP 工具结果格式不正确。')
    if isinstance(result.get('structuredContent'), dict):
        data = result['structuredContent']
    else:
        content = result.get('content', [])
        if not isinstance(content, list):
            raise ClientError('invalid_tool_result', 'MCP content 须为内容列表。')
        texts = [item['text'] for item in content
                 if isinstance(item, dict) and item.get('type') == 'text'
                 and isinstance(item.get('text'), str)]
        text = '\n'.join(texts)
        try:
            data = parse_json(text)
        except RecursionError:
            raise ClientError('json_depth_limit', '工具文本 JSON 嵌套过深；停止处理。') from None
        except (ValueError, TypeError):
            data = {'text': text} if texts else {'content': result.get('content', [])}
    return data, bool(result.get('isError'))


def validate_challenge(challenge, now=None):
    now = time.time() if now is None else now
    if not isinstance(challenge, dict) or challenge.get('algo') != ALGO:
        raise ClientError('invalid_pow_challenge', 'PoW 算法缺失或不受支持；未开始计算。')
    for field in ('session_id', 'challenge_id'):
        value = challenge.get(field)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
            raise ClientError('invalid_pow_challenge', 'PoW 会话或挑战编号格式不正确。')
    difficulty = challenge.get('difficulty')
    expires = challenge.get('expires_at')
    if type(difficulty) is not int or not 1 <= difficulty <= 24:
        raise ClientError('pow_difficulty_limit', 'PoW 难度超出客户端支持范围 1–24；未开始计算。')
    try:
        valid_ttl = (type(expires) in (int, float) and math.isfinite(float(expires))
                     and 0 < float(expires) - now <= 300)
    except (OverflowError, TypeError, ValueError):
        valid_ttl = False
    if not valid_ttl:
        raise ClientError('pow_expired_or_invalid_ttl', 'PoW 已过期或有效期超过 300 秒；未开始计算。')
    return challenge


class MCPClient:
    """One reusable client/prefetch pipeline; no automatic network retry or reconnect."""
    def __init__(self, server='query', api_key=None, timeout=10, opener=None, max_requests=8):
        if server not in ENDPOINTS:
            raise ClientError('invalid_server', 'server 只能为 query 或 assist。')
        if not 0 < timeout <= 30 or not 1 <= max_requests <= 32:
            raise ClientError('invalid_limits', '请求超时须在 0–30 秒，数量上限须在 1–32。')
        if server == 'query' and api_key:
            raise ClientError('query_is_anonymous', '查询客户端不发送助手个人密钥。')
        if api_key and (len(api_key) > 512 or any(ord(c) <= 32 or ord(c) >= 127 for c in api_key)):
            raise ClientError('invalid_key_format', '个人密钥格式不正确。')
        self.server, self.url, self.api_key = server, ENDPOINTS[server], api_key
        self.timeout, self.max_requests = timeout, max_requests
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.requests = 0
        self.protocol_version = None
        self.session_id = None
        self.server_info = None
        self._id = 0
        self._pow = PowPipeline()

    def _message(self, payload, request_id):
        if not isinstance(payload, dict) or payload.get('jsonrpc') != '2.0':
            raise ClientError('invalid_rpc_response', '收到无效 JSON-RPC 响应。')
        if 'method' in payload:
            if 'id' in payload:
                raise ClientError('unsupported_server_request', '本客户端未协商服务端主动请求能力。')
            return None
        if payload.get('id') != request_id or type(payload.get('id')) is not type(request_id):
            raise ClientError('rpc_id_mismatch', 'JSON-RPC 响应编号不匹配。')
        if 'error' in payload:
            err = payload['error']
            raise ClientError('rpc_error', 'MCP 返回协议错误；未自动重试。',
                              rpc_code=err.get('code') if isinstance(err, dict) else None)
        if 'result' not in payload:
            raise ClientError('invalid_rpc_response', 'JSON-RPC 响应缺少 result。')
        return payload['result']

    def _read_result(self, response, request_id, deadline):
        media_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if media_type not in ('application/json', 'text/event-stream'):
            raise ClientError('unsupported_content_type', 'MCP 返回了不支持的内容格式。')
        body, pending, event, total = bytearray(), bytearray(), [], 0
        while True:
            if time.monotonic() >= deadline:
                raise ClientError('request_timeout', 'MCP 请求超过时限；未自动重试。')
            chunk = response.read1(8192)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_RESPONSE:
                raise ClientError('response_too_large', 'MCP 响应超过 2 MiB 上限。')
            if media_type == 'application/json':
                body.extend(chunk)
                continue
            pending.extend(chunk)
            while b'\n' in pending:
                line, _, rest = pending.partition(b'\n')
                pending = bytearray(rest)
                line = line.rstrip(b'\r')
                if line.startswith(b'data:'):
                    event.append(line[5:].lstrip(b' '))
                elif not line and event:
                    payload = parse_json(b'\n'.join(event).decode('utf-8'))
                    event = []
                    result = self._message(payload, request_id)
                    if result is not None:
                        return result
        if media_type == 'text/event-stream':
            raise ClientError('incomplete_sse', 'SSE 在对应请求结果到达前关闭；未重发请求。')
        return self._message(parse_json(body.decode('utf-8')), request_id)

    def _post(self, payload, request_id=None):
        if self.requests >= self.max_requests:
            raise ClientError('request_budget_exceeded', '本次命令已达请求数量上限。')
        headers = {'Content-Type': 'application/json', 'Accept': 'application/json, text/event-stream',
                   'User-Agent': 'study-abroad-planner/0.8.0'}
        if self.api_key:
            headers['Authorization'] = 'Bearer ' + self.api_key
        if self.protocol_version:
            headers['MCP-Protocol-Version'] = self.protocol_version
        if self.session_id:
            headers['Mcp-Session-Id'] = self.session_id
        request = urllib.request.Request(self.url, json.dumps(payload).encode('utf-8'), headers, method='POST')
        self.requests += 1
        deadline = time.monotonic() + self.timeout
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                if payload.get('method') == 'initialize':
                    sid = response.headers.get('Mcp-Session-Id')
                    if sid:
                        if len(sid) > 256 or any(ord(c) < 33 or ord(c) > 126 for c in sid):
                            raise ClientError('invalid_session_id', 'MCP 会话编号格式不正确。')
                        self.session_id = sid
                if request_id is None:
                    if response.status != 202:
                        raise ClientError('invalid_notification_status', '初始化通知未被按协议接受。')
                    return None
                if response.status != 200:
                    raise ClientError('unexpected_http_status', 'MCP 请求未返回正常结果。', http_status=response.status)
                return self._read_result(response, request_id, deadline)
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get('Retry-After', '') if exc.headers else ''
            details = {'http_status': exc.code}
            if retry_after.isdigit():
                details['retry_after_seconds'] = min(int(retry_after), 86400)
            code = 'redirect_blocked' if 300 <= exc.code < 400 else 'http_error'
            # Do not print arbitrary HTTP bodies, URLs, headers or credential echoes.
            exc.close()
            raise ClientError(code, 'MCP HTTP 请求失败；未自动重试。', **details) from None
        except (TimeoutError, socket.timeout):
            raise ClientError('request_timeout', 'MCP 请求超时；未自动重试。') from None
        except urllib.error.URLError as exc:
            code = 'request_timeout' if isinstance(exc.reason, (TimeoutError, socket.timeout)) else 'connection_error'
            raise ClientError(code, 'MCP 连接失败；未自动重试。') from None
        except (OSError, http.client.HTTPException):
            raise ClientError('connection_error', 'MCP 连接中断；未自动重试。') from None
        except RecursionError:
            raise ClientError('json_depth_limit', 'MCP JSON 嵌套过深；停止处理且不重试。') from None
        except (ValueError, UnicodeError, OverflowError):
            raise ClientError('invalid_response_encoding', 'MCP 返回的 JSON 或 UTF-8 无效。') from None

    def _rpc(self, method, params=None):
        self._id += 1
        payload = {'jsonrpc': '2.0', 'id': self._id, 'method': method}
        if params is not None:
            payload['params'] = params
        return self._post(payload, self._id)

    def initialize(self):
        if self.protocol_version:
            return self.server_info
        result = self._rpc('initialize', {'protocolVersion': VERSIONS[0], 'capabilities': {},
                           'clientInfo': {'name': 'study-abroad-planner', 'version': '0.8.0'}})
        if not isinstance(result, dict) or result.get('protocolVersion') not in VERSIONS:
            raise ClientError('unsupported_protocol', '服务端协商的 MCP 协议版本不受支持。')
        capabilities = result.get('capabilities')
        if not isinstance(capabilities, dict) or 'tools' not in capabilities:
            raise ClientError('tools_unavailable', '服务端未声明 tools 能力。')
        self.protocol_version = result['protocolVersion']
        self.server_info = result.get('serverInfo', {})
        try:
            self._post({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except ClientError:
            self.protocol_version = None
            raise
        return self.server_info

    def list_tools(self):
        self.initialize()
        tools, cursor, seen = [], None, set()
        for _ in range(4):
            result = self._rpc('tools/list', {'cursor': cursor} if cursor else {})
            if not isinstance(result, dict) or not isinstance(result.get('tools'), list):
                raise ClientError('invalid_tool_list', '工具目录格式不正确。')
            tools.extend(result['tools'])
            cursor = result.get('nextCursor')
            if not cursor:
                return tools
            if not isinstance(cursor, str) or cursor in seen:
                raise ClientError('invalid_tool_cursor', '工具目录出现无效或重复分页游标。')
            seen.add(cursor)
        raise ClientError('tool_page_limit', '工具目录超过 4 页，未继续请求。')

    def call_tool(self, name, arguments, allow_write=False, confirm_write=False):
        if not isinstance(arguments, dict):
            raise ClientError('invalid_arguments', '工具参数须为 JSON 对象。')
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except (TypeError, ValueError, RecursionError, OverflowError):
            raise ClientError('invalid_arguments', '参数必须为有效 JSON，不能含 NaN 或无穷大。') from None
        if len(encoded) > MAX_ARGUMENTS:
            raise ClientError('arguments_too_large', '工具参数超过 48 KiB 上限。')
        parse_json(encoded)
        if any(not isinstance(k, str) or k.startswith('pow_') for k in arguments):
            raise ClientError('reserved_pow_arguments', 'pow_ 参数由客户端管理。')
        is_submission = name == WRITE_TOOL
        is_intake = self.server == 'assist' and name in INTAKE_TOOLS
        intake_mutation = is_intake and (name == 'start_intake' or
            (name == 'upload_intake_file' and bool(arguments.get('content_base64'))))
        is_mutation = intake_mutation or (is_submission and arguments.get('confirmed') is True)
        if intake_mutation and not allow_write:
            raise ClientError('write_not_allowed', '创建材料票或上传文件需要对应学生授权及显式 --allow-write。')
        if is_submission:
            if self.server != 'assist' or not allow_write:
                raise ClientError('write_not_allowed', 'submit_to_hri 需要助手服务与显式 --allow-write。')
            if type(arguments.get('confirmed')) is not bool:
                raise ClientError('confirmation_required', '请明确提供 confirmed=false 预览或 confirmed=true 确认。')
            if arguments['confirmed'] and (not confirm_write or not arguments.get('confirm_code')
                                            or not isinstance(arguments.get('summary'), dict)):
                raise ClientError('confirmation_required', '确认阶段需要 --confirm-write、原摘要及服务返回的 confirm_code。')
        elif not is_intake and name not in READ_TOOLS[self.server]:
            raise ClientError('unsupported_tool', '该工具不属于本版本支持的服务工具名单。')
        self.initialize()
        prepared = self._pow.attach_args(arguments)
        retried = False
        for attempt in range(2):
            try:
                raw = self._rpc('tools/call', {'name': name, 'arguments': prepared})
                data, is_error = decode_tool_result(raw)
            except ClientError as exc:
                if is_mutation:
                    exc.details['outcome_unknown'] = True
                raise
            error = data.get('error') if isinstance(data, dict) else None
            if isinstance(error, str) and error in ('pow_required', 'pow_invalid') and attempt == 0:
                validate_challenge(data.get('pow'))
                try:
                    action, proof = self._pow.handle_response(data)
                except TimeoutError:
                    raise ClientError('pow_solve_limit', 'PoW 达到 500 万次求解上限；未重试业务请求。') from None
                if action == 'retry':
                    prepared = {**arguments, **proof}
                    retried = True
                    continue
            business_error = (isinstance(data, dict) and
                              (('error' in data and error is not None) or data.get('ok') is False))
            outcome = {'result': data, 'is_error': business_error or is_error, 'pow_retried': retried}
            if not outcome['is_error'] and isinstance(data, dict) and data.get('pow'):
                try:
                    validate_challenge(data['pow'])
                    self._pow.handle_response(data)
                except ClientError as exc:
                    # A bad *next* challenge must not erase an already received result.
                    outcome['warnings'] = [exc.as_dict()]
            if is_submission:
                outcome['submission_status'] = 'not_verified_by_client'
            return outcome
        raise AssertionError('unreachable')

    def doctor(self, probe=False):
        if probe and self.server != 'query':
            raise ClientError('probe_query_only', 'doctor --probe 仅对匿名查询服务执行只读样例。')
        tools = self.list_tools()
        result = {'server': self.server, 'protocol_version': self.protocol_version,
                  'tool_count': len(tools), 'tool_names': [t.get('name') for t in tools if isinstance(t, dict)],
                  'checks': {'initialize': True, 'tools_list': True}}
        available = {name for name in result['tool_names'] if isinstance(name, str)}
        missing = sorted(CORE_READ_TOOLS[self.server] - available)
        if self.server == 'query':
            result['features'] = {'knowledge': KNOWLEDGE_TOOLS <= available}
        else:
            result['features'] = {'intake': INTAKE_TOOLS <= available}
        result['checks']['required_tools'] = not missing
        if missing:
            return {**result, 'ok': False, 'error': 'required_tools_missing',
                    'missing_tools': missing, 'requests': self.requests}
        if probe:
            gpa = self.call_tool('convert_gpa', {'score': 85, 'from_scale': 'percent', 'to_scale': '4.0'})
            data = gpa['result']
            valid_gpa = (isinstance(data, dict) and data.get('input') == 85 and data.get('from') == 'percent'
                         and isinstance(data.get('results'), list) and bool(data['results'])
                         and all(isinstance(row, dict) and isinstance(row.get('method'), str) and row['method']
                                 and type(row.get('result')) in (int, float) and 0 <= row['result'] <= 4.0
                                 for row in data['results']))
            result['checks']['convert_gpa'] = bool(not gpa['is_error'] and valid_gpa)
            if not result['checks']['convert_gpa']:
                return {**result, 'ok': False, 'failed_probe': 'convert_gpa',
                        'error': 'probe_tool_error' if gpa['is_error'] else 'invalid_probe_result',
                        'requests': self.requests}
            sample = self.call_tool('search_programs', {'country': 'SG', 'keyword': 'computer', 'limit': 1})
            data = sample['result']
            valid_search = (isinstance(data, dict) and isinstance(data.get('results'), list)
                            and type(data.get('count')) is int and data['count'] == len(data['results']) <= 1
                            and type(data.get('offset')) is int and data['offset'] == 0
                            and isinstance(data.get('meta'), dict)
                            and all(isinstance(row, dict) and isinstance(row.get('id'), str) and row['id']
                                    and isinstance(row.get('name'), str) and row['name'] and row.get('country') == 'SG'
                                    for row in data['results']))
            result['checks']['search_programs'] = bool(not sample['is_error'] and valid_search)
            result['pow_retried'] = sample['pow_retried']
            if not result['checks']['search_programs']:
                return {**result, 'ok': False, 'failed_probe': 'search_programs',
                        'error': 'probe_tool_error' if sample['is_error'] else 'invalid_probe_result',
                        'requests': self.requests}
        return {**result, 'ok': True, 'requests': self.requests}


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('list-tools', 'call', 'doctor'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--server', choices=ENDPOINTS, default='query')
        cmd.add_argument('--key-file', type=Path)
        cmd.add_argument('--timeout', type=float, default=10)
        if name == 'call':
            cmd.add_argument('--tool', required=True)
            inputs = cmd.add_mutually_exclusive_group()
            inputs.add_argument('--args-json', default='{}')
            inputs.add_argument('--args-file', type=Path)
            cmd.add_argument('--allow-write', action='store_true')
            cmd.add_argument('--confirm-write', action='store_true')
        elif name == 'doctor':
            cmd.add_argument('--probe', action='store_true')
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    key = None
    try:
        key = private_key(args.server, args.key_file)
        client = MCPClient(args.server, api_key=key, timeout=args.timeout)
        if args.command == 'list-tools':
            output = {'ok': True, 'server': args.server, 'tools': client.list_tools(), 'requests': client.requests}
        elif args.command == 'doctor':
            output = client.doctor(probe=args.probe)
        else:
            try:
                if args.args_file:
                    with args.args_file.open('rb') as handle:
                        raw = handle.read(MAX_ARGUMENTS + 1).decode('utf-8')
                else:
                    raw = args.args_json
                if len(raw.encode('utf-8')) > MAX_ARGUMENTS:
                    raise ClientError('arguments_too_large', '工具参数文件超过 48 KiB 上限。')
                arguments = parse_json(raw)
            except (OSError, UnicodeError, ValueError, RecursionError, OverflowError):
                raise ClientError('invalid_arguments', '无法读取有效的 JSON 工具参数。') from None
            outcome = client.call_tool(args.tool, arguments, args.allow_write, args.confirm_write)
            output = {'ok': not outcome['is_error'], 'server': args.server, 'tool': args.tool,
                      'result': outcome['result'], 'pow_retried': outcome['pow_retried'], 'requests': client.requests}
            for field in ('warnings', 'submission_status'):
                if field in outcome:
                    output[field] = outcome[field]
        print(json.dumps(redact(output, key), ensure_ascii=False))
        return 0 if output.get('ok') else 1
    except ClientError as exc:
        print(json.dumps(redact(exc.as_dict(), key), ensure_ascii=False))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
