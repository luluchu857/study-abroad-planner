"""Skill / 客户端 PoW 助手：预发挑战 → 后台预计算 → 下次请求带上 proof。

协议（与 mcp/pow.py 一致）：
1) 冷启动：不带 proof 调工具 → 收到 error=pow_required + pow{session_id,challenge_id,difficulty,...}
2) 本地 solve → 同一参数重试，附 pow_session / pow_challenge_id / pow_nonce
3) 成功响应里的 pow 是【下一轮】挑战；立刻后台开始算，不要等下次请求再算
4) 持 API Key 也不免 PoW
"""
from __future__ import annotations
import hashlib, threading, time
from typing import Any

ALGO = 'sha256-leading-zero-bits'


def leading_zero_bits(digest: bytes) -> int:
    n = 0
    for b in digest:
        if b == 0:
            n += 8
            continue
        n += 8 - b.bit_length()
        break
    return n


def solve(challenge_id: str, session_id: str, difficulty: int, max_iters: int = 5_000_000) -> str:
    for i in range(max_iters):
        nonce = str(i)
        dig = hashlib.sha256(f'{challenge_id}:{nonce}:{session_id}'.encode()).digest()
        if leading_zero_bits(dig) >= difficulty:
            return nonce
    raise TimeoutError('pow_solve_timeout')


class PowPipeline:
    """Keep session + precomputed next proof between tool calls."""

    def __init__(self):
        self.session_id: str | None = None
        self._next: dict | None = None  # {challenge_id, nonce, difficulty}
        self._lock = threading.Lock()
        self._bg: threading.Thread | None = None

    def attach_args(self, kwargs: dict) -> dict:
        out = dict(kwargs)
        with self._lock:
            if self.session_id:
                out['pow_session'] = self.session_id
            if self._next:
                out['pow_challenge_id'] = self._next['challenge_id']
                out['pow_nonce'] = self._next['nonce']
                # consume — one-time
                self._next = None
        return out

    def _start_prefetch(self, pow_obj: dict):
        if not isinstance(pow_obj, dict):
            return
        sid = pow_obj.get('session_id') or self.session_id
        cid = pow_obj.get('challenge_id') or pow_obj.get('challenge')
        diff = int(pow_obj.get('difficulty') or 16)
        if not sid or not cid:
            return
        self.session_id = sid

        def run():
            try:
                nonce = solve(cid, sid, diff)
                with self._lock:
                    self._next = {'challenge_id': cid, 'nonce': nonce, 'difficulty': diff}
            except Exception:
                pass

        if self._bg and self._bg.is_alive():
            # abandon previous; new challenge supersedes
            pass
        self._bg = threading.Thread(target=run, name='pow-prefetch', daemon=True)
        self._bg.start()

    def handle_response(self, res: Any) -> Any:
        """If pow_required: solve sync and return ('retry', pow_args). Else start prefetch; return ('ok', res)."""
        if not isinstance(res, dict):
            return 'ok', res
        pow_obj = res.get('pow')
        if res.get('error') == 'pow_required' and isinstance(pow_obj, dict):
            sid = pow_obj.get('session_id') or self.session_id or ''
            cid = pow_obj.get('challenge_id') or pow_obj.get('challenge')
            diff = int(pow_obj.get('difficulty') or 12)
            self.session_id = sid
            nonce = solve(cid, sid, diff)
            return 'retry', {'pow_session': sid, 'pow_challenge_id': cid, 'pow_nonce': nonce}
        if pow_obj:
            self._start_prefetch(pow_obj)
        return 'ok', res

    def wait_prefetch(self, timeout: float = 5.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self._next:
                    return True
            if self._bg:
                self._bg.join(0.05)
            else:
                time.sleep(0.05)
        with self._lock:
            return self._next is not None


if __name__ == '__main__':
    # 命令行：python3 pow_client.py solve <challenge_id> <session_id> <difficulty>
    # 只在 stdout 打印 nonce（不要自己再写求解脚本；失败不要循环重试）
    import sys
    if len(sys.argv) == 5 and sys.argv[1] == 'solve':
        try:
            print(solve(sys.argv[2], sys.argv[3], int(sys.argv[4])))
        except Exception as e:  # noqa: BLE001
            print(f'ERROR:{e}', file=sys.stderr)
            sys.exit(1)
    else:
        print('usage: python3 pow_client.py solve <challenge_id> <session_id> <difficulty>', file=sys.stderr)
        sys.exit(2)
