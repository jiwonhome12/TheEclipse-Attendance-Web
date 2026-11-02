from insightface.gui.__main__ import main
from insightface.gui.app import create_context
from insightface.gui.core.config import AppConfig, save_config


def test_cli_import_and_version(capsys):
    import insightface
    import insightface.gui

    assert insightface.__version__ == "1.0.1"
    assert insightface.gui.__version__ == "1.0.1"
    assert main(["--version"]) == 0
    out = capsys.readouterr().out
    assert "InsightFace Evaluation Studio 1.0.1" in out
    assert "insightface 1.0.1" in out


def test_insightface_cli_import_does_not_require_mxnet():
    from insightface.commands import insightface_cli

    assert callable(insightface_cli.main)


def test_safe_mode_is_runtime_only(tmp_path):
    workspace = tmp_path / "workspace"
    cfg = AppConfig(workspace_path=str(workspace))
    cfg.safe_mode = True
    cfg.auto_load_model = False
    save_config(cfg)

    args = type(
        "Args",
        (),
        {"workspace": str(workspace), "model": None, "provider": None, "safe_mode": True},
    )
    context = create_context(args())

    assert context.runtime_safe_mode is True
    assert context.config.safe_mode is False
    assert context.config.auto_load_model is True
