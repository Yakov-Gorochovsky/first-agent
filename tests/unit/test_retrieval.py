"""
tests/unit/test_retrieval.py - FP-14: Hybrid Retrieval, Vector Store & Cross-Tenant Isolation Tests.

Tests:
1. Vector math: L2 normalization, packing/unpacking binary BLOBs, dot-product cosine similarity.
2. Sparse Okapi BM25 ranking and term frequency accounting.
3. Reciprocal Rank Fusion (RRF constant 60) fusing dense and sparse rank lists.
4. Multi-Tenant Isolation: Zero cross-tenant data leaks across probe queries.
5. Graceful BM25 Fallback when dense embedding generation drops.
6. Cascading Tenant Purge: complete erasure across vectors, memories, traces, and indexes.
"""

from pathlib import Path

import pytest

from core.retrieval import (
    BM25Partition,
    HybridRetrievalStore,
    MockDeterministicEmbeddingProvider,
    dot_product,
    normalize_vector,
    pack_vector,
    reciprocal_rank_fusion,
    unpack_vector,
)
from core.storage import SQLiteStorage


@pytest.mark.unit
def test_vector_math_and_packing() -> None:
    """Vectors are normalized to unit length and packed/unpacked with strict dimension validation."""
    raw = [3.0, 4.0, 0.0]
    norm = normalize_vector(raw)
    assert round(norm[0], 2) == 0.6
    assert round(norm[1], 2) == 0.8
    assert round(norm[2], 2) == 0.0

    # Dot product of identical unit vectors is 1.0
    assert round(dot_product(norm, norm), 2) == 1.0

    # Binary pack and unpack
    blob = pack_vector(norm)
    assert len(blob) == 12  # 3 floats * 4 bytes
    unpacked = unpack_vector(blob, expected_dim=3)
    assert pytest.approx(norm) == unpacked

    # Size mismatch raises ValueError
    with pytest.raises(ValueError):
        unpack_vector(blob, expected_dim=4)


@pytest.mark.unit
def test_bm25_scoring_and_ranking() -> None:
    """Okapi BM25 accurately scores documents matching query tokens."""
    bm25 = BM25Partition()
    bm25.index_document("doc1", "The quick brown fox jumps over the lazy dog")
    bm25.index_document("doc2", "Quantum computing and artificial intelligence breakthroughs")
    bm25.index_document("doc3", "Autonomous agent swarms and multi-agent systems")

    # Query for quantum
    scores_quantum = bm25.score("quantum computing")
    assert len(scores_quantum) >= 1
    assert scores_quantum[0][0] == "doc2"

    # Query for swarms
    scores_swarms = bm25.score("autonomous multi-agent")
    assert len(scores_swarms) >= 1
    assert scores_swarms[0][0] == "doc3"


@pytest.mark.unit
def test_reciprocal_rank_fusion() -> None:
    """RRF (k=60) appropriately fuses multiple rankings."""
    # List 1: docA (rank 1), docB (rank 2), docC (rank 3)
    # List 2: docB (rank 1), docA (rank 2), docD (rank 3)
    list1 = ["docA", "docB", "docC"]
    list2 = ["docB", "docA", "docD"]

    fused = reciprocal_rank_fusion([list1, list2], k=60)
    top_doc, top_score = fused[0]
    # docA score: 1/61 + 1/62 = 0.01639 + 0.01613 = 0.03252
    # docB score: 1/62 + 1/61 = 0.03252 (tied)
    assert top_doc in {"docA", "docB"}
    assert len(fused) == 4


@pytest.mark.unit
def test_hybrid_retrieval_and_cross_tenant_isolation(tmp_path: Path) -> None:
    """Hybrid retrieval returns relevant documents and strictly isolates tenant partitions."""
    db_file = tmp_path / "test_retrieval.db"
    storage = SQLiteStorage(db_path=str(db_file))
    embedder = MockDeterministicEmbeddingProvider(dimension=16)
    store = HybridRetrievalStore(storage=storage, embedding_provider=embedder)

    # Insert documents into tenant_alpha
    store.add_document(
        tenant_id="tenant_alpha",
        item_id="doc_alpha_1",
        content="Confidential financial merger between Alpha Corp and Gamma LLC.",
        metadata={"category": "finance"},
    )
    store.add_document(
        tenant_id="tenant_alpha",
        item_id="doc_alpha_2",
        content="Q3 Earnings report with 25% revenue growth and expansion plans.",
        metadata={"category": "earnings"},
    )

    # Insert document into tenant_beta
    store.add_document(
        tenant_id="tenant_beta",
        item_id="doc_beta_1",
        content="Project Orion stealth cybersecurity protocol specification.",
        metadata={"category": "security"},
    )

    # Search in tenant_alpha
    results_alpha = store.search(tenant_id="tenant_alpha", query="financial merger growth", limit=5)
    assert len(results_alpha) >= 1
    assert results_alpha[0].item_id in {"doc_alpha_1", "doc_alpha_2"}
    assert results_alpha[0].tenant_id == "tenant_alpha"

    # Cross-tenant leak probe: tenant_beta queries for "merger" or "Alpha Corp"
    results_beta_probe = store.search(tenant_id="tenant_beta", query="Alpha Corp merger", limit=5)
    # MUST NEVER LEAK ANY TENANT_ALPHA DOCUMENTS
    assert not any(r.item_id in {"doc_alpha_1", "doc_alpha_2"} for r in results_beta_probe)
    for r in results_beta_probe:
        assert r.tenant_id == "tenant_beta"

    # Probe on non-existent tenant returns 0 results
    empty_results = store.search(tenant_id="tenant_gamma", query="financial merger growth", limit=5)
    assert len(empty_results) == 0


@pytest.mark.unit
def test_dense_failure_fallback_to_bm25(tmp_path: Path) -> None:
    """When dense vector calculation fails, the store falls back to pure BM25 search."""
    db_file = tmp_path / "test_fallback.db"
    storage = SQLiteStorage(db_path=str(db_file))

    class FailingEmbedder(MockDeterministicEmbeddingProvider):
        def embed_text(self, text: str):
            raise RuntimeError("Embedder service unreachable")

    # Add doc with normal embedder
    normal_embedder = MockDeterministicEmbeddingProvider(dimension=16)
    store = HybridRetrievalStore(storage=storage, embedding_provider=normal_embedder)
    store.add_document("tenant_fallback", "doc1", "Distributed consensus algorithms and Paxos")

    # Swap in failing embedder for querying
    store.embedder = FailingEmbedder(dimension=16)
    results = store.search("tenant_fallback", query="Paxos consensus", limit=5)

    assert len(results) == 1
    assert results[0].item_id == "doc1"
    assert results[0].retrieval_method == "bm25_fallback"


@pytest.mark.unit
def test_cascading_tenant_purge(tmp_path: Path) -> None:
    """Purging tenant data completely erases vectors, memories, and index state."""
    db_file = tmp_path / "test_purge.db"
    storage = SQLiteStorage(db_path=str(db_file))
    store = HybridRetrievalStore(storage=storage)

    store.add_document("tenant_to_delete", "doc_del_1", "Data to be purged")
    storage.save_tenant_memory("tenant_to_delete", "secret", "value123")

    deleted = store.purge_tenant_data("tenant_to_delete")
    assert deleted >= 1

    # Search returns 0
    results = store.search("tenant_to_delete", query="Data", limit=5)
    assert len(results) == 0

    # Memory returns empty
    mems = storage.get_tenant_memories("tenant_to_delete")
    assert len(mems) == 0
