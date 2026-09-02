import json
import logging
import os
from typing import Any
from contextlib import AsyncExitStack

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from openai import AsyncOpenAI

from backend.core.config import settings

logger = logging.getLogger(__name__)

def deduplicate_chunks(chunks):

    seen = set()
    result = []

    for chunk in chunks:

        text = chunk.get(
            "text",
            "",
        ).strip()

        if not text:
            continue

        key = text

        if key in seen:
            continue

        seen.add(key)
        result.append(chunk)

    return result

class RetrievalAgent:
    """
    Agentic RAG agent.

    The agent uses:
        - Ollama for reasoning and answer generation
        - MCP for document retrieval
        - ChromaDB indirectly through the MCP server

    Retrieval loop:

        question
            ↓
        MCP retrieval
            ↓
        evaluate evidence
            ↓
        ┌───────────────┐
        │ sufficient?   │
        └───────┬───────┘
                │
          ┌─────┴─────┐
          │           │
         yes          no
          │           │
          ↓           ↓
       answer    rewrite query
                      │
                      ↓
                 MCP retrieval
    """

    def __init__(self) -> None:

        # ---------------------------------------------------------
        # Configuration
        # ---------------------------------------------------------

        self.ollama_base_url = f"{settings.base_url}/v1"

        self.model = settings.chat_model

        self.mcp_url = os.getenv(
            "RETRIEVAL_MCP_URL",
            "http://localhost:8001/mcp",
        )

        self.top_k = int(
            os.getenv(
                "RETRIEVAL_TOP_K",
                "10",
            )
        )

        self.max_iterations = int(
            os.getenv(
                "RETRIEVAL_MAX_ITERATIONS",
                "5",
            )
        )

        # ---------------------------------------------------------
        # Ollama
        # ---------------------------------------------------------

        self.llm = AsyncOpenAI(
            base_url=self.ollama_base_url,
            api_key="ollama",
        )

        logger.info(
            "RetrievalAgent initialized "
            "model=%s mcp=%s top_k=%s max_iterations=%s",
            self.model,
            self.mcp_url,
            self.top_k,
            self.max_iterations,
        )

    # ============================================================
    # MCP
    # ============================================================

    async def retrieve(
        self,
        document_id: str,
        query: str,
    ) -> dict[str, Any]:
        """
        Call the FastMCP retrieval server.

        The MCP server is responsible only for retrieving
        relevant document chunks.
        """

        logger.info(
            "MCP retrieval: document=%s query=%s",
            document_id,
            query,
        )

        async with streamable_http_client(
            self.mcp_url
        ) as (read, write, _):

            async with ClientSession(
                read,
                write,
            ) as session:

                await session.initialize()

                result = await session.call_tool(
                    "retrieve_document",
                    {
                        "document_id": document_id,
                        "query": query,
                        "top_k": self.top_k,
                    },
                )

                return self._parse_mcp_result(result)

    @staticmethod
    def _parse_mcp_result(
        result: Any,
    ) -> dict[str, Any]:

        if not result.content:
            return {
                "success": False,
                "error": "MCP returned empty content.",
            }

        content = result.content[0]

        if hasattr(content, "text"):

            try:
                parsed = json.loads(
                    content.text
                )

                if isinstance(parsed, dict):
                    return parsed

                return {
                    "success": True,
                    "result": parsed,
                }

            except json.JSONDecodeError:

                return {
                    "success": False,
                    "error": content.text,
                }

        return {
            "success": False,
            "error": str(content),
        }

    # ============================================================
    # Evidence formatting
    # ============================================================

    @staticmethod
    def format_evidence(
        retrieval_results: list[dict[str, Any]],
    ) -> str:
        """
        Convert accumulated retrieved chunks into evidence
        for Ollama.

        Duplicate chunks are removed across retrieval iterations.
        """

        if not retrieval_results:
            return "No evidence was retrieved."

        all_chunks = []

        for result in retrieval_results:
            chunks = result.get(
                "source_chunks",
                [],
            )

            all_chunks.extend(chunks)

        all_chunks = deduplicate_chunks(all_chunks)

        if not all_chunks:
            return "No evidence was retrieved."

        sections = []

        for index, chunk in enumerate(
            all_chunks,
            start=1,
        ):

            text = chunk.get(
                "text",
                "",
            )

            score = chunk.get(
                "score",
            )

            metadata = chunk.get(
                "metadata",
                {},
            )

            sections.append(
                f"""
    ===============================
    SOURCE {index}
    ===============================

    TEXT:
    {text}

    SCORE:
    {score}

    METADATA:
    {json.dumps(
        metadata,
        ensure_ascii=False,
        default=str,
    )}
    """
            )

        return "\n".join(sections)
    # ============================================================
    # Retrieval evaluation
    # ============================================================

    async def evaluate_evidence(
        self,
        question: str,
        evidence: str,
    ) -> dict[str, Any]:
        """
        Ask Ollama whether the retrieved evidence is sufficient.

        If insufficient, Ollama generates ONE focused
        follow-up retrieval query.
        """

        prompt = f"""
You are an expert retrieval evaluator.

Your job is to determine whether the retrieved
document evidence is sufficient to answer the
user's question.

USER QUESTION:
{question}

RETRIEVED DOCUMENT EVIDENCE:
{evidence}

Evaluate the evidence carefully.

Return ONLY valid JSON.

If the evidence is sufficient to answer the question,
return:

{{
    "status": "sufficient",
    "missing_information": [],
    "next_query": null
}}

If the evidence is NOT sufficient, return:

{{
    "status": "insufficient",
    "missing_information": [
        "specific missing fact 1",
        "specific missing fact 2"
    ],
    "next_query": "ONE focused retrieval query"
}}

Rules:

1. Use ONLY the retrieved document evidence.
2. Do not use general knowledge.
3. Do not invent facts.
4. Identify the specific information that is missing.
5. Generate exactly ONE follow-up retrieval query.
6. The follow-up query must target the missing information.
7. Do not simply repeat the original question.
8. Prefer concrete entities, terms, names, dates,
   sections, or concepts from the evidence.
9. If the evidence is sufficient, stop retrieval.
10. Return JSON only.
"""

        response = await self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a precise retrieval "
                        "evaluation engine."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0,
            response_format={"type":"json_object"}
        )

        content = (
            response.choices[0]
            .message
            .content
        )

        if not content:
            raise RuntimeError(
                "Ollama returned an empty evaluation response."
            )

        logger.info(
            "Retrieval evaluation: %s",
            content,
        )

        return self._parse_json_response(
            content
        )

    # ============================================================
    # Final answer
    # ============================================================

    async def generate_answer(
        self,
        question: str,
        evidence: str,
    ) -> str:
        """
        Generate the final answer from accumulated evidence.
        """

        prompt = f"""
You are an expert document question-answering agent.

Answer the user's question using ONLY the retrieved
document evidence.

USER QUESTION:
{question}

DOCUMENT EVIDENCE:
{evidence}

Instructions:

1. Answer the question directly.
2. Use only information supported by the evidence.
3. Do not use outside knowledge.
4. Do not hallucinate missing facts.
5. If the evidence does not contain enough information,
   explicitly say that the document does not provide
   enough information.
6. When metadata contains page numbers, sections,
   or source information, use them to identify sources.
7. Prefer a concise but complete answer.
"""

        response = await self.llm.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You answer questions using "
                        "document evidence only."
                    ),
                },
                {
                    "role": "user",
                    "content": prompt,
                },
            ],
            temperature=0,
        )

        content = (
            response.choices[0]
            .message
            .content
        )

        if not content:
            return (
                "I could not generate an answer "
                "from the retrieved document."
            )

        return content

    # ============================================================
    # Main agent loop
    # ============================================================

    # async def answer_query(
    #     self,
    #     document_id: str,
    #     question: str,
    # ) -> str:
    #     """
    #     Run the agentic retrieval loop.
    #     """

    #     if not document_id:
    #         raise ValueError(
    #             "document_id is required."
    #         )

    #     if not question.strip():
    #         raise ValueError(
    #             "question is required."
    #         )

    #     logger.info(
    #         "Starting retrieval agent: "
    #         "document=%s question=%s",
    #         document_id,
    #         question,
    #     )

    #     current_query = question

    #     all_retrieval_results: list[
    #         dict[str, Any]
    #     ] = []

    #     # ---------------------------------------------------------
    #     # Agentic retrieval loop
    #     # ---------------------------------------------------------

    #     for iteration in range(
    #         1,
    #         self.max_iterations + 1,
    #     ):

    #         logger.info(
    #             "Retrieval iteration %s/%s",
    #             iteration,
    #             self.max_iterations,
    #         )

    #         # ---------------------------------------------
    #         # 1. Retrieve from MCP
    #         # ---------------------------------------------

    #         retrieval_result = await self.retrieve(
    #             document_id=document_id,
    #             query=current_query,
    #         )

    #         if not retrieval_result.get(
    #             "success",
    #             False,
    #         ):

    #             error = retrieval_result.get(
    #                 "error",
    #                 "Unknown retrieval error.",
    #             )

    #             logger.error(
    #                 "Retrieval failed: %r",
    #                 error,
    #             )

    #             return (
    #                 f"I was unable to retrieve "
    #                 f"information from the document. "
    #                 f"Error: {error}"
    #             )

    #         all_retrieval_results.append(
    #             retrieval_result
    #         )

    #         # ---------------------------------------------
    #         # 2. Build accumulated evidence
    #         # ---------------------------------------------

    #         evidence = self.format_evidence(
    #             all_retrieval_results
    #         )

    #         # ---------------------------------------------
    #         # 3. Ask Ollama whether evidence is enough
    #         # ---------------------------------------------

    #         decision = await self.evaluate_evidence(
    #             question=question,
    #             evidence=evidence,
    #         )

    #         status = decision.get(
    #             "status"
    #         )

    #         logger.info(
    #             "Retrieval decision: %s",
    #             decision,
    #         )

    #         # ---------------------------------------------
    #         # 4. We have enough evidence
    #         # ---------------------------------------------

    #         if status == "sufficient":

    #             logger.info(
    #                 "Evidence sufficient after "
    #                 "%s retrieval iteration(s).",
    #                 iteration,
    #             )

    #             return await self.generate_answer(
    #                 question=question,
    #                 evidence=evidence,
    #             )

    #         # ---------------------------------------------
    #         # 5. Evidence insufficient
    #         # ---------------------------------------------

    #         if status != "insufficient":

    #             logger.warning(
    #                 "Unexpected retrieval status: %s",
    #                 status,
    #             )

    #             break

    #         next_query = decision.get(
    #             "next_query"
    #         )

    #         if not next_query:
    #             logger.warning(
    #                 "Agent said evidence was insufficient "
    #                 "but did not provide a next query."
    #             )
    #             break

    #         logger.info(
    #             "Next retrieval query: %s",
    #             next_query,
    #         )

    #         current_query = next_query

    #     # ---------------------------------------------------------
    #     # Maximum iterations reached
    #     # ---------------------------------------------------------

    #     logger.info(
    #         "Maximum retrieval iterations reached."
    #     )

    #     evidence = self.format_evidence(
    #         all_retrieval_results
    #     )

    #     return await self.generate_answer(
    #         question=question,
    #         evidence=evidence,
    #     )

    async def answer_query(
        self,
        document_id: str,
        question: str,
    ) :#-> dict[str, str | int | list[str]]:
        if not document_id:
            raise ValueError(
                "document_id is required."
            )

        if not question or not question.strip():
            raise ValueError(
                "question is required."
            )

        async with AsyncExitStack() as stack:

            # ==============================================
            # CONNECT TO MCP ONCE
            # ==============================================

            streams = await stack.enter_async_context(
                streamable_http_client(
                    self.mcp_url
                )
            )

            read_stream = streams[0]
            write_stream = streams[1]

            session = await stack.enter_async_context(
                ClientSession(
                    read_stream,
                    write_stream,
                )
            )

            await session.initialize()

            logger.info(
                "MCP session initialized"
            )

            # ==============================================
            # AGENTIC RAG LOOP
            # ==============================================

            all_results = []
            previous_queries = set()

            current_query = question
            for iteration in range(
                self.max_iterations
            ):

                logger.info(
                    "RAG iteration %d/%d",
                    iteration + 1,
                    self.max_iterations,
                )

                logger.info(
                    "Retrieval query: %s",
                    current_query,
                )
                normalized_query = current_query.strip().lower()

                if normalized_query in previous_queries:
                    logger.warning(
                        "Query repeated. Stopping retrieval loop: %s",
                        current_query,
                    )
                    break

                previous_queries.add(normalized_query)

                # ------------------------------------------
                # RETRIEVE
                # ------------------------------------------

                result = await session.call_tool(
                    "retrieve_document",
                    {
                        "document_id": document_id,
                        "query": current_query,
                        "top_k": self.top_k,
                    },
                )

                retrieval = self._parse_mcp_result(
                    result
                )

                if not retrieval.get("success"):
                    raise RuntimeError(
                        retrieval.get(
                            "error",
                            "MCP retrieval failed",
                        )
                    )

                chunks = retrieval.get(
                    "source_chunks",
                    [],
                )

                logger.info(
                    "Retrieved %d chunks",
                    len(chunks),
                )

                all_results.append(
                    retrieval
                )

                # ------------------------------------------
                # FORMAT EVIDENCE
                # ------------------------------------------

                evidence = self.format_evidence(
                    all_results
                )

                # ------------------------------------------
                # ASK OLLAMA
                # ------------------------------------------

                decision = await self.evaluate_evidence(
                    question=question,
                    evidence=evidence,
                )

                logger.info(
                    "Decision: %s",
                    decision,
                )

                # ------------------------------------------
                # STOP
                # ------------------------------------------

                if decision["status"] == "sufficient":

                    logger.info(
                        "Evidence sufficient."
                    )

                    break

                # ------------------------------------------
                # REWRITE
                # ------------------------------------------

                next_query = decision.get(
                    "next_query"
                )

                if not next_query:
                    logger.warning(
                        "No next query returned."
                    )
                    break

                current_query = next_query

            # ==============================================
            # FINAL ANSWER
            # ==============================================

            evidence = self.format_evidence(
                all_results
            )
            print(f"\n{'='*60}")
            print("FINAL EVIDENCE")
            print(f"{'='*60}")
            print(evidence)

            answer = await self.generate_answer(
                    question=question,
                    evidence=evidence,
                )

            # =============================================
            # FORMAT SOURCES
            # =============================================
            sources = []
            seen = set()
            for result in all_results:
                for chunk in result.get("source_chunks", []):
                    text = chunk.get("text", "").strip()

                    if not text or text in seen:
                        continue

                    seen.add(text)

                    sources.append({
                        "text": text,
                        "score": chunk.get("score"),
                        "metadata": chunk.get("metadata", {}),
                    })

        return {
            "answer": answer,
            "sources": sources,
            "retrieval_iterations": len(all_results),
        }

    # ============================================================
    # JSON helper
    # ============================================================

    @staticmethod
    def _parse_json_response(content: str) -> dict:
        """
        Parse JSON returned by Ollama.

        Handles:
        - normal JSON
        - ```json ... ``` responses
        - surrounding whitespace
        - occasional text before/after JSON
        """

        if not content:
            raise RuntimeError(
                "Ollama returned an empty response."
            )

        content = content.strip()

        logger.debug(
            "Raw Ollama JSON response repr: %r",
            content,
        )

        # ---------------------------------------------------------
        # 1. Remove Markdown code fences
        # ---------------------------------------------------------

        if content.startswith("```"):
            lines = content.splitlines()

            # Remove opening ```json / ```
            if lines:
                lines = lines[1:]

            # Remove closing ```
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]

            content = "\n".join(lines).strip()

        # ---------------------------------------------------------
        # 2. Try normal JSON first
        # ---------------------------------------------------------

        try:
            result = json.loads(content)

        except json.JSONDecodeError:

            # -----------------------------------------------------
            # 3. Try extracting the JSON object
            # -----------------------------------------------------

            start = content.find("{")
            end = content.rfind("}")

            if start == -1 or end == -1 or end <= start:
                logger.error(
                    "Invalid JSON from Ollama: %r",
                    content,
                )

                raise RuntimeError(
                    "Ollama returned invalid JSON "
                    "for retrieval evaluation."
                )

            json_text = content[start:end + 1]

            try:
                result = json.loads(json_text)

            except json.JSONDecodeError as exc:

                logger.error(
                    "Invalid JSON from Ollama: %r",
                    content,
                )

                raise RuntimeError(
                    "Ollama returned invalid JSON "
                    "for retrieval evaluation."
                ) from exc

        # ---------------------------------------------------------
        # 4. Validate object
        # ---------------------------------------------------------

        if not isinstance(result, dict):
            raise RuntimeError(
                "Ollama retrieval evaluation "
                "must return a JSON object."
            )

        # ---------------------------------------------------------
        # 5. Validate status
        # ---------------------------------------------------------

        status = result.get("status")

        if status not in {
            "sufficient",
            "insufficient",
        }:
            raise RuntimeError(
                f"Invalid retrieval status: {status!r}"
            )

        # ---------------------------------------------------------
        # 6. Normalize fields
        # ---------------------------------------------------------

        missing_information = result.get(
            "missing_information",
            [],
        )

        if not isinstance(missing_information, list):
            raise RuntimeError(
                "missing_information must be a list."
            )

        next_query = result.get("next_query")

        # ---------------------------------------------------------
        # next_query rules:
        #
        # sufficient   -> must be null/None
        # insufficient -> must be a non-empty string
        # ---------------------------------------------------------

        if status == "sufficient":

            # We don't need a follow-up query.
            next_query = None

        else:  # insufficient

            if not isinstance(next_query, str):
                raise RuntimeError(
                    "next_query must be a string when "
                    "status is insufficient."
                )

            next_query = next_query.strip()

            if not next_query:
                raise RuntimeError(
                    "next_query cannot be empty when "
                    "status is insufficient."
                )

        return {
            "status": status,
            "missing_information": missing_information,
            "next_query": next_query,
        }
 