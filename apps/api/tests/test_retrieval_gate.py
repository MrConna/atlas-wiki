from copy import deepcopy

from scripts.eval_retrieval import native_environment_gates


SHA = "a" * 40
DIGEST = "b" * 64
CORPUS = [
    {
        "title": "fixture",
        "content_sha256": "c" * 64,
        "chunk_count": 2,
        "current_chunk_count": 2,
    }
]


def valid_metadata():
    return {
        "backend": "native-pgvector",
        "app_git_sha": SHA,
        "dimensions": 768,
        "model_identity": f"ollama:model@{DIGEST}:dim768:embeddinggemma-retrieval-v1",
        "total_chunks": 2,
        "current_chunks": 2,
        "corpus": deepcopy(CORPUS),
    }


def test_native_environment_gate_accepts_exact_attestation():
    assert all(native_environment_gates(valid_metadata(), SHA, DIGEST, CORPUS).values())


def test_native_environment_gate_rejects_wrong_sha_model_corpus_and_stale_chunks():
    metadata = valid_metadata()
    assert not native_environment_gates(metadata, "0" * 40, DIGEST, CORPUS)["deployed_git_sha"]
    assert not native_environment_gates(metadata, SHA, "0" * 64, CORPUS)["embedding_model_digest"]

    altered_corpus = deepcopy(CORPUS)
    altered_corpus[0]["content_sha256"] = "0" * 64
    assert not native_environment_gates(metadata, SHA, DIGEST, altered_corpus)["exact_fixture_corpus"]

    metadata["current_chunks"] = 1
    assert not native_environment_gates(metadata, SHA, DIGEST, CORPUS)["all_chunks_current"]
