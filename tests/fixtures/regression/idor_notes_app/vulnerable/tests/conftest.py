import pytest

from app import create_app


@pytest.fixture
def client():
    return create_app().test_client()


@pytest.fixture
def auth_headers(client):
    """Register and log in a user; returns that user's Authorization headers."""

    def login(username, password="correct-horse-battery"):
        client.post("/api/register", json={"username": username, "password": password})
        response = client.post("/api/login", json={"username": username, "password": password})
        return {"Authorization": f"Bearer {response.get_json()['token']}"}

    return login
