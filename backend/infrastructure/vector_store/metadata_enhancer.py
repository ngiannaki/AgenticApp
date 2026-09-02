"""
Enhanced metadata extraction for documents using LlamaIndex.

Provides:
- Document summaries
- Section/hierarchy extraction
- Document statistics
- Metadata enrichment
"""

from typing import Optional, Dict, List
from llama_index.core import Document
from llama_index.core.node_parser import HierarchicalNodeParser, SimpleNodeParser
from llama_index.llms.ollama import Ollama
import logging

from backend.core.config import settings

logger = logging.getLogger(__name__)


class MetadataEnhancer:
    """Enhances document metadata with summaries, sections, and hierarchy info."""

    def __init__(self, ollama_base_url: str = settings.base_url):
        """
        Initialize metadata enhancer with Ollama LLM.

        Args:
            ollama_base_url: URL where Ollama is running
        """
        self.llm = Ollama(
            model=settings.chat_model,
            base_url=ollama_base_url,
            temperature=0.1,
        )
        
        logger.info("MetadataEnhancer initialized")
    
    def extract_sections(self, text: str, top_k: int = 5) -> List[Dict]:
        """
        Extract main sections/topics from document text using LLM.
        
        Args:
            text: Document text
            top_k: Number of top sections to extract
        
        Returns:
            List of sections with titles and descriptions
        """
        try:
            if not text or len(text.strip()) < 100:
                return []
            
            prompt = f"""Extract the {top_k} main sections or topics from this document text.
Return as JSON array with "title" and "description" fields.

Text:
{text[:2000]}...

Return ONLY the JSON array, nothing else."""
            
            response = self.llm.complete(prompt)
            
            # Try to parse JSON from response
            try:
                import json
                # Find JSON array in response
                response_text = str(response)
                start_idx = response_text.find('[')
                end_idx = response_text.rfind(']') + 1
                
                if start_idx != -1 and end_idx > start_idx:
                    sections = json.loads(response_text[start_idx:end_idx])
                    return sections[:top_k]
            except:
                pass
            
            return []
        
        except Exception as e:
            logger.warning(f"Failed to extract sections: {str(e)}")
            return []
    
    def generate_summary(self, text: str, max_length: int = 500) -> str:
        """
        Generate a concise summary of the document.
        
        Args:
            text: Document text
            max_length: Maximum summary length
        
        Returns:
            Document summary
        """
        try:
            if not text or len(text.strip()) < 100:
                return ""
            
            prompt = f"""Summarize this document in {max_length} characters or less. 
Be concise and capture the main points.

Document:
{text[:3000]}...

Summary:"""
            
            response = self.llm.complete(prompt, max_tokens=max_length // 2)  # Rough estimate of tokens
            summary = str(response).strip()
            
            # Trim to max length
            # if len(summary) > max_length:
            #     summary = summary[:max_length] + "..."
            
            return summary
        
        except Exception as e:
            logger.warning(f"Failed to generate summary: {str(e)}")
            return ""
    
    def extract_metadata(self, text: str, document_name: str = "Untitled") -> Dict:
        """
        Extract comprehensive metadata from document.
        
        Args:
            text: Document text
            document_name: Original document file name
        
        Returns:
            Dictionary with enhanced metadata
        """
        try:
            word_count = len(text.split())
            char_count = len(text)
            
            metadata = {
                "document_name": document_name,
                "word_count": word_count,
                "char_count": char_count,
                "document_type": "pdf",  # Can be enhanced based on actual file type
            }
            
            # Generate summary
            if word_count > 50:
                summary = self.generate_summary(text)
                if summary:
                    metadata["summary"] = summary
            
            # Extract sections
            if word_count > 100:
                sections = self.extract_sections(text)
                if sections:
                    metadata["sections"] = sections
                    metadata["section_count"] = len(sections)
            
            logger.info(f"Enhanced metadata extracted: {len(metadata)} fields")
            return metadata
        
        except Exception as e:
            logger.error(f"Failed to extract metadata: {str(e)}")
            return {
                "document_name": document_name,
                "document_type": "pdf",
                "error": str(e)
            }


class HierarchicalChunker:
    """Create hierarchical chunks using LlamaIndex HierarchicalNodeParser."""

    def __init__(self):
        """Initialize hierarchical node parser with production-optimized settings."""
        self.parser = HierarchicalNodeParser.from_defaults(
            chunk_sizes=[1024, 256],  # Large, medium, small chunks (optimized for ~400, ~100, ~25 tokens)
            chunk_overlap=100,  # ~47% overlap on smallest chunk for context continuity (must be < min chunk_size)
        )

        logger.info("HierarchicalChunker initialized with production chunk sizes: [1024, 256], overlap: 100")
    
    def create_hierarchical_chunks(
        self,
        text: str,
        document_id: str,
        document_name: str = "Untitled",
        page_number: int = 1
    ) -> List[Dict]:
        """
        Create hierarchical chunks from text.
        
        Each chunk has information about its hierarchy level and parent chunks.
        
        Args:
            text: Text to chunk
            document_id: Document UUID
            document_name: Original document name
            page_number: Page number (for multi-page docs)
        
        Returns:
            List of chunks with hierarchy metadata
        """
        try:
            # Create Document object
            doc = Document(
                text=text,
                metadata={
                    "document_id": document_id,
                    "document_name": document_name,
                    "page_number": page_number,
                }
            )
            
            # Parse into hierarchical nodes
            nodes = self.parser.get_nodes_from_documents([doc])
            
            # Convert nodes to chunk dictionaries
            chunks = []
            for i, node in enumerate(nodes):
                chunk = {
                    "text": node.get_content(),
                    "metadata": {
                        "document_id": document_id,
                        "document_name": document_name,
                        "page_number": page_number,
                        "chunk_index": i,
                        "chunk_id": node.node_id if hasattr(node, 'node_id') else f"{document_id}-chunk-{i}",
                        "hierarchy_level": getattr(node, 'level', 0) if hasattr(node, 'level') else 0,
                        "source": "hierarchical_parse"
                    }
                }
                
                # Add parent node reference if exists
                if hasattr(node, 'parent_node') and node.parent_node:
                    chunk["metadata"]["parent_chunk_id"] = node.parent_node.node_id
                
                chunks.append(chunk)
            
            logger.info(f"Created {len(chunks)} hierarchical chunks for {document_id}")
            return chunks
        
        except Exception as e:
            logger.warning(f"Hierarchical chunking failed, falling back to simple chunks: {str(e)}")
            return [
                {
                    "text": text,
                    "metadata": {
                        "document_id": document_id,
                        "document_name": document_name,
                        "page_number": page_number,
                        "chunk_index": 0,
                        "source": "fallback_simple",
                    }
                }
            ]
    
    def create_chunks_from_pages(
        self,
        pages: List[Dict],
        document_id: str,
        document_name: str = "Untitled"
    ) -> List[Dict]:
        """
        Create hierarchical chunks from pages.
        
        Args:
            pages: List of page dicts with 'text' and 'page_number'
            document_id: Document UUID
            document_name: Original document name
        
        Returns:
            List of hierarchical chunks
        """
        all_chunks = []
        
        for page in pages:
            page_text = page.get("text", "")
            page_num = page.get("page_number", 1)
            
            if page_text.strip():
                page_chunks = self.create_hierarchical_chunks(
                    text=page_text,
                    document_id=document_id,
                    document_name=document_name,
                    page_number=page_num
                )
                all_chunks.extend(page_chunks)
        
        return all_chunks
