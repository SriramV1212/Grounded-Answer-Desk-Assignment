"""One-off data-quality audit of the anthropic_docs collection: a random
sample of stored chunks to eyeball for garbled text or bad headings, plus a
targeted check on the stray code-fence chunk found during Step 2 ingestion.

Run with: uv run python mcp_server/spot_check.py
"""

import os
import random

from qdrant_client import QdrantClient

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "anthropic_docs")
SAMPLE_SIZE = 20


def main() -> None:
    client = QdrantClient(url=QDRANT_URL)
    total = client.count(collection_name=QDRANT_COLLECTION, exact=True).count
    print(f"Collection '{QDRANT_COLLECTION}' has {total} points\n")

    all_points = []
    next_offset = None
    while True:
        points, next_offset = client.scroll(
            collection_name=QDRANT_COLLECTION,
            with_payload=True,
            with_vectors=False,
            limit=512,
            offset=next_offset,
        )
        all_points.extend(points)
        if next_offset is None:
            break

    sample = random.sample(all_points, min(SAMPLE_SIZE, len(all_points)))
    print(f"=== Random sample of {len(sample)} chunks ===\n")
    for i, point in enumerate(sample, 1):
        payload = point.payload or {}
        text = payload.get("text", "")
        print(f"[{i}] section_heading={payload.get('section_heading')!r}")
        print(f"    parent_heading={payload.get('parent_heading')!r}")
        print(f"    source_url={payload.get('source_url')}")
        print(f"    text ({len(text.split())} words): {text[:200]!r}")
        print()

    print("=== Stray code-fence chunk check (known Step 2 edge case) ===")
    fence_chunks = [p for p in all_points if (p.payload or {}).get("text", "").strip() == "```"]
    if not fence_chunks:
        print("Not found in this run's chunk set.")
    else:
        for point in fence_chunks:
            payload = point.payload or {}
            print(f"  chunk_id={payload.get('chunk_id')}")
            print(f"  section_heading={payload.get('section_heading')!r}")
            print(f"  parent_heading={payload.get('parent_heading')!r}")
            print(f"  source_url={payload.get('source_url')}")
            print(f"  text={payload.get('text')!r}")


if __name__ == "__main__":
    main()
