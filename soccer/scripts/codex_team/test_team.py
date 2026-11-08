import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import team


class TeamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        (self.state / 'phase.json').write_text(json.dumps({'phase': 'meeting', 'deadline': 0}))

    def test_chat_identity_cursor_and_history(self):
        team.chat(self.state, 'mac', 'send', {'message': '我来回防'})
        self.assertEqual(team.chat(self.state, 'mac', 'inbox', {}), [])
        self.assertEqual(team.chat(self.state, 'agent', 'status', {})['unread'], 1)
        self.assertEqual(team.chat(self.state, 'agent', 'inbox', {})[0]['body'], '我来回防')
        self.assertEqual(team.chat(self.state, 'agent', 'inbox', {}), [])
        self.assertEqual(len(team.chat(self.state, 'agent', 'log', {})), 1)
        self.assertEqual(team.chat(self.state, 'agent', 'clock', {})['remaining_seconds'], 0)

    def packet(self, path, body):
        packets = []
        team.relay_packet({'id': 'test', 'method': 'POST', 'path': path,
            'body': base64.b64encode(json.dumps(body).encode()).decode()}, packets.append, self.state)
        return packets

    def test_reject_arbitrary_urls_and_other_models(self):
        for path, body in [('http://example.com', {}), ('/v1/responses?url=x', {}),
                           ('/v1/responses', {'model': 'other'})]:
            with patch('urllib.request.build_opener') as opener:
                packets = self.packet(path, body)
                opener.assert_not_called()
                self.assertEqual(packets[0]['status'], 502)
                self.assertEqual(packets[-1]['kind'], 'end')

    def test_relay_does_not_accept_sender(self):
        self.packet('/teamchat', {'action': 'send', 'sender': 'mac', 'message': 'hello'})
        self.assertEqual(team.chat(self.state, 'mac', 'inbox', {})[0]['sender'], 'agent')

    def test_model_stream_and_fixed_auth(self):
        class Response:
            status = 200
            headers = {'Content-Type': 'text/event-stream'}
            def __enter__(self):
                self.chunks = iter([b'data: one\n\n', b'data: two\n\n', b''])
                return self
            def __exit__(self, *args):
                pass
            def read1(self, size):
                return next(self.chunks)
        with patch('urllib.request.build_opener') as opener, patch('team.gateway_api_key', return_value='test-key'):
            opener.return_value.open.return_value = Response()
            packets = self.packet('/v1/responses', {'model': team.MODEL, 'reasoning': {'effort': 'low'}})
            req = opener.return_value.open.call_args.args[0]
            self.assertEqual(req.full_url, 'http://localhost:3425/v1/responses')
            self.assertEqual(req.get_header('Authorization'), 'Bearer test-key')
            self.assertEqual(json.loads(req.data)['reasoning']['effort'], 'high')
            self.assertEqual([x['kind'] for x in packets], ['headers', 'data', 'data', 'end'])


if __name__ == '__main__':
    unittest.main()
