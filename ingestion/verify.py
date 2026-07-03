"""Sanity-check the anthropic_docs Qdrant collection after ingestion."""

import os
import sys

from dotenv import load_dotenv
from qdrant_client import QdrantClient

load_dotenv()

QDRANT_URL = os.environ.get("QDRANT_URL", "http://localhost:6333")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "anthropic_docs")

EXPECTED_MIN = 2500
EXPECTED_MAX = 3500
HARD_FLOOR = 1000
HARD_CEILING = 5000
SAMPLE_SIZE = 5


def main() -> int:
    client = QdrantClient(url=QDRANT_URL)

    if not client.collection_exists(QDRANT_COLLECTION):
        print(f"FAIL: collection '{QDRANT_COLLECTION}' does not exist at {QDRANT_URL}")
        return 1

    count = client.count(collection_name=QDRANT_COLLECTION, exact=True).count
    print(f"Collection '{QDRANT_COLLECTION}' at {QDRANT_URL}: {count} vectors")

    sample, _ = client.scroll(
        collection_name=QDRANT_COLLECTION,
        limit=SAMPLE_SIZE,
        with_payload=True,
        with_vectors=False,
    )

    print(f"\nSample of {len(sample)} points:")
    for point in sample:
        payload = point.payload or {}
        heading = payload.get("section_heading", "<missing>")
        parent = payload.get("parent_heading", "<missing>")
        url = payload.get("source_url", "<missing>")
        text_preview = (payload.get("text") or "")[:80].replace("\n", " ")
        print(f"  - [{parent} > {heading}] {url}")
        print(f"      \"{text_preview}...\"")

    print()
    if EXPECTED_MIN <= count <= EXPECTED_MAX:
        print(f"PASS: vector count {count} is within expected range [{EXPECTED_MIN}, {EXPECTED_MAX}]")
        return 0
    if count < HARD_FLOOR or count > HARD_CEILING:
        print(
            f"FAIL: vector count {count} is well outside the expected range "
            f"(hard bounds [{HARD_FLOOR}, {HARD_CEILING}]) — investigate ingestion"
        )
        return 1

    print(
        f"WARN: vector count {count} is outside the target range "
        f"[{EXPECTED_MIN}, {EXPECTED_MAX}] but within plausible bounds — "
        "corpus size can vary, review before treating as a hard failure"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
