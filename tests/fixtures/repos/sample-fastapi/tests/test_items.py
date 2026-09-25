from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_list_items():
    resp = client.get("/items")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


def test_get_missing_item():
    assert client.get("/items/999").status_code == 404
