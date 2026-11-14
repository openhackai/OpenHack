"""
Notes API: the remediated counterpart of the IDOR fixture used by OpenHack's
regression-test generation tests. Do not deploy.

Users register, log in for a bearer token, and keep private notes. The
single-note routes only ever return or change notes the caller owns.
"""

import secrets

from flask import Flask, abort, jsonify, request


def create_app():
    app = Flask(__name__)
    users = {}  # username -> {"id": int, "password": str}
    tokens = {}  # token -> user id
    notes = {}  # note id -> {"id", "owner_id", "title", "body"}
    counters = {"user": 0, "note": 0}

    def current_user_id():
        header = request.headers.get("Authorization", "")
        token = header[len("Bearer "):] if header.startswith("Bearer ") else ""
        user_id = tokens.get(token)
        if user_id is None:
            abort(401)
        return user_id

    def owned_note(note_id, user_id):
        note = notes.get(note_id)
        # Same answer whether the note is missing or someone else's, so note
        # IDs cannot be probed.
        if note is None or note["owner_id"] != user_id:
            abort(404)
        return note

    @app.post("/api/register")
    def register():
        data = request.get_json(force=True) or {}
        username, password = data.get("username"), data.get("password")
        if not username or not password:
            abort(400)
        if username in users:
            abort(409)
        counters["user"] += 1
        users[username] = {"id": counters["user"], "password": password}
        return jsonify({"id": counters["user"], "username": username}), 201

    @app.post("/api/login")
    def login():
        data = request.get_json(force=True) or {}
        user = users.get(data.get("username"))
        if not user or user["password"] != data.get("password"):
            abort(401)
        token = secrets.token_urlsafe(24)
        tokens[token] = user["id"]
        return jsonify({"token": token})

    @app.post("/api/notes")
    def create_note():
        owner_id = current_user_id()
        data = request.get_json(force=True) or {}
        counters["note"] += 1
        note = {
            "id": counters["note"],
            "owner_id": owner_id,
            "title": data.get("title", ""),
            "body": data.get("body", ""),
        }
        notes[note["id"]] = note
        return jsonify(note), 201

    @app.get("/api/notes")
    def list_notes():
        owner_id = current_user_id()
        return jsonify([n for n in notes.values() if n["owner_id"] == owner_id])

    @app.get("/api/notes/<int:note_id>")
    def get_note(note_id):
        return jsonify(owned_note(note_id, current_user_id()))

    @app.put("/api/notes/<int:note_id>")
    def update_note(note_id):
        note = owned_note(note_id, current_user_id())
        data = request.get_json(force=True) or {}
        note.update({key: data[key] for key in ("title", "body") if key in data})
        return jsonify(note)

    @app.delete("/api/notes/<int:note_id>")
    def delete_note(note_id):
        note = owned_note(note_id, current_user_id())
        notes.pop(note["id"])
        return "", 204

    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=3000)
