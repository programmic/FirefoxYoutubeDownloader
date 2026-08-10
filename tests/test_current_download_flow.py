import pathlib
import sys

from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app_nano


def test_enqueuing_single_url_works():
    with TestClient(app_nano.app) as client:
        response = client.post('/enqueue', json={'url': 'https://www.youtube.com/watch?v=demo'})
        assert response.status_code == 200
        payload = response.json()
        assert payload['status'] in {'queued', 'already_exists'}
        assert payload['url'] == 'https://www.youtube.com/watch?v=demo'


def test_command_unknown_command_is_rejected():
    with TestClient(app_nano.app) as client:
        response = client.post('/command', json={'cmd': 'not-a-command'})
        assert response.status_code == 200
        assert response.json()['status'] == 'unknown_command'
