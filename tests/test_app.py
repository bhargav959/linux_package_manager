from fastapi.testclient import TestClient
from app.main import app, AptManager, FlatpakManager, PackageAction, set_sudo_password

client = TestClient(app)


def test_package_action_validation():
    action = PackageAction(manager="apt", package="linux-image-foo:amd64", sudo_password="secretpassword")
    assert action.manager == "apt"
    assert action.sudo_password == "secretpassword"


def test_manager_available_methods_exist():
    assert hasattr(AptManager(), "updates")
    assert hasattr(FlatpakManager(), "updates")


def test_sudo_status_endpoint():
    r = client.get("/api/auth/sudo-status")
    assert r.status_code == 200
    data = r.json()
    assert "authenticated" in data
    assert "passwordless" in data


def test_sudo_verify_invalid_password():
    r = client.post("/api/auth/sudo-verify", json={"password": "definitely_wrong_password_123456"})
    assert r.status_code == 401
    assert "Incorrect sudo password" in r.json()["detail"]


def test_sudo_clear_endpoint():
    set_sudo_password("test_pass")
    r = client.post("/api/auth/sudo-clear")
    assert r.status_code == 200
    assert r.json()["success"] is True
