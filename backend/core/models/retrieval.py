class RetrievalResult:
    chunk_id: str
    document_id: str
    text: str
    score: float
    metadata: dict

class RetrievalResponse:
    answerable: bool
    evidence: list[RetrievalResult]
    searches_performed: list[str]
    confidence: float
    missing_information: list[str]