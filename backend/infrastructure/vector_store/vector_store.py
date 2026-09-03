import chromadb
from llama_index.core import Document, VectorStoreIndex
from llama_index.vector_stores.chroma import ChromaVectorStore
from llama_index.core.storage import StorageContext
import logging
from pathlib import Path
import json

from backend.infrastructure.vector_store.metadata_enhancer import (
    MetadataEnhancer,
    HierarchicalChunker
)
from backend.config import get_llamaindex_embedding, settings

logger = logging.getLogger(__name__)


class VectorStore:
    def __init__(self, persist_dir: str | None = None):
        """
        Initialize vector store with ChromaDB backend and the configured
        embedding model (Ollama in DEV, OpenAI in PROD).
        Vectors are persisted to disk organized by document_id.

        Args:
            persist_dir: Directory where vectors will be persisted
        """
        # Create persistence directory
        self.persist_dir = Path(persist_dir or settings.vector_store.dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)

        # Initialize embeddings
        self.embed_model = get_llamaindex_embedding(settings.embedding_model)

        # Initialize ChromaDB persistent client
        # This stores vectors to disk instead of in-memory
        self.chroma_client = chromadb.PersistentClient(
            path=str(self.persist_dir)
        )

        # Initialize metadata enhancer and hierarchical chunker
        self.metadata_enhancer = MetadataEnhancer()
        self.hierarchical_chunker = HierarchicalChunker()

        logger.info(f"Vector store initialized with persistent storage at: {self.persist_dir}")
    
    def index_document_enhanced(
        self,
        document_id: str,
        parsed_content: dict,
        document_name: str = "Untitled",
        use_hierarchical: bool = True,
        extract_metadata: bool = True
    ) -> dict:
        """
        Index a document with enhanced metadata extraction.
        
        Uses:
        - Hierarchical chunking for better semantic relationships
        - Document summary extraction
        - Section/topic extraction
        - Document statistics
        
        Args:
            document_id: UUID of the document
            parsed_content: Parsed document content dict with 'pages' and 'full_text'
            document_name: Original document file name
            use_hierarchical: Use hierarchical chunking instead of page-based
            extract_metadata: Extract document summary and sections
        
        Returns:
            Dictionary with indexing status and enhanced metadata
        """
        try:
            logger.info(f"[ENHANCED INDEX] Starting enhanced indexing for UUID: {document_id}")
            
            # Extract metadata if requested
            enhanced_metadata = {}
            if extract_metadata and parsed_content.get("full_text"):
                logger.info(f"[ENHANCED INDEX] Extracting document metadata...")
                enhanced_metadata = self.metadata_enhancer.extract_metadata(
                    parsed_content["full_text"],
                    document_name=document_name
                )
                logger.info(f"[ENHANCED INDEX] Extracted metadata fields: {list(enhanced_metadata.keys())}")
            
            # Create chunks
            if use_hierarchical:
                logger.info(f"[ENHANCED INDEX] Using hierarchical chunking...")
                chunks = self.hierarchical_chunker.create_chunks_from_pages(
                    parsed_content.get("pages", []),
                    document_id=document_id,
                    document_name=document_name
                )
            else:
                logger.info(f"[ENHANCED INDEX] Using page-based chunking...")
                chunks = self._create_page_chunks(
                    parsed_content.get("pages", []),
                    document_id=document_id,
                    document_name=document_name
                )
            
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
            # Create a collection per document (organized by UUID)
            collection_name = f"doc_{document_id}"
            collection = self.chroma_client.get_or_create_collection(
                name=collection_name,
                metadata={"hnsw:space": "cosine", "document_id": document_id}
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
