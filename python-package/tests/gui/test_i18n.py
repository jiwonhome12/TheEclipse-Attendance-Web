import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from insightface.gui.core.i18n import (
    LANGUAGE_OPTIONS,
    effective_language,
    normalize_language,
    tr,
)


def test_supported_languages_match_homepage_language_set():
    values = {option.value for option in LANGUAGE_OPTIONS}

    assert {
        "system",
        "en",
        "zh",
        "ja",
        "ko",
        "es",
        "fr",
        "de",
        "pt",
        "ru",
    }.issubset(values)


def test_language_normalization_and_fallback():
    assert normalize_language("zh_CN") == "zh"
    assert normalize_language("pt-BR") == "pt"
    assert normalize_language("auto") == "system"
    assert normalize_language("xx") == "en"
    assert effective_language("en") == "en"


def test_core_business_translations_have_professional_terms():
    assert tr("Settings", "zh") == "设置"
    assert tr("Enterprise Evaluation", "ja") == "エンタープライズ評価"
    assert tr("Contact Enterprise Support", "ko") == "기업 지원 문의"
    assert tr("Commercial production", "de") == "Kommerzieller Produktivbetrieb"
    assert tr("requires commercial model license", "fr") == "requiert une licence commerciale du modèle"
    assert tr("Face Recognition", "es") == "Reconocimiento facial"
    assert tr("Run Evaluation", "pt") == "Executar avaliação"
    assert tr("License Center", "ru") == "Центр лицензий"


def test_apply_translations_updates_basic_widgets():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QVBoxLayout, QWidget

    from insightface.gui.app import configure_qt_plugin_paths
    from insightface.gui.core.i18n import apply_translations

    configure_qt_plugin_paths()
    QApplication.instance() or QApplication([])
    widget = QWidget()
    layout = QVBoxLayout(widget)
    label = QLabel("Settings")
    button = QPushButton("Run Evaluation")
    button.setToolTip("Choose how evaluation handles images where more than one face is detected.")
    layout.addWidget(label)
    layout.addWidget(button)

    apply_translations(widget, "zh")

    assert label.text() == "设置"
    assert button.text() == "运行评测"
    assert button.toolTip() == "选择评测中检测到多张人脸时的处理方式。"

    apply_translations(widget, "en")

    assert label.text() == "Settings"
    assert button.text() == "Run Evaluation"
    assert button.toolTip() == "Choose how evaluation handles images where more than one face is detected."


def test_apply_translations_localizes_generic_button_tooltip():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QPushButton

    from insightface.gui.app import configure_qt_plugin_paths
    from insightface.gui.core.i18n import apply_translations

    configure_qt_plugin_paths()
    QApplication.instance() or QApplication([])
    button = QPushButton("Run Face Swap")
    button.setToolTip("Run face swap with the configured local swap model.")

    apply_translations(button, "zh")

    assert button.text() == "运行换脸"
    assert button.toolTip() == "点击执行：运行换脸。"

    apply_translations(button, "en")

    assert button.text() == "Run Face Swap"
    assert button.toolTip() == "Run face swap with the configured local swap model."


def test_icon_only_button_keeps_symbol_when_tooltip_is_localized():
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication, QPushButton

    from insightface.gui.app import configure_qt_plugin_paths
    from insightface.gui.core.i18n import apply_translations

    configure_qt_plugin_paths()
    QApplication.instance() or QApplication([])
    button = QPushButton("×")
    button.setToolTip("Remove the current file.")

    apply_translations(button, "zh")

    assert button.text() == "×"
    assert button.toolTip() == "点击执行：移除。"


def test_dataset_rules_dialog_has_localized_help_summary():
    from insightface.gui.pages.enterprise_eval_page import dataset_rules_text

    text = dataset_rules_text("zh")

    assert text.startswith("评测数据集规则")
    assert "支持从身份文件夹进行本地 1:1 验证和 1:N 识别评测" in text
    assert "dataset_1v1/" in text
    assert "dataset_1n/" in text
    assert "gallery/" in text
    assert "probe/" in text
    assert "多人脸处理" in text
    assert "报告输出" in text
    assert "Enterprise Evaluation Dataset Rules" not in text
    assert "Each subfolder is one identity" not in text


def test_album_and_recognition_page_copy_is_localized():
    assert (
        tr(
            "Import or refresh local album folders, cluster detected faces, and review the photos in each person group.",
            "zh",
        )
        == "导入或刷新本地相册文件夹，对检测到的人脸进行聚类，并查看每个人物组中的照片。"
    )
    assert "所有相册处理均在本地完成" in tr(
        "All album processing is local. Import / Refresh scans every selected folder, extracts features only for new images, then runs DBSCAN clustering over all indexed faces using the selected cosine threshold.",
        "zh",
    )
    assert "上传一张查询图片" in tr(
        "Upload one query image and a gallery image, image set, or folder. One gallery image runs 1:1 compare; multiple gallery images run 1:N gallery search.",
        "zh",
    )
    assert "源图 + 目标 = 结果" in tr(
        "Source + Target = Result. Target can be an image or a video; the workflow chooses image or video swap automatically.",
        "zh",
    )
    assert "商业授权" in tr(
        "Face swap may require separate commercial authorization depending on usage and model license. Use only with appropriate rights and consent.",
        "zh",
    )
    assert "采购决策" in tr(
        "Run local 1:1 verification or 1:N identification evaluation from identity folders and export procurement-ready reports.",
        "zh",
    )
    assert "运行后端" in tr(
        "Configure model packs, execution provider, face swap models, and runtime checks.",
        "zh",
    )
    assert "手动刷新 GitHub Release" in tr(
        "Manually refresh GitHub release model URLs and download selected model packages locally.",
        "zh",
    )
    assert "商业部署" in tr(
        "Code and model files may have different licenses. Commercial deployment requires appropriate model authorization.",
        "zh",
    )
    assert tr("Refresh source", "zh") == "刷新来源"
    assert tr("Local model root", "zh") == "本地模型根目录"
