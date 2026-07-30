"""Path handling shared by /scan and /cd."""

import asyncio

from openhack.tui import OpenHackApp, _resolve_path_argument


def test_resolve_path_argument_strips_at_before_resolving_parent_path(
    tmp_path,
    monkeypatch,
):
    scanner = tmp_path / "openhack-scanner"
    target = tmp_path / "samples" / "vaultwise"
    scanner.mkdir()
    target.mkdir(parents=True)
    monkeypatch.chdir(scanner)

    assert _resolve_path_argument("@../samples/vaultwise") == target


def test_scan_accepts_at_prefixed_relative_directory(tmp_path, monkeypatch):
    scanner = tmp_path / "openhack-scanner"
    target = tmp_path / "samples" / "vaultwise"
    scanner.mkdir()
    target.mkdir(parents=True)
    monkeypatch.chdir(scanner)

    app = OpenHackApp.__new__(OpenHackApp)
    app._logout_armed = False
    app._verify_arm_subject = None
    started = []
    app._start_scan = started.append

    asyncio.run(app._handle_input("/scan @../samples/vaultwise"))

    assert started == [str(target)]


def test_scan_rejects_a_file_target(tmp_path):
    target = tmp_path / "not-a-directory"
    target.write_text("x")

    app = OpenHackApp.__new__(OpenHackApp)
    app._logout_armed = False
    app._verify_arm_subject = None
    app._start_scan = lambda _: None

    asyncio.run(app._handle_input(f"/scan @{target}"))

    assert app.last_status_line == f"error: not a directory: {target}"
