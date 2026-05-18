#!/bin/bash
# DataMapper AI — commit + push knowledge graph feature branch
# Branch: feature/kg-hybrid-rag  (DO NOT merge to dev until confirmed)
set -e
cd "$(dirname "$0")"

GITHUB_USER="shankarjun1997"
REPO="datamapper-ai"
# PAT read from env var — set it before running:
#   export GH_PAT=<your_token>  bash git-kg-push.sh
PAT="${GH_PAT:?GH_PAT env var is required}"
REMOTE="https://${GITHUB_USER}:${PAT}@github.com/${GITHUB_USER}/${REPO}.git"
BRANCH="feature/kg-hybrid-rag"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  DataMapper AI — Push $BRANCH"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

git remote set-url origin "$REMOTE" 2>/dev/null || git remote add origin "$REMOTE"
git checkout "$BRANCH" 2>/dev/null || git checkout -b "$BRANCH"

echo "→ Staging changes..."
git add server.py graph_engine.py graph_retriever.py requirements.txt git-kg-push.sh

git commit -m "feat(kg): Knowledge Graph + Hybrid Retrieval pipeline

Knowledge Graph Engine (graph_engine.py)
- In-process NetworkX DiGraph — no external DB required
- Nodes: column, table, domain, concept, mapping
- Edges: has_column, foreign_key, belongs_to, has_role, maps_to, semantic_sim
- Domain classifier: Subscriber, Billing, Network, Order, Location, Product, Reference
- Concept classifier: Identity, Temporal, Metric, Flag, Code, Descriptor, Revenue
- Heuristic FK inference from naming patterns (<entity>_id / _key / _num)
- Persisted as audits/knowledge_graph.json across sessions
- record_approved_mapping(): every Gate 2 approval strengthens maps_to edges

Hybrid Retriever (graph_retriever.py)
- retrieve_candidates(): 3-signal retrieval per source column
    1. Graph traversal: domain match, concept match, FK chain proximity
    2. Vector search: sentence-transformers all-MiniLM-L6-v2 embeddings
    3. Rule-based: rapidfuzz name score + type compatibility
- Composite score with configurable weights (full hybrid vs degraded fallback)
- llm_rerank(): LLM judges top-k pre-filtered candidates — not 400+ raw columns
- map_table_hybrid(): drop-in replacement for L3 batch loop per source table
- Graceful degradation: if sentence-transformers/numpy not installed, reverts to graph+rule

Pipeline Integration (server.py)
- DM_KG_ENABLED env var: true (default) enables KG, false falls back to batch LLM
- L1 completion: ingest_source_schema() populates src column/table/domain nodes
- L2 completion: ingest_target_schema() + build_vector_index() readies vector index
- L3: uses map_table_hybrid() when KG available, batch LLM otherwise
    - SSE emits 'Hybrid KG+RAG' vs 'Batch LLM' mode label
- approve_mapping(): record_approved_mapping() writes maps_to edge with weight
    - Repeated approvals across sessions accumulate edge weight
- All KG imports wrapped in try/except — server starts even if deps missing

New REST endpoints
- GET  /api/graph/stats         — node/edge counts by kind
- GET  /api/graph/domains       — domain list with tgt column counts
- GET  /api/graph/candidates    — test retrieval for any src_table.src_col
- GET  /api/graph/mappings      — past approved edges for a source column
- POST /api/graph/reset         — clear graph (dev use)

Requirements
- networkx>=3.3
- sentence-transformers>=3.0.0
- numpy>=1.26.0
- (torch CPU-only sufficient — transitive dep of sentence-transformers)"

echo "→ Pushing $BRANCH..."
git push -u origin "$BRANCH"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Done! Branch pushed (NOT merged to dev)"
echo ""
echo "  main                    → v1.0.0 stable"
echo "  dev                     → v1.1.0"
echo "  feature/v2-enhancements → Slack, audit, deploy"
echo "  feature/kg-hybrid-rag   → Knowledge Graph + Hybrid RAG (this)"
echo ""
echo "  Repo: https://github.com/${GITHUB_USER}/${REPO}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
