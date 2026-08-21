from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_release_units_execute_from_release_venv() -> None:
    for name, module in (
        ("tradingbot-paper-release.conf", "bot.cli paper"),
        ("tradingbot-frontend-release.conf", "bot.web"),
    ):
        unit = (ROOT / "deploy" / "systemd" / name).read_text(encoding="utf-8")
        assert "WorkingDirectory=/home/diego/.local/share/tradingbot/current" in unit
        assert f"current/.venv/bin/python -m {module}" in unit
        assert "EnvironmentFile=-/home/diego/.local/share/tradingbot/current.env" in unit
        assert "Documents/tradingbot/.venv" not in unit


def test_deploy_requires_lock_and_rejects_mutable_releases() -> None:
    deploy = (ROOT / "scripts" / "deploy_release.sh").read_text(encoding="utf-8")

    assert 'uv lock --project "$ROOT_DIR" --check' in deploy
    assert "uv sync" in deploy and "--frozen" in deploy
    assert '--python "$ROOT_DIR/.venv/bin/python"' in deploy
    assert "--extra dev" in deploy
    assert "-p no:cacheprovider" in deploy
    assert 'find "$RELEASE_DIR" -perm /222' in deploy
    assert "verify_release_source" in deploy
    assert 'chmod -R a-w "$BUILD_DIR"' in deploy
    assert 'if [ "$BUILD_ONLY" = "true" ]' in deploy
    assert "systemctl is-active --quiet tradingbot-paper.service tradingbot-frontend.service" in deploy
    assert 'epoch >= int(os.environ["PAPER_RESTART_EPOCH"])' in deploy
