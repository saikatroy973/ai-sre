#!/usr/bin/env python3
"""Build the Kubernetes documentation index for the MCP docs tool.

Source: the English documentation from kubernetes/website (CC BY 4.0).
Fetch it locally without growing the repository:

    git clone --depth 1 --filter=blob:none --sparse \
        https://github.com/kubernetes/website .data/k8s-docs/src
    git -C .data/k8s-docs/src sparse-checkout set content/en/docs

Then index it:

    python scripts/index_kubernetes_docs.py
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from ai_sre.knowledge import build_index, index_path

REPO = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = REPO / ".data" / "k8s-docs" / "src" / "content" / "en" / "docs"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--index", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=REPO / ".data")
    parser.add_argument(
        "--knowledge",
        type=Path,
        default=REPO / "knowledge",
        help="per-agent SRE knowledge root (indexed as knowledge/<agent>/...)",
    )
    args = parser.parse_args()

    index = args.index or index_path(args.data_dir)
    started = time.perf_counter()
    stats = build_index(
        args.source,
        index,
        extra=((args.knowledge, "knowledge"),) if args.knowledge.is_dir() else (),
    )
    elapsed = time.perf_counter() - started
    size = index.stat().st_size / 1e6
    print(
        f"indexed {stats['documents']} documents into {stats['chunks']} passages "
        f"({size:.1f} MB) at {index} in {elapsed:.1f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
