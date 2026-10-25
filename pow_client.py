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
    """Keep the latest proof; callers own the single business-request retry."""

    def __init__(self):
        self.session_id: str | None = None
        self._next: dict | None = None  # {challenge_id, nonce, difficulty, expires_at}
        self._lock = threading.Lock()
        self._bg: threading.Thread | None = None
        self._generation = 0

    @staticmethod
    def _unexpired(expires_at):
        return expires_at is None or time.time() < expires_at

    def _ready(self):
        # Called with _lock held.
        if self._next and not self._unexpired(self._next['expires_at']):
            self._next = None
        return self._next is not None

    def _new_challenge(self, pow_obj: dict, default_difficulty: int):
        cid = pow_obj.get('challenge_id') or pow_obj.get('challenge')
        diff = int(pow_obj.get('difficulty') or default_difficulty)
        expires = pow_obj.get('expires_at')
        expires = float(expires) if expires is not None else None
        with self._lock:
            sid = pow_obj.get('session_id') or self.session_id
            if not sid or not cid:
                return None
            self.session_id = sid
            self._generation += 1
            self._next = None
            self._bg = None
            return sid, cid, diff, expires, self._generation

    def _current(self, generation, expires):
        # Called with _lock held; stale workers may finish but cannot publish.
        return generation == self._generation and self._unexpired(expires)

    def attach_args(self, kwargs: dict) -> dict:
        out = dict(kwargs)
        with self._lock:
            if self.session_id:
                out['pow_session'] = self.session_id
            if self._ready():
                out['pow_challenge_id'] = self._next['challenge_id']
                out['pow_nonce'] = self._next['nonce']
                # consume — one-time
                self._next = None
        return out

    def _start_prefetch(self, pow_obj: dict):
        if not isinstance(pow_obj, dict):
            return
        challenge = self._new_challenge(pow_obj, 16)
        if challenge is None:
            return
        sid, cid, diff, expires, generation = challenge

        def run():
            try:
                nonce = solve(cid, sid, diff)
                with self._lock:
                    if self._current(generation, expires):
                        self._next = {'challenge_id': cid, 'nonce': nonce,
                                      'difficulty': diff, 'expires_at': expires}
            except Exception:
                pass

        with self._lock:
            if self._current(generation, expires):
                self._bg = threading.Thread(target=run, name='pow-prefetch', daemon=True)
                self._bg.start()

    def handle_response(self, res: Any) -> Any:
        """Suggest one retry for a fresh required/invalid challenge; never send it here."""
        if not isinstance(res, dict):
            return 'ok', res
        pow_obj = res.get('pow')
        if res.get('error') in ('pow_required', 'pow_invalid') and isinstance(pow_obj, dict):
            challenge = self._new_challenge(pow_obj, 12)
            if challenge is None:
                return 'ok', res
            sid, cid, diff, expires, generation = challenge
            with self._lock:
                if not self._current(generation, expires):
                    return 'ok', res
            nonce = solve(cid, sid, diff)
            with self._lock:
                if self._current(generation, expires):
                    return 'retry', {'pow_session': sid, 'pow_challenge_id': cid, 'pow_nonce': nonce}
            return 'ok', res
        if pow_obj:
            self._start_prefetch(pow_obj)
        return 'ok', res

    def wait_prefetch(self, timeout: float = 5.0) -> bool:
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if self._ready():
                    return True
                bg = self._bg
            if bg:
                bg.join(0.05)
            else:
                time.sleep(0.05)
        with self._lock:
            return self._ready()


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
