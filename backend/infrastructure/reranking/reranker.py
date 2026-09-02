"""
Reranking module for RAG systems using cross-encoders and semantic scoring.

Provides flexible re-ranking strategies for retrieved chunks:
- Semantic similarity re-ranking using cross-encoders
- Relevance threshold filtering
- Score normalization and combination
"""

from typing import List, Dict, Optional, Tuple
import logging
import re

logger = logging.getLogger(__name__)


class Reranker:
    """
    Base reranker class for post-retrieval chunk scoring and filtering.
    Can be extended with different reranking strategies.
    """

    def __init__(self, relevance_threshold: float = 0.3):
        """
        Initialize reranker.

        Args:
            relevance_threshold: Minimum similarity score to include a chunk (0.0-1.0)
        """
        self.relevance_threshold = relevance_threshold
        logger.info(f"Reranker initialized with threshold: {relevance_threshold}")

    def rerank(
        self,
        query: str,
        chunks: List[Dict],
        top_k: Optional[int] = None
    ) -> List[Dict]:
        """
        Rerank retrieved chunks based on query relevance.

        Args:
            query: The search query
            chunks: List of chunk dicts with 'text', 'score', 'metadata'
            top_k: Optional limit on returned chunks (if None, use relevance_threshold)

        Returns:
            Reranked and filtered chunks sorted by relevance
        """
        if not chunks:
            return []

        # Score each chunk
        scored_chunks = []
        for chunk in chunks:
            relevance_score = self._compute_relevance_score(query, chunk)
            scored_chunks.append({
                **chunk,
                "rerank_score": relevance_score
            })

        # Log all scores before filtering
        all_scores = [c["rerank_score"] for c in scored_chunks]
        logger.info(f"Reranker: All scores before filtering: {all_scores[:5]} (showing first 5)")

        # Filter by relevance threshold
        filtered_chunks = [
            c for c in scored_chunks
            if c["rerank_score"] >= self.relevance_threshold
        ]

        logger.info(f"Reranker: Threshold={self.relevance_threshold}, Filtered: {len(filtered_chunks)}/{len(chunks)}")

        # Sort by rerank score (descending)
        filtered_chunks.sort(key=lambda x: x["rerank_score"], reverse=True)

        # Apply top_k limit if specified
        if top_k:
            filtered_chunks = filtered_chunks[:top_k]

        logger.info(
            f"Reranked {len(chunks)} chunks → {len(filtered_chunks)} above threshold, "
            f"top scores: {[c['rerank_score'] for c in filtered_chunks[:3]]}"
        )

        return filtered_chunks

    def _compute_relevance_score(self, query: str, chunk: Dict) -> float:
        """
        Compute relevance score between query and chunk.
        Base implementation uses existing vector similarity score.
        Override in subclasses for different scoring strategies.

        Args:
            query: Search query
            chunk: Chunk dict with 'score' (vector similarity)

        Returns:
            Relevance score (0.0-1.0)
        """
        # Use existing vector similarity score as base
        vector_score = chunk.get("score", 0.0)
        if vector_score is None:
            vector_score = 0.0

        # Normalize to 0-1 if needed (some similarity metrics range differently)
        if vector_score > 1.0:
            vector_score = min(1.0, vector_score)

        return float(vector_score)


class CrossEncoderReranker(Reranker):
    """
    Reranker using cross-encoder models for more accurate relevance scoring.
    Uses sentence-transformers cross-encoder models.
    """

    def __init__(
        self,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-12-v2",
        relevance_threshold: float = 0.5,
        use_gpu: bool = False
    ):
        """
        Initialize cross-encoder reranker.

        Args:
            model_name: HuggingFace model ID for cross-encoder
            relevance_threshold: Minimum relevance score (0.0-1.0)
            use_gpu: Whether to use GPU if available
        """
        super().__init__(relevance_threshold)

        self.model_name = model_name
        self.use_gpu = use_gpu
        self.model = None

        try:
            from sentence_transformers import CrossEncoder
            device = "cuda" if use_gpu else "cpu"
            self.model = CrossEncoder(model_name, max_length=512, device=device)
            logger.info(f"CrossEncoderReranker initialized with {model_name} on {device}")
        except ImportError:
            logger.error(
                "sentence-transformers not installed. "
                "Install with: pip install sentence-transformers"
            )
            self.model = None

    def _compute_relevance_score(self, query: str, chunk: Dict) -> float:
        """
        Compute relevance using cross-encoder model.
        Falls back to vector score if model not available.

        Args:
            query: Search query
            chunk: Chunk with 'text' and 'score'

        Returns:
            Relevance score (0.0-1.0)
        """
        if self.model is None:
            # Fallback to vector similarity
            return super()._compute_relevance_score(query, chunk)

        chunk_text = chunk.get("text", "")
        if not chunk_text:
            return 0.0

        try:
            # Cross-encoder returns logits, normalize to 0-1 range
            score = self.model.predict([[query, chunk_text]])[0]
            # Convert logit to probability (sigmoid-like normalization)
            normalized_score = 1.0 / (1.0 + abs(score) ** -1)
            return float(normalized_score)
        except Exception as e:
            logger.warning(f"Cross-encoder scoring failed: {str(e)}, using vector score")
            return super()._compute_relevance_score(query, chunk)


class HybridReranker(Reranker):
    """
    Hybrid reranker combining vector similarity and cross-encoder scores.
    Weights both signals for balanced relevance assessment.
    """

    def __init__(
        self,
        cross_encoder_weight: float = 0.6,
        vector_weight: float = 0.4,
        model_name: str = "cross-encoder/ms-marco-MiniLM-L-12-v2",
        relevance_threshold: float = 0.4,
        use_gpu: bool = False
    ):
        """
        Initialize hybrid reranker.

        Args:
            cross_encoder_weight: Weight for cross-encoder score (0.0-1.0)
            vector_weight: Weight for vector similarity score (0.0-1.0)
            model_name: HuggingFace cross-encoder model ID
            relevance_threshold: Minimum combined score
            use_gpu: Whether to use GPU
        """
        super().__init__(relevance_threshold)

        # Normalize weights
        total = cross_encoder_weight + vector_weight
        self.cross_encoder_weight = cross_encoder_weight / total
        self.vector_weight = vector_weight / total

        self.cross_encoder = None
        try:
            from sentence_transformers import CrossEncoder
            device = "cuda" if use_gpu else "cpu"
            self.cross_encoder = CrossEncoder(model_name, max_length=512, device=device)
            logger.info(
                f"HybridReranker initialized: "
                f"CE={self.cross_encoder_weight:.2f}, VEC={self.vector_weight:.2f}"
            )
        except ImportError:
            logger.warning("sentence-transformers not installed, using vector-only scoring")

    def _compute_relevance_score(self, query: str, chunk: Dict) -> float:
        """
        Compute hybrid relevance score.

        Args:
            query: Search query
            chunk: Chunk with 'text' and 'score'

        Returns:
            Combined relevance score (0.0-1.0)
        """
        # Vector similarity component
        vector_score = chunk.get("score", 0.0) or 0.0
        if vector_score > 1.0:
            vector_score = min(1.0, vector_score)

        # Cross-encoder component (if available)
        cross_score = 0.0
        if self.cross_encoder is not None:
            chunk_text = chunk.get("text", "")
            if chunk_text:
                try:
                    score = self.cross_encoder.predict([[query, chunk_text]])[0]
                    cross_score = 1.0 / (1.0 + abs(score) ** -1)
                except Exception as e:
                    logger.warning(f"Cross-encoder failed: {str(e)}")
                    cross_score = vector_score  # Fall back to vector score
        else:
            cross_score = vector_score  # If no CE model, use vector for both

        # Combine scores
        combined_score = (
            self.cross_encoder_weight * cross_score +
            self.vector_weight * vector_score
        )

        return float(combined_score)


class QueryNormalizer:
    """Normalize and preprocess queries for consistent retrieval."""

    @staticmethod
    def normalize(query: str) -> str:
        """
        Normalize query by removing noise and standardizing format.

        Args:
            query: Raw query string

        Returns:
            Normalized query
        """
        # Remove extra whitespace
        query = " ".join(query.split())

        # Remove special characters but keep important punctuation
        query = re.sub(r'[^\w\s\?\!\.\-]', '', query)

        # Lowercase
        query = query.lower()

        return query

    @staticmethod
    def remove_stopwords(query: str) -> str:
        """
        Remove common stopwords from query.

        Args:
            query: Query string

        Returns:
            Query with stopwords removed
        """
        stopwords = {
            'a', 'an', 'and', 'are', 'as', 'at', 'be', 'by', 'for', 'from',
            'has', 'he', 'in', 'is', 'it', 'its', 'of', 'on', 'or', 'that',
            'the', 'to', 'was', 'will', 'with', 'what', 'where', 'when', 'why'
        }

        words = query.lower().split()
        filtered = [w for w in words if w not in stopwords]
        return " ".join(filtered)
