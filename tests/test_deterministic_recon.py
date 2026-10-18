import os

import pytest
from tests.conftest import write_file, write_json

from openhack.tools.registry import ToolRegistry
from openhack.deterministic_recon import (
    _FEATURE_INDICATORS,
    _detect_features_fast,
    run_deterministic_recon,
)
from openhack.tools.filesystem import FileSystemTools


class TestFeatureDetection:
    def test_detects_file_upload(self, tmp_path):
        write_file(tmp_path, "upload.py", """
from flask import request

@app.route('/upload', methods=['POST'])
def upload():
    file = request.files['document']
    file.save('/uploads/' + file.filename)
""")
        write_json(tmp_path, "package.json", {"dependencies": {}})
        tools = ToolRegistry(target_dir=tmp_path)
        result = run_deterministic_recon(tools)
        features = result.get("features", {})
        assert "file_uploads" in features

    def test_detects_auth_system(self, tmp_path):
        write_file(tmp_path, "auth.py", """
import jwt
from flask import request

def verify_token(token):
    return jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
""")
        tools = ToolRegistry(target_dir=tmp_path)
        result = run_deterministic_recon(tools)
        features = result.get("features", {})
        assert "auth_system" in features

    def test_empty_project(self, tmp_path):
        write_file(tmp_path, "readme.md", "# empty project")
        tools = ToolRegistry(target_dir=tmp_path)
        result = run_deterministic_recon(tools)
        assert "summary" in result
        assert "attack_surface" in result


class TestReconOutput:
    def test_returns_expected_keys(self, tmp_path):
        write_json(tmp_path, "package.json", {"dependencies": {"express": "4.0.0"}})
        write_file(tmp_path, "index.js", "const express = require('express');\nconst app = express();\n")
        tools = ToolRegistry(target_dir=tmp_path)
        result = run_deterministic_recon(tools)
        assert "summary" in result
        assert "type" in result
        assert result["type"] == "recon_complete"
        assert "attack_surface" in result


class TestFeatureDetectionPaths:
    @pytest.mark.parametrize("file_count", [2, 5001], ids=["regex", "fixed-string"])
    def test_quoted_paths_preserve_all_features(self, tmp_path, file_count):
        write_file(tmp_path, "src/owner's uploads/don't_break.js", "multer();\n")
        write_file(tmp_path, "auth.js", "bcrypt();\n")
        for index in range(file_count - 2):
            write_file(tmp_path, f"src/empty_{index}.js", "")

        features = _detect_features_fast(FileSystemTools(tmp_path), _FEATURE_INDICATORS)

        assert features == {
            "file_uploads": ["File Uploads (1 files)"],
            "auth_system": ["Auth System (1 files)"],
        }

    @pytest.mark.skipif(os.name == "nt", reason="Requires POSIX filename characters")
    @pytest.mark.parametrize("filename", ['double"quote.js', "back\\slash.js", "new\nline.js", "tab\tname.js"])
    def test_special_filename_is_counted_once(self, tmp_path, filename):
        write_file(tmp_path, filename, "multer();\n")

        features = _detect_features_fast(FileSystemTools(tmp_path), _FEATURE_INDICATORS)

        assert features == {"file_uploads": ["File Uploads (1 files)"]}
