import numpy as np

from insightface.gui.core.storage import Storage
from insightface.gui.core.utils import encode_webp_thumbnail


def test_storage_people_samples_and_search(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)
    person_id = storage.add_person("Alice")
    emb = np.array([1.0, 0.0, 0.0], dtype=np.float32)
    sample_id = storage.add_face_sample(person_id, emb, source_image_path="a.jpg", det_score=0.9)
    people = storage.list_people()
    assert people[0]["name"] == "Alice"
    assert people[0]["sample_count"] == 1
    samples = storage.list_face_samples(person_id)
    assert samples[0]["id"] == sample_id
    assert np.allclose(samples[0]["embedding"], emb)
    results = storage.search_embeddings(np.array([1.0, 0.0, 0.0], dtype=np.float32), top_k=1, threshold=0.5)
    assert results[0].person_id == person_id
    assert results[0].status == "matched"


def test_album_directories_and_results_persist(tmp_path):
    db = tmp_path / "test.db"
    storage = Storage(db)
    album_dir = tmp_path / "album"
    album_dir.mkdir()
    image_path = album_dir / "a.jpg"
    image_path.write_bytes(b"placeholder")
    photo_thumb = b"photo-webp"
    face_thumb = b"face-webp"

    storage.save_album_directories([str(album_dir)])
    assert storage.list_album_directories() == [str(album_dir)]

    media_id = storage.add_media_item(str(image_path), "image", thumbnail=photo_thumb)
    face_id = storage.add_media_face(
        media_id,
        np.array([1.0, 0.0], dtype=np.float32),
        thumbnail=face_thumb,
    )
    cluster = {
        "id": 1,
        "label": 0,
        "name": "Album Person 1",
        "source": "album",
        "face_count": 1,
        "photo_count": 1,
        "avg_quality": 0.0,
        "thumbnail_face_id": face_id,
        "thumbnail_path": "",
        "photos": [str(image_path)],
    }
    storage.save_album_results(
        [cluster],
        {1: [{"id": face_id, "media_path": str(image_path)}]},
        "DBSCAN",
        cluster_threshold=0.28,
        min_samples=2,
        min_face_size=80,
    )

    results = storage.load_album_results()
    assert results["algorithm"] == "DBSCAN"
    assert results["cluster_threshold"] == 0.28
    assert "duplicate_threshold" not in results
    assert results["min_face_size"] == 80
    assert results["clusters"][0]["thumbnail_face_id"] == face_id
    assert results["clusters"][0]["face_ids"] == [face_id]
    face = storage.list_media_faces()[0]
    assert face["cluster_id"] == 1
    assert face["thumbnail"] == face_thumb
    assert face["thumbnail_mime"] == "image/webp"
    assert face["media_thumbnail"] == photo_thumb
    assert face["media_thumbnail_mime"] == "image/webp"

    storage.clear_album_results()
    assert storage.load_album_results() == {}
    assert storage.list_media_faces()[0]["cluster_id"] is None


def test_webp_thumbnail_encoder_outputs_small_webp_bytes():
    image = np.zeros((80, 160, 3), dtype=np.uint8)
    image[:, :, 1] = 200

    payload = encode_webp_thumbnail(image, max_side=120, quality=35)

    assert payload is not None
    assert payload[:4] == b"RIFF"
    assert b"WEBP" in payload[:16]
