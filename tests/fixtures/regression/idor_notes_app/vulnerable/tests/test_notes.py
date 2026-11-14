def test_create_and_list_own_notes(client, auth_headers):
    headers = auth_headers("alice")
    created = client.post("/api/notes", json={"title": "groceries", "body": "milk"}, headers=headers)
    assert created.status_code == 201

    listed = client.get("/api/notes", headers=headers)
    assert [note["title"] for note in listed.get_json()] == ["groceries"]


def test_notes_require_authentication(client):
    assert client.get("/api/notes").status_code == 401
    assert client.post("/api/notes", json={"title": "x"}).status_code == 401
