import io
import json
import runpy
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

SOURCE = Path(__file__).resolve().parents[1] / 'agent/qwen_control.py'
with tempfile.TemporaryDirectory() as home:
    config = Path(home) / '.config/qwen-control/config.env'
    config.parent.mkdir(parents=True)
    config.write_text('QWEN_AGENT_TOKEN=unit-test-only\nQWEN_AGENT_BIND=100.64.0.2\n')
    with patch.object(Path, 'home', return_value=Path(home)):
        module = runpy.run_path(str(SOURCE))
Handler = module['Handler']
globals_ = Handler.do_POST.__globals__


class AgentSecurityTests(unittest.TestCase):
    def handler(self, *, peer='127.0.0.1', token='unit-test-only', body=b'{}', host='127.0.0.1:8121', origin=None, content_type='application/json'):
        h = object.__new__(Handler)
        h.client_address = (peer, 1)
        h.server = SimpleNamespace(server_port=8121)
        h.connection = Mock()
        h.headers = Message()
        h.headers['Host'] = host
        h.headers['Content-Type'] = content_type
        h.headers['Content-Length'] = str(len(body))
        if token is not None: h.headers['Authorization'] = 'Bearer ' + token
        if origin is not None: h.headers['Origin'] = origin
        h.rfile = io.BytesIO(body)
        h.path = '/localai/stop'
        h._dispatch = Mock(return_value=(True, 'mock only', {}))
        h._json = Mock()
        return h

    def post(self, h):
        with patch.dict(globals_, {'build_state': lambda: {}}):
            h.do_POST()
        return h._json.call_args.args[0]

    def test_loopback_and_tailnet_both_require_token(self):
        for peer in ('127.0.0.1', '::1', '100.64.0.3'):
            for token in (None, 'wrong'):
                h = self.handler(peer=peer, token=token)
                self.assertEqual(self.post(h), 401)
                h._dispatch.assert_not_called()

    def test_authenticated_native_clients_work(self):
        for peer in ('127.0.0.1', '100.64.0.3'):
            h = self.handler(peer=peer)
            self.assertEqual(self.post(h), 200)
            h._dispatch.assert_called_once_with('/localai/stop', {})

    def test_rebinding_and_cross_origin_requests_rejected(self):
        for kwargs in ({'host':'attacker.example:8121'}, {'origin':'https://attacker.example'}, {'origin':'null'}, {'host':'127.0.0.1:9999'}):
            h = self.handler(**kwargs)
            self.assertEqual(self.post(h), 403)
            h._dispatch.assert_not_called()

    def test_invalid_bodies_and_framing_never_dispatch(self):
        for body in (b'', b'not json', b'[]', b'null', b' ' * 4097):
            h = self.handler(body=body)
            self.assertEqual(self.post(h), 400)
            h._dispatch.assert_not_called()
        h = self.handler(content_type='text/plain')
        self.assertEqual(self.post(h), 400)
        for name, value in [('Content-Length','2'), ('Transfer-Encoding','chunked')]:
            h = self.handler()
            h.headers[name] = value
            self.assertEqual(self.post(h), 400)

    def test_slow_body_has_total_deadline(self):
        h = self.handler(body=b'{}')
        h.rfile = Mock()
        h.rfile.read1.return_value = b'{'
        with patch.dict(globals_, {'build_state':lambda:{}}), patch.object(module['time'], 'monotonic', side_effect=[0, 1, 6]):
            self.assertEqual(self.post(h), 408)
        h._dispatch.assert_not_called()

    def test_health_open_but_state_authenticated(self):
        h = self.handler(token=None)
        h.path = '/health'
        h.do_GET()
        self.assertEqual(h._json.call_args.args[0], 200)
        h.path = '/state'
        h.do_GET()
        self.assertEqual(h._json.call_args.args[0], 401)


if __name__ == '__main__': unittest.main()
