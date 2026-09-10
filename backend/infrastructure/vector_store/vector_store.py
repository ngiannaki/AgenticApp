import chromadb
from llama_index.core import Document, VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core.storage import StorageContext
import logging
from pathlib import Path
import json

from backend.infrastructure.vector_store.metadata_enhancer import (
    MetadataEnhancer,
    HierarchicalChunker,
    TabularChunker,
    split_into_pages,
)
from backend.config import get_llamaindex_embedding, settings

logger = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, persist_dir: str | None = None, host: str | None = None, port: int | None = None):
        """
        Initialize vector store with ChromaDB backend and the configured
        embedding model (Ollama in DEV, OpenAI in PROD).
        Vectors are persisted to disk organized by document_id.

        Args:
            persist_dir: Directory where vectors will be persisted. Only used
                to make sure it exists — the actual store is a standalone
                `chroma run` server process at host:port (see
                start_mcp_servers.ps1), not opened directly here.
            host: Chroma server host (defaults to settings.vector_store.host)
            port: Chroma server port (defaults to settings.vector_store.port)
        """
        # Create persistence directory (used by the standalone chroma server)
        self.persist_dir = Path(persist_dir or settings.vector_store.dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # Initialize embeddings
        self.embed_model = get_llamaindex_embedding(settings.embedding_model)

        # Connect to the standalone Chroma server rather than opening the
        # on-disk store in-process — see VectorStoreSettings docstring for
        # why (this class and DocumentRetriever run in separate processes
        # and must share one consistent view of the store).
        self.chroma_client = chromadb.HttpClient(
            host=host or settings.vector_store.host,
            port=port or settings.vector_store.port,
        )

        # Initialize metadata enhancer and chunkers
        self.metadata_enhancer = MetadataEnhancer()
        self.hierarchical_chunker = HierarchicalChunker()
        self.tabular_chunker = TabularChunker()

        logger.info(f"Vector store initialized with persistent storage at: {self.persist_dir}")
    
    def index_document_enhanced(
        self,
        document_id: str,
        parsed_content: dict,
        document_name: str = "Untitled",
        use_hierarchical: bool = True,
        extract_metadata: bool = True,
        content_type: str = "pdf",
        extra_metadata: dict | None = None,
    ) -> dict:
        """
        Index a document with enhanced metadata extraction.

        Chunking strategy depends on `content_type`:
        - "csv"/"xlsx": row-group chunks via TabularChunker (a sliding text
          window over a rendered table risks splitting a row or separating
          it from its header — row-group chunks with the header repeated
          avoid both).
        - "pdf"/"pptx": hierarchical chunking over each physical page/slide
          (parsed_content["pages"]).
        - "docx"/"txt" (md/log included): hierarchical chunking over
          heading-delimited pseudo-pages (split_into_pages), so structure
          survives instead of one undifferentiated blob.

        Args:
            document_id: UUID of the document
            parsed_content: Parser output — 'pages'+'full_text' for
                pdf/pptx/docx/txt, or 'sheets' (list of {name, headers, rows})
                for csv/xlsx
            document_name: Original document file name
            use_hierarchical: Use hierarchical chunking instead of page-based
                (ignored for csv/xlsx, which always use TabularChunker)
            extract_metadata: Extract document summary and sections (skipped
                for csv/xlsx — a summary chunk is produced directly instead)
            content_type: One of detect.SUPPORTED_TYPES
            extra_metadata: Additional fields stamped onto every chunk
                (e.g. {"user_id": ...})

        Returns:
            Dictionary with indexing status and enhanced metadata
        """
        extra_metadata = extra_metadata or {}
        try:
            logger.info(f"[ENHANCED INDEX] Starting enhanced indexing for UUID: {document_id} (type={content_type})")

            if content_type in ("csv", "xlsx"):
                chunks = self.tabular_chunker.create_chunks_from_sheets(
                    parsed_content.get("sheets", []),
                    document_id=document_id,
                    document_name=document_name,
                    extra_metadata={**extra_metadata, "content_type": content_type},
                )
                enhanced_metadata = {"document_name": document_name, "document_type": content_type}
            else:
                enhanced_metadata = {}
                if extract_metadata and parsed_content.get("full_text"):
                    logger.info(f"[ENHANCED INDEX] Extracting document metadata...")
                    enhanced_metadata = self.metadata_enhancer.extract_metadata(
                        parsed_content["full_text"],
                        document_name=document_name,
                        content_type=content_type,
                    )
                    logger.info(f"[ENHANCED INDEX] Extracted metadata fields: {list(enhanced_metadata.keys())}")

                pages = parsed_content.get("pages", [])
                if not pages and content_type in ("docx", "txt"):
                    # No physical pages (docx/txt/md/log) — synthesize
                    # heading-delimited pseudo-pages so structure survives
                    # chunking instead of hierarchy-chunking one giant blob.
                    source_text = parsed_content.get("markdown") or parsed_content.get("full_text", "")
                    pages = split_into_pages(source_text)

                if use_hierarchical:
                    logger.info(f"[ENHANCED INDEX] Using hierarchical chunking...")
                    chunks = self.hierarchical_chunker.create_chunks_from_pages(
                        pages,
                        document_id=document_id,
                        document_name=document_name,
                        extra_metadata={**extra_metadata, "content_type": content_type},
                    )
                else:
                    logger.info(f"[ENHANCED INDEX] Using page-based chunking...")
                    chunks = self._create_page_chunks(
                        pages,
                        document_id=document_id,
                        document_name=document_name,
                    )
                    for chunk in chunks:
                        chunk["metadata"].update({**extra_metadata, "content_type": content_type})

            if not chunks:
                return {
                    "success": False,
                    "document_id": document_id,
                    "error": "No chunks created"
                }

            # Index with enhanced metadata
            result = self.index_document(document_id, chunks)

            # Add enhanced metadata to result
            result["enhanced_metadata"] = enhanced_metadata

            logger.info(f"[ENHANCED INDEX] Successfully indexed with enhanced metadata for {document_id}")

            return result

        except Exception as e:
            logger.error(f"[ENHANCED INDEX] Failed for {document_id}: {str(e)}", exc_info=True)
            return {
                "success": False,
                "document_id": document_id,
                "error": str(e)
            }
    
    def _create_page_chunks(
        self,
        pages: list,
        document_id: str,
        document_name: str = "Untitled"
    ) -> list:
        """Create simple page-based chunks."""
        chunks = []
        for page in pages:
            chunk = {
                "text": page.get("text", ""),
                "metadata": {
                    "document_id": document_id,
                    "document_name": document_name,
                    "page_number": page.get("page_number", 0),
                    "source": "page_based"
                }
            }
            chunks.append(chunk)
        return chunks
    
    def index_document(self, document_id: str, chunks: list[dict]) -> dict:
        """
        Index document chunks into the vector store using ChromaDB and LlamaIndex.
        
        Each document gets its own collection organized by document_id.
        Vectors are persisted to: data/vector_store/{document_id}/
        
        Args:
            document_id: UUID of the document
            chunks: List of chunk dictionaries with 'text' and optional 'metadata'
        
        Returns:
            Dictionary with indexing status and metadata
        """
        try:
            # Create a collection per document (organized by UUID). user_id
            # (if any chunk carries one) is stamped onto the collection itself
            # too, so retrieval can check ownership without a vector search.
            collection_name = f"doc_{document_id}"
            collection_metadata = {"hnsw:space": "cosine", "document_id": document_id}
            user_id = next((c.get("metadata", {}).get("user_id") for c in chunks if c.get("metadata", {}).get("user_id")), None)
            if user_id:
                collection_metadata["user_id"] = user_id
            collection = self.chroma_client.get_or_create_collection(
                name=collection_name,
                metadata=collection_metadata
            )
            
            # Create ChromaDB vector store
            chroma_vector_store = ChromaVectorStore(chroma_collection=collection)
            
            # Create storage context
            storage_context = StorageContext.from_defaults(
                vector_store=chroma_vector_store
            )
            
            # Create Document objects from chunks
            documents = []
            for i, chunk in enumerate(chunks):
                doc_text = chunk.get("text", "")
                doc_metadata = chunk.get("metadata", {})
                
                # Add document_id to metadata to link chunks to their source document
                doc_metadata["document_id"] = document_id
                doc_metadata["chunk_index"] = i
                
                doc = Document(
                    text=doc_text,
                    metadata=doc_metadata,
                    doc_id=f"{document_id}-chunk-{i}"
                )
                documents.append(doc)
            
            # Create index from documents
            index = VectorStoreIndex.from_documents(
                documents,
                storage_context=storage_context,
                embed_model=self.embed_model,
                show_progress=True
            )
            
            logger.info(f"Successfully indexed {len(documents)} chunks for document {document_id}")
            logger.info(f"Vectors persisted to: {self.persist_dir / collection_name}")
            
            return {
                "success": True,
                "document_id": document_id,
                "chunks_indexed": len(documents),
                "collection_name": collection_name,
                "persist_path": str(self.persist_dir / collection_name),
                "message": f"Document {document_id} indexed successfully"
            }
        
        except Exception as e:
            logger.error(f"Failed to index document {document_id}: {str(e)}")
            return {
                "success": False,
                "document_id": document_id,
                "error": str(e)
            }
    
    def search(self, query: str, document_id: str = None, top_k: int = 5) -> dict:
        """
        Search the vector store.
        
        Args:
            query: Search query
            document_id: Optional document UUID to search within specific document vectors
            top_k: Number of results to return
        
        Returns:
            Dictionary with search results
        """
        try:
            if document_id:
                # Search within a specific document's collection
                collection_name = f"doc_{document_id}"
                collection = self.chroma_client.get_collection(name=collection_name)
                chroma_vector_store = ChromaVectorStore(chroma_collection=collection)
                storage_context = StorageContext.from_defaults(
                    vector_store=chroma_vector_store
                )
            else:
                # Search across all documents - not implemented for cross-doc search
                return {
                    "success": False,
                    "error": "document_id is required for search. Each document has isolated vectors."
                }
            
            index = VectorStoreIndex.from_vector_store(
                chroma_vector_store,
                embed_model=self.embed_model
            )
            
            retriever = index.as_retriever(similarity_top_k=top_k)
            results = retriever.retrieve(query)
            
            return {
                "success": True,
                "query": query,
                "document_id": document_id,
                "results": [
                    {
                        "text": r.get_content(),
                        "score": r.score,
                        "metadata": r.metadata
                    }
                    for r in results
                ],
                "count": len(results)
            }
        
        except Exception as e:
            logger.error(f"Search failed: {str(e)}")
            return {
                "success": False,
                "error": str(e)
            }
