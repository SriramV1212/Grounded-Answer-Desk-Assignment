"""Download, clean, chunk, embed, and upsert the Anthropic docs corpus into Qdrant."""

import os
import re
import uuid
from pathlib import Path

import httpx
import tiktoken
from dotenv import load_dotenv
from langchain_text_splitters import RecursiveCharacterTextSplitter
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams
from sentence_transformers import SentenceTransformer

load_dotenv()

CORPUS_URL = "https://docs.anthropic.com/llms-full.txt"
RAW_PATH = Path(__file__).parent / "raw" / "llms-full.txt"
MAX_PAGES = 100

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "anthropic_docs")

EMBED_MODEL = "nomic-ai/nomic-embed-text-v1"
EMBED_DIM = 768

CHUNK_MAX_TOKENS = 400
CHUNK_MIN_TARGET_TOKENS = 300
CHUNK_TARGET_TOKENS = 380
CHUNK_OVERLAP_TOKENS = 50
# Trailing fragment left at the end of a page, too small to stand on its own.
MIN_VIABLE_CHUNK_TOKENS = 50

UPSERT_BATCH_SIZE = 64
EMBED_BATCH_SIZE = 32

# Deterministic UUID namespace so re-running ingest.py upserts the same point
# IDs instead of duplicating vectors.
CHUNK_ID_NAMESPACE = uuid.UUID("6f6f7263-6873-4964-8e6e-616d65737061")

TAG_PATTERN = re.compile(r"</?[A-Z][\w.]*(?:\s+[^<>]*)?/?>")
CODE_FENCE_PATTERN = re.compile(r"```.*?```", re.DOTALL)
URL_MARKER_PATTERN = re.compile(r"\*\*URL:\*\*\s*(\S+)")
TOP_HEADING_PATTERN = re.compile(r"^#\s+(.+)$", re.MULTILINE)
SECTION_HEADING_PATTERN = re.compile(r"^(#{2,3})\s+(.+)$", re.MULTILINE)

_encoding = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding.encode(text))


def download_corpus() -> str:
    """Fetch the corpus and cache it locally so re-runs don't re-download."""
    if RAW_PATH.exists():
        print(f"Using cached corpus at {RAW_PATH}")
        return RAW_PATH.read_text(encoding="utf-8")

    print(f"Downloading {CORPUS_URL} ...")
    response = httpx.get(CORPUS_URL, timeout=60, follow_redirects=True)
    response.raise_for_status()
    text = response.text

    RAW_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_PATH.write_text(text, encoding="utf-8")
    print(f"Saved raw corpus ({len(text):,} chars) to {RAW_PATH}")
    return text


def strip_component_tags(text: str) -> str:
    """Strip JSX/MDX component tags, preserving their inner text and leaving
    fenced code blocks and tables untouched."""

    def clean_segment(segment: str) -> str:
        return TAG_PATTERN.sub("", segment)

    parts = CODE_FENCE_PATTERN.split(text)
    fences = CODE_FENCE_PATTERN.findall(text)

    cleaned_parts = [clean_segment(part) for part in parts]

    result = []
    for i, part in enumerate(cleaned_parts):
        result.append(part)
        if i < len(fences):
            result.append(fences[i])
    return "".join(result)


def split_into_pages(text: str) -> list[tuple[str, str]]:
    """Split the corpus on `**URL:**` markers into (source_url, page_text) pairs."""
    pieces = URL_MARKER_PATTERN.split(text)
    # pieces = [preamble, url1, content1, url2, content2, ...]
    pages = []
    for i in range(1, len(pieces), 2):
        url = pieces[i].strip()
        content = pieces[i + 1] if i + 1 < len(pieces) else ""
        pages.append((url, content))
    return pages


def _fence_spans(text: str) -> list[tuple[int, int]]:
    """Character ranges covered by fenced code blocks, so heading regexes can
    skip lines like `# comment` that appear inside example code rather than
    as real markdown structure."""
    return [m.span() for m in CODE_FENCE_PATTERN.finditer(text)]


def _in_fence(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= pos < end for start, end in spans)


def _non_fenced_matches(pattern: re.Pattern, text: str, spans: list[tuple[int, int]]) -> list[re.Match]:
    return [m for m in pattern.finditer(text) if not _in_fence(m.start(), spans)]


def _first_section_boundary(page_text: str, spans: list[tuple[int, int]]) -> int:
    """Position of the first real (non-fenced) ##/### header, or end-of-page
    if there isn't one. A page's real `#` title, if any, only ever appears
    before this point -- a stray `#` line inside a later section's body is
    just incidental text, not a heading."""
    matches = _non_fenced_matches(SECTION_HEADING_PATTERN, page_text, spans)
    return matches[0].start() if matches else len(page_text)


def extract_parent_heading(page_text: str, source_url: str) -> str:
    spans = _fence_spans(page_text)
    intro_end = _first_section_boundary(page_text, spans)
    top_matches = _non_fenced_matches(TOP_HEADING_PATTERN, page_text[:intro_end], spans)
    if top_matches:
        return top_matches[0].group(1).strip()
    # No real title in the intro: fall back to the last non-empty URL path segment.
    segment = source_url.rstrip("/").split("/")[-1]
    return segment.replace("-", " ").title() if segment else "Untitled"


def split_into_sections(page_text: str) -> list[tuple[str | None, str]]:
    """Pass 1: split page content on ##/### headers.

    Returns a list of (section_heading, section_body) tuples. section_heading
    is None for content that appears before the first ##/### header (the
    page's intro/overview), to be labelled with the parent heading upstream.
    Headers that fall inside fenced code blocks (e.g. `# comment` in a code
    sample) are not treated as real section boundaries.
    """
    spans = _fence_spans(page_text)
    matches = _non_fenced_matches(SECTION_HEADING_PATTERN, page_text, spans)
    sections = []

    intro_end = matches[0].start() if matches else len(page_text)
    intro = page_text[:intro_end]
    top_matches = _non_fenced_matches(TOP_HEADING_PATTERN, intro, spans)
    if top_matches:
        top_match = top_matches[0]
        intro = intro[: top_match.start()] + intro[top_match.end() :]
    intro = intro.strip()
    if len(intro) > 20:
        sections.append((None, intro))

    for idx, match in enumerate(matches):
        heading = match.group(2).strip()
        body_start = match.end()
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(page_text)
        body = page_text[body_start:body_end].strip()
        if body:
            sections.append((heading, body))

    return sections


text_splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
    encoding_name="cl100k_base",
    chunk_size=CHUNK_TARGET_TOKENS,
    chunk_overlap=CHUNK_OVERLAP_TOKENS,
    separators=["\n\n", "\n", ". ", " ", ""],
)


def chunk_page(source_url: str, parent_heading: str, page_text: str) -> list[dict]:
    """Pass 1 (header split, with small-section merging) + Pass 2 (token-bounded
    recursive split fallback for oversized sections).

    Header-bounded sections are frequently much smaller than the 300-400 token
    target (a "Prerequisites" section might be 20 tokens), so adjacent sections
    are accumulated into a running buffer and only finalized as a chunk once
    the buffer reaches the target range or the next section would overflow it.
    Only a section that is *individually* over CHUNK_MAX_TOKENS bypasses the
    buffer and goes straight through Pass 2.
    """
    sections = [
        (heading or parent_heading, body.strip())
        for heading, body in split_into_sections(page_text)
        if body.strip()
    ]

    chunks: list[dict] = []
    buffer_text = ""
    buffer_headings: list[str] = []

    def flush(text: str, headings: list[str]) -> None:
        text = text.strip()
        if not text or not headings:
            return
        chunks.append(
            {
                "text": text,
                "source_url": source_url,
                "section_heading": headings[-1],
                "parent_heading": parent_heading,
                "merged_headings": list(dict.fromkeys(headings)),
            }
        )

    for heading, body in sections:
        section_tokens = count_tokens(body)

        if section_tokens > CHUNK_MAX_TOKENS:
            # Oversized on its own: flush whatever's buffered, then Pass-2
            # split this section by itself.
            flush(buffer_text, buffer_headings)
            buffer_text, buffer_headings = "", []
            for piece in text_splitter.split_text(body):
                piece = piece.strip()
                if piece:
                    chunks.append(
                        {
                            "text": piece,
                            "source_url": source_url,
                            "section_heading": heading,
                            "parent_heading": parent_heading,
                            "merged_headings": [heading],
                        }
                    )
            continue

        candidate_text = f"{buffer_text}\n\n{body}".strip() if buffer_text else body
        candidate_tokens = count_tokens(candidate_text)

        if candidate_tokens > CHUNK_MAX_TOKENS:
            # Adding this section would overflow the buffer: finalize what's
            # there, then start a fresh buffer with this section.
            flush(buffer_text, buffer_headings)
            buffer_text, buffer_headings = body, [heading]
        else:
            buffer_text, buffer_headings = candidate_text, buffer_headings + [heading]
            if candidate_tokens >= CHUNK_MIN_TARGET_TOKENS:
                flush(buffer_text, buffer_headings)
                buffer_text, buffer_headings = "", []

    if buffer_text.strip():
        if count_tokens(buffer_text) < MIN_VIABLE_CHUNK_TOKENS and chunks:
            # Trailing fragment too small to stand alone: merge it backward
            # into the previous chunk from this same page instead of emitting
            # a near-empty chunk with a misleading, overly-specific heading.
            last = chunks[-1]
            last["text"] = f"{last['text']}\n\n{buffer_text}".strip()
            last["merged_headings"] = list(dict.fromkeys(last["merged_headings"] + buffer_headings))
            last["section_heading"] = last["merged_headings"][-1]
        else:
            flush(buffer_text, buffer_headings)

    return chunks


def make_chunk_id(source_url: str, section_heading: str, chunk_index: int) -> str:
    key = f"{source_url}|{section_heading}|{chunk_index}"
    return str(uuid.uuid5(CHUNK_ID_NAMESPACE, key))


def build_chunks(raw_text: str) -> list[dict]:
    cleaned = strip_component_tags(raw_text)
    pages = split_into_pages(cleaned)[:MAX_PAGES]
    print(f"Processing {len(pages)} pages (of {len(split_into_pages(cleaned))} available)")

    all_chunks = []
    chunk_index = 0
    for source_url, page_text in pages:
        parent_heading = extract_parent_heading(page_text, source_url)
        for chunk in chunk_page(source_url, parent_heading, page_text):
            chunk["chunk_index"] = chunk_index
            chunk["chunk_id"] = make_chunk_id(
                source_url, chunk["section_heading"], chunk_index
            )
            all_chunks.append(chunk)
            chunk_index += 1

    print(f"Built {len(all_chunks)} chunks from {len(pages)} pages")
    return all_chunks


def embed_chunks(chunks: list[dict]) -> list[list[float]]:
    print(f"Loading embedding model {EMBED_MODEL} ...")
    model = SentenceTransformer(EMBED_MODEL, trust_remote_code=True)
    texts = [c["text"] for c in chunks]
    print(f"Embedding {len(texts)} chunks ...")
    embeddings = model.encode(
        texts,
        batch_size=EMBED_BATCH_SIZE,
        show_progress_bar=True,
        convert_to_numpy=True,
    )
    return embeddings.tolist()


def upsert_chunks(chunks: list[dict], embeddings: list[list[float]]) -> None:
    client = QdrantClient(url=QDRANT_URL)

    # chunk_id is derived from a corpus-wide sequential chunk_index, so any
    # change to chunking logic shifts IDs for everything downstream of the
    # change. Recreating the collection on every run avoids accumulating
    # orphaned points from a previous script version instead of trying to
    # reconcile old and new ID sets.
    if client.collection_exists(QDRANT_COLLECTION):
        print(f"Dropping existing collection '{QDRANT_COLLECTION}' for a clean rebuild ...")
        client.delete_collection(QDRANT_COLLECTION)

    print(f"Creating collection '{QDRANT_COLLECTION}' ...")
    client.create_collection(
        collection_name=QDRANT_COLLECTION,
        vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
    )

    points = [
        PointStruct(
            id=chunk["chunk_id"],
            vector=vector,
            payload={
                "text": chunk["text"],
                "source_url": chunk["source_url"],
                "section_heading": chunk["section_heading"],
                "parent_heading": chunk["parent_heading"],
                "merged_headings": chunk.get("merged_headings", [chunk["section_heading"]]),
                "chunk_index": chunk["chunk_index"],
                "chunk_id": chunk["chunk_id"],
            },
        )
        for chunk, vector in zip(chunks, embeddings)
    ]

    print(f"Upserting {len(points)} points into '{QDRANT_COLLECTION}' ...")
    for i in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[i : i + UPSERT_BATCH_SIZE]
        client.upsert(collection_name=QDRANT_COLLECTION, points=batch)
        print(f"  upserted {min(i + UPSERT_BATCH_SIZE, len(points))}/{len(points)}")


def main() -> None:
    raw_text = download_corpus()
    chunks = build_chunks(raw_text)
    embeddings = embed_chunks(chunks)
    upsert_chunks(chunks, embeddings)
    print("Ingestion complete.")


if __name__ == "__main__":
    main()
