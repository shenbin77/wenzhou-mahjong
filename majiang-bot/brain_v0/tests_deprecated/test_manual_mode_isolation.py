"""Manual experiments must not require capture or pollute real-game records."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import main


def test_manual_mode_never_constructs_recognizer(monkeypatch):
    monkeypatch.setenv('MVP_DASHBOARD_ONLY', '1')
    monkeypatch.delenv('MVP_AFA_ENABLED', raising=False)
    def forbidden():
        raise AssertionError('manual mode attempted visual initialization')
    monkeypatch.setattr(main, 'TileRecognizer', forbidden)
    app = main.MahjongTrackerApp()
    assert app.dashboard_only
    assert app.recognizer is None


def test_manual_shutdown_never_persists(monkeypatch):
    monkeypatch.setenv('MVP_DASHBOARD_ONLY', '1')
    monkeypatch.delenv('MVP_AFA_ENABLED', raising=False)
    app = main.MahjongTrackerApp()
    def forbidden():
        raise AssertionError('manual experiment written to real database')
    monkeypatch.setattr(app.tracker, 'persist', forbidden)
    monkeypatch.setattr(main, 'stop_dashboard', lambda: None)
    calls = []
    monkeypatch.setattr(app.tracker, 'persist', lambda: calls.append(True))
    app._shutdown()
    assert calls == []
