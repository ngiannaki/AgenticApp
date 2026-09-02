from llama_index.core import VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core.storage import StorageContext
from llama_index.llms.ollama import Ollama
from llama_index.embeddings.ollama import OllamaEmbedding
import chromadb
import logging
from pathlib import Path
from backend.infrastructure.reranking import Reranker, QueryNormalizer
from backend.core.config import settings

logger = logging.getLogger(__name__)
# Ensure logger has handlers configured
if not logger.handlers:
    log_dir = Path(__file__).parent.parent.parent.parent / "logs"
    log_dir.mkdir(exist_ok=True)
    handler = logging.FileHandler(log_dir / "retrieval_server.log")
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# Configuration
VECTOR_STORE_DIR = "data/vector_store"
OLLAMA_BASE_URL = settings.base_url
OLLAMA_CHAT_MODEL = settings.chat_model
OLLAMA_EMBEDDING_MODEL = settings.embedding_model


class DocumentRetriever:
    """RAG-based document retriever using Ollama and ChromaDB with reranking."""

    def __init__(
        self,
        persist_dir: str = VECTOR_STORE_DIR,
        ollama_base_url: str = OLLAMA_BASE_URL,
        use_reranker: bool = True,
        relevance_threshold: float = 0.1
    ):
        self.persist_dir = Path(persist_dir)
        self.ollama_base_url = ollama_base_url

        # Initialize ChromaDB persistent client
        self.chroma_client = chromadb.PersistentClient(
            path=str(self.persist_dir)
        )

        # Initialize Ollama LLM
        self.llm = Ollama(
            model=OLLAMA_CHAT_MODEL,
            base_url=ollama_base_url,
            temperature=0.7,
        )

        # Initialize Ollama embeddings
        self.embed_model = OllamaEmbedding(
            model_name=OLLAMA_EMBEDDING_MODEL,
            base_url=ollama_base_url,
        )

        # Initialize reranker for post-retrieval scoring
        self.reranker = Reranker(relevance_threshold) if use_reranker else None

        logger.info(f"DocumentRetriever initialized with ChromaDB at {self.persist_dir}")
        if self.reranker:
            logger.info(f"Reranker enabled with threshold: {relevance_threshold}")
    
    def query(self, document_id: str, query: str, top_k: int = 5) -> dict:
        """
        Query a document and get an answer using RAG with reranking.

        Args:
            document_id: UUID of the document
            query: Question to ask
            top_k: Number of chunks to retrieve

        Returns:
            Dictionary with answer and source chunks
        """
        try:
            # Normalize query for consistent retrieval
            normalized_query = QueryNormalizer.normalize(query)
            logger.info(f"[QUERY] Starting query for document {document_id}")
            logger.info(f"[QUERY] Original query: {query}")
            logger.info(f"[QUERY] Normalized query: {normalized_query}")

            # Get the collection for this document
            collection_name = f"doc_{document_id}"

            try:
                collection = self.chroma_client.get_collection(name=collection_name)
                logger.info(f"[QUERY] Collection found: {collection_name}")
            except ValueError:
                logger.error(f"[QUERY] Collection not found: {collection_name}")
                return {
                    "success": False,
                    "document_id": document_id,
                    "error": f"Document not found in vector store: {document_id}"
                }

            # Create vector store from collection
            chroma_vector_store = ChromaVectorStore(chroma_collection=collection)

            # Create index
            index = VectorStoreIndex.from_vector_store(
                chroma_vector_store,
                embed_model=self.embed_model
            )

            # Retrieve more chunks than needed for reranking (1.5x to 2x multiplier)
            retrieval_k = max(top_k * 2, 10)
            logger.info(f"[QUERY] Retrieving {retrieval_k} chunks for reranking")

            query_engine = index.as_query_engine(
                llm=self.llm,
                similarity_top_k=retrieval_k,
                response_mode="compact"
            )

            # Execute query
            logger.info(f"[QUERY] Executing query with LLM")
            response = query_engine.query(normalized_query)

            # Extract source nodes for citations
            source_chunks = []
            if hasattr(response, 'source_nodes'):
                logger.info(f"[QUERY] Extracted {len(response.source_nodes)} source nodes")
                for node in response.source_nodes:
                    chunk_score = node.score if hasattr(node, 'score') else 0.0
                    if chunk_score is None:
                        chunk_score = 0.0
                    source_chunks.append({
                        "text": node.get_content(),
                        "score": chunk_score,
                        "metadata": node.metadata if hasattr(node, 'metadata') else {}
                    })

            # Apply reranking if enabled
            if self.reranker and source_chunks:
                logger.info(f"[QUERY] Reranking {len(source_chunks)} chunks")
                source_chunks = self.reranker.rerank(normalized_query, source_chunks, top_k=top_k)
                logger.info(f"[QUERY] After reranking: {len(source_chunks)} chunks")

            # Enrich with parent chunks for context
            source_chunks = self._enrich_chunks_with_parents(source_chunks, collection)

            logger.info(f"[QUERY] Query complete - {len(source_chunks)} chunks used")

            return {
                "success": True,
                "document_id": document_id,
                "query": query,
                "answer": str(response),
                "source_chunks": source_chunks,
                "chunks_count": len(source_chunks)
            }

        except Exception as e:
            logger.error(f"Query failed for document {document_id}: {str(e)}")
            return {
                "success": False,
                "document_id": document_id,
                "query": query,
                "error": str(e)
            }
    
    def _enrich_chunks_with_parents(
        self,
        chunks: list,
        collection
    ) -> list:
        """
        Enrich retrieved chunks by including their parent chunks for context.

        Args:
            chunks: List of retrieved chunks
            collection: ChromaDB collection

        Returns:
            Enhanced chunks with parent context where available
        """
        if not chunks or not collection:
            return chunks

        enriched_chunks = []
        seen_ids = set()

        # Add original chunks
        for chunk in chunks:
            chunk_id = chunk.get("metadata", {}).get("chunk_id")
            if chunk_id:
                seen_ids.add(chunk_id)
            enriched_chunks.append(chunk)

        # Try to fetch parent chunks
        parent_chunk_ids = set()
        for chunk in chunks:
            parent_id = chunk.get("metadata", {}).get("parent_chunk_id")
            if parent_id and parent_id not in seen_ids:
                parent_chunk_ids.add(parent_id)

        if parent_chunk_ids:
            try:
                logger.info(f"[PARENT_RETRIEVAL] Fetching {len(parent_chunk_ids)} parent chunks")
                parent_results = collection.get(
                    ids=list(parent_chunk_ids),
                    include=["documents", "metadatas"]
                )

                if parent_results and parent_results.get("documents"):
                    for doc_text, metadata in zip(parent_results["documents"], parent_results["metadatas"]):
                        enriched_chunks.append({
                            "text": doc_text,
                            "score": None,
                            "metadata": metadata or {},
                            "is_parent": True
                        })
                        logger.info(f"[PARENT_RETRIEVAL] Added parent chunk {metadata.get('chunk_id')}")
            except Exception as e:
                logger.warning(f"[PARENT_RETRIEVAL] Failed to fetch parent chunks: {str(e)}")

        return enriched_chunks

    def get_document_metadata(self, document_id: str) -> dict:
        """
        Get enhanced metadata for a document from vector store collection metadata.
        
        Args:
            document_id: UUID of the document
        
        Returns:
            Dictionary with document metadata
        """
        try:
            # Get the collection
            collection_name = f"doc_{document_id}"
            
            try:
                collection = self.chroma_client.get_collection(name=collection_name)
            except ValueError:
                return {
                    "success": False,
                    "document_id": document_id,
                    "error": f"Document not found in vector store: {document_id}"
                }
            
            # Get collection metadata
            collection_metadata = collection.metadata or {}
            
            # Get sample documents to extract statistics
            result = collection.get(limit=100)
            
            metadata_info = {
                "success": True,
                "document_id": document_id,
                "collection_metadata": collection_metadata,
            }
            
            if result:
                # Extract statistics from chunks
                all_metadata = result.get('metadatas', [])
                
                # Get document name from first chunk
                if all_metadata:
                    metadata_info["document_name"] = all_metadata[0].get("document_name", "Untitled")
                
                # Count total chunks
                metadata_info["total_chunks"] = len(all_metadata)
                
                # Extract unique sections if present
                sections = set()
                for meta in all_metadata:
                    if "section" in meta:
                        sections.add(meta["section"])
                
                if sections:
                    metadata_info["sections"] = list(sections)
                
                # Extract hierarchy levels
                hierarchy_levels = {}
                for meta in all_metadata:
                    level = meta.get("hierarchy_level", 0)
                    hierarchy_levels[level] = hierarchy_levels.get(level, 0) + 1
                
                if hierarchy_levels:
                    metadata_info["hierarchy_distribution"] = hierarchy_levels
                
                # Check for enhanced metadata fields
                first_meta = all_metadata[0]
                if "page_number" in first_meta:
                    page_numbers = set()
                    for meta in all_metadata:
                        page_numbers.add(meta.get("page_number"))
                    metadata_info["total_pages"] = len(page_numbers)
            
            logger.info(f"Retrieved metadata for document {document_id}")
            return metadata_info
        
        except Exception as e:
            logger.error(f"Failed to get metadata for document {document_id}: {str(e)}")
            return {
                "success": False,
                "document_id": document_id,
                "error": str(e)
            }


    def retrieve(
        self,
        document_id: str,
        query: str,
        top_k: int = 5,
    ) -> dict:
        """
        Retrieve relevant chunks from a document with reranking.

        Unlike query(), this method does NOT generate an answer.
        It only performs vector retrieval and returns the source chunks.
        """

        try:
            # Normalize query for consistent retrieval
            normalized_query = QueryNormalizer.normalize(query)
            logger.info(f"[RETRIEVE] Starting retrieval for document {document_id}")
            logger.info(f"[RETRIEVE] Normalized query: {normalized_query}")

            collection_name = f"doc_{document_id}"

            try:
                collection = self.chroma_client.get_collection(
                    name=collection_name
                )
            except ValueError:
                return {
                    "success": False,
                    "document_id": document_id,
                    "query": query,
                    "error": (
                        f"Document not found in vector store: "
                        f"{document_id}"
                    ),
                }

            chroma_vector_store = ChromaVectorStore(
                chroma_collection=collection
            )

            index = VectorStoreIndex.from_vector_store(
                chroma_vector_store,
                embed_model=self.embed_model,
            )

            # Retrieve more chunks for reranking
            retrieval_k = max(top_k * 2, 10)
            logger.info(f"[RETRIEVE] Retrieving {retrieval_k} chunks for reranking")

            retriever = index.as_retriever(
                similarity_top_k=retrieval_k,
            )

            logger.info(f"[RETRIEVE] Executing vector search with query: '{normalized_query}'")
            nodes = retriever.retrieve(normalized_query)
            logger.info(f"[RETRIEVE] Vector search returned {len(nodes)} nodes")

            source_chunks = []

            for node in nodes:
                source_chunks.append(
                    {
                        "text": node.get_content(),
                        "score": node.score or 0.0,
                        "metadata": node.metadata,
                    }
                )

            # Apply reranking if enabled
            if self.reranker and source_chunks:
                logger.info(f"[RETRIEVE] Reranking {len(source_chunks)} chunks")
                source_chunks = self.reranker.rerank(normalized_query, source_chunks, top_k=top_k)
                logger.info(f"[RETRIEVE] After reranking: {len(source_chunks)} chunks")

            # Enrich with parent chunks for context
            source_chunks = self._enrich_chunks_with_parents(source_chunks, collection)

            logger.info(
                "Retrieved %s chunks for document %s",
                len(source_chunks),
                document_id,
            )

            return {
                "success": True,
                "document_id": document_id,
                "query": query,
                "source_chunks": source_chunks,
                "chunks_count": len(source_chunks),
            }

        except Exception as e:
            logger.exception(
                "Retrieval failed for document %s",
                document_id,
            )

            return {
                "success": False,
                "document_id": document_id,
                "query": query,
                "error": str(e),
            }

    def summarize_document(self, document_id: str, query: str) -> dict:
        # read the whole document from the document_id

        # run the summarization with map_reduce
        ## from 
        # answer the query from the summary
        pass