from insightface.gui.core.model_downloads import GFPGAN_DOWNLOAD_URL, _content_range_total, fallback_model_assets, local_model_status
from insightface.gui.core.paths import default_workspace, workspace_paths


def test_default_gui_workspace_path():
    workspace = default_workspace()
    assert workspace.name == "gui"
    assert workspace.parent.name == ".insightface"
    assert workspace.is_absolute()


def test_workspace_paths_are_under_gui_workspace(tmp_path):
    workspace = (tmp_path / ".insightface" / "gui").resolve()
    paths = workspace_paths(workspace)
    assert paths["workspace"] == workspace
    for key in ("database", "crops", "exports", "reports", "logs", "cache"):
        assert paths[key].is_relative_to(paths["workspace"])


def test_fallback_model_assets_have_github_release_urls(tmp_path):
    assets = fallback_model_assets()
    names = {asset.name for asset in assets}
    assert {"buffalo_l.zip", "buffalo_s.zip", "antelopev2.zip", "GFPGANv1.4.onnx"}.issubset(names)
    for asset in assets:
        if asset.name == "GFPGANv1.4.onnx":
            assert asset.source == "third party"
            assert asset.kind == "third-party restore model"
            assert asset.browser_download_url == GFPGAN_DOWNLOAD_URL
            assert "harisreedhar/Face-Upscalers-ONNX/releases/download/Models" in asset.browser_download_url
        else:
            assert asset.tag_name == "v0.7"
            assert asset.browser_download_url.startswith(
                "https://github.com/deepinsight/insightface/releases/download/v0.7/"
            )
    assert local_model_status(assets[0], tmp_path) == "not installed"


def test_content_range_total_parser():
    assert _content_range_total("bytes 10-19/100") == 100
    assert _content_range_total("bytes 10-19/*") == 0
    assert _content_range_total(None) == 0
    assert _content_range_total("not-a-range") == 0
