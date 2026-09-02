from pathlib import Path
from typing import Any
import pymupdf  # PyMuPDF

def parse_pdf(pdf_path: str) -> dict:

    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(
            f"PDF not found: {pdf_path}"
        )

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(
            f"Expected PDF, got {pdf_path.suffix}"
        )

    result = {
        "filename": pdf_path.name,
        "page_count": 0,
        "pages": [],
        "full_text": "",
    }

    with pymupdf.open(pdf_path) as pdf:

        result["page_count"] = len(pdf)

        all_text = []

        for page_number, page in enumerate(pdf, start=1):

            text = page.get_text("text")

            page_data = {
                "page_number": page_number,
                "text": text,
            }

            result["pages"].append(page_data)

            all_text.append(text)

    result["full_text"] = "\n\n".join(all_text)

    return result


def parse_pdf2(
    pdf_path: str | Path,
    extract_images: bool = False,
) -> dict[str, Any]:
    """
    Parse a PDF into a structured representation.

    Parameters
    ----------
    pdf_path:
        Path to the PDF file.

    extract_images:
        Whether to collect information about images found on each page.

    Returns
    -------
    dict
        Structured PDF representation containing:
        - document metadata
        - page information
        - text blocks
        - full document text
        - optional image metadata
    """

    pdf_path = Path(pdf_path)

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a PDF file, got: {pdf_path.suffix}")

    document = {
        "filename": pdf_path.name,
        "path": str(pdf_path),
        "metadata": {},
        "page_count": 0,
        "pages": [],
        "full_text": "",
    }

    with pymupdf.open(pdf_path) as pdf:

        # -------------------------
        # Document metadata
        # -------------------------

        document["metadata"] = pdf.metadata or {}
        document["page_count"] = len(pdf)

        all_page_text = []

        # -------------------------
        # Process pages
        # -------------------------

        for page_number, page in enumerate(pdf, start=1):

            page_data = {
                "page_number": page_number,
                "width": page.rect.width,
                "height": page.rect.height,
                "text": "",
                "blocks": [],
                "images": [],
            }

            # -------------------------
            # Extract structured text
            # -------------------------

            page_dict = page.get_text("dict", sort=True)

            page_text_parts = []

            for block_index, block in enumerate(
                page_dict.get("blocks", [])
            ):

                # Text block
                if block.get("type") == 0:

                    block_text_parts = []
                    lines = []

                    for line in block.get("lines", []):

                        line_text_parts = []
                        spans = []

                        for span in line.get("spans", []):

                            text = span.get("text", "")

                            if not text:
                                continue

                            line_text_parts.append(text)

                            spans.append({
                                "text": text,
                                "bbox": span.get("bbox"),
                                "font": span.get("font"),
                                "size": span.get("size"),
                                "flags": span.get("flags"),
                            })

                        line_text = "".join(line_text_parts).strip()

                        if line_text:
                            block_text_parts.append(line_text)

                            lines.append({
                                "text": line_text,
                                "bbox": line.get("bbox"),
                                "spans": spans,
                            })

                    block_text = "\n".join(block_text_parts).strip()

                    if block_text:

                        page_text_parts.append(block_text)

                        page_data["blocks"].append({
                            "block_id": f"p{page_number}_b{block_index}",
                            "type": "text",
                            "text": block_text,
                            "bbox": block.get("bbox"),
                            "lines": lines,
                        })

            page_data["text"] = "\n\n".join(page_text_parts)

            # -------------------------
            # Extract image information
            # -------------------------

            if extract_images:

                for image_index, image in enumerate(
                    page.get_images(full=True)
                ):

                    xref = image[0]

                    image_info = {
                        "image_index": image_index,
                        "xref": xref,
                        "width": image[2],
                        "height": image[3],
                        "colorspace": image[5],
                        "rects": [],
                    }

                    for rect in page.get_image_rects(xref):
                        image_info["rects"].append({
                            "x0": rect.x0,
                            "y0": rect.y0,
                            "x1": rect.x1,
                            "y1": rect.y1,
                        })

                    page_data["images"].append(image_info)

            document["pages"].append(page_data)

            all_page_text.append(page_data["text"])

        # -------------------------
        # Full document text
        # -------------------------

        document["full_text"] = "\n\n".join(
            text for text in all_page_text if text.strip()
        )

    return document