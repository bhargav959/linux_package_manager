from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)

def test_home():
    r = client.get('/')
    assert r.status_code == 200
    assert 'Linux Package Manager' in r.text

def test_system():
    r = client.get('/api/system')
    assert r.status_code == 200
    assert 'managers' in r.json()
