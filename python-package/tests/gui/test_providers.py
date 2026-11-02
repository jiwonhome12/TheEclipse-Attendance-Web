from insightface.gui.core import face_engine


def test_cuda_choice_falls_back_when_provider_is_unavailable(monkeypatch):
    monkeypatch.setattr(face_engine, "available_execution_providers", lambda: ["CPUExecutionProvider"])

    assert face_engine.is_cuda_provider_available() is False
    assert face_engine.providers_from_choice("CUDA") == ["CPUExecutionProvider"]


def test_cuda_choice_is_enabled_when_provider_is_available(monkeypatch):
    monkeypatch.setattr(
        face_engine,
        "available_execution_providers",
        lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
    )

    assert face_engine.is_cuda_provider_available() is True
    assert face_engine.providers_from_choice("CUDA") == ["CUDAExecutionProvider", "CPUExecutionProvider"]
    assert face_engine.providers_from_choice("Auto") == ["CUDAExecutionProvider", "CPUExecutionProvider"]

