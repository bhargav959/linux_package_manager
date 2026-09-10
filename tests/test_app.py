from fastapi.testclient import TestClient

from app.main import app, AptManager, FlatpakManager, Package, PackageAction

client = TestClient(app)


def test_package_action_validation():
    action = PackageAction(manager="apt", package="linux-image-foo:amd64")
    assert action.manager == "apt"
    assert action.package == "linux-image-foo:amd64"


def test_manager_available_methods_exist():
    assert hasattr(AptManager(), "updates")
    assert hasattr(AptManager(), "search")
    assert hasattr(FlatpakManager(), "updates")
    assert hasattr(FlatpakManager(), "search")


def test_sudo_status_endpoint():
    response = client.get("/api/auth/sudo-status")
    assert response.status_code == 200
    data = response.json()
    assert "authenticated" in data
    assert "passwordless" in data


def test_search_aggregates_manager_sources(monkeypatch):
    class FakeManager:
        display_name = "Fake"
        name = "fake"

        def available(self):
            return True

        def installed(self):
            return []

        def updates(self):
            return []

        def search(self, query):
            return [Package("demo-pkg", "fake", latest_version="1.2.3", source="Demo Repository", description="Demo package", installed=False)]

        def details(self, package):
            return Package(package, "fake", latest_version="1.2.3", source="Demo Repository", description="Demo package", installed=False)

    import app.main as main

    old = main.MANAGERS
    monkeypatch.setattr(main, "MANAGERS", {"fake": FakeManager()})

    response = client.get("/api/search", params={"q": "demo"})
    assert response.status_code == 200
    data = response.json()
    assert data["count"] == 1
    assert data["results"][0]["source"] == "Demo Repository"
    assert data["results"][0]["manager"] == "fake"

    monkeypatch.setattr(main, "MANAGERS", old)


def test_search_rejects_unsafe_query():
    response = client.get("/api/search", params={"q": "demo; rm -rf /"})
    assert response.status_code == 400
    assert "unsupported characters" in response.json()["detail"].lower()
