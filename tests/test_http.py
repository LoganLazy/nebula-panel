"""HTTP smoke test with isolated local state; no SSH or model calls."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import server as panel


class PanelHttpTest(unittest.TestCase):
    def test_session_state_and_authenticated_server_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            with patch.multiple(panel, DATA=base, STATE_FILE=base / 'state.json', JOBS={}):
                httpd = panel.ThreadingHTTPServer(('127.0.0.1', 0), panel.Handler)
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                origin = 'http://127.0.0.1:' + str(httpd.server_port)

                def request(path, body=None, token=None, method=None):
                    headers = {'Content-Type': 'application/json'}
                    if token:
                        headers['X-Nebula-Token'] = token
                    req = urllib.request.Request(origin + path,
                        data=json.dumps(body).encode() if body is not None else None,
                        headers=headers, method=method)
                    with urllib.request.urlopen(req, timeout=5) as response:
                        return json.load(response)

                try:
                    session = request('/api/session')
                    self.assertEqual(session['version'], panel.PANEL_VERSION)
                    self.assertEqual(request('/api/state'), {'servers': [], 'projects': []})
                    body = {'name': 'Example', 'host': 'example.test', 'user': 'deploy'}
                    with self.assertRaises(urllib.error.HTTPError) as error:
                        request('/api/servers', body)
                    self.assertEqual(error.exception.code, 403)
                    self.assertEqual(request('/api/state')['servers'], [])
                    item = request('/api/servers', body, session['token'])
                    self.assertEqual(request('/api/state')['servers'][0]['host'], 'example.test')
                    self.assertEqual((base / 'state.json').stat().st_mode & 0o777, 0o600)
                    request('/api/servers/' + item['id'], {}, session['token'], 'DELETE')
                    self.assertEqual(request('/api/state')['servers'], [])
                    with self.assertRaises(urllib.error.HTTPError) as missing:
                        request('/static/../server.py')
                    self.assertEqual(missing.exception.code, 404)
                finally:
                    httpd.shutdown()
                    httpd.server_close()
                    thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
