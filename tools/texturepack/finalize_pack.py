#!/usr/bin/env python3
"""Apply audited visual-review decisions and produce a safe texture pack."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from collections import Counter
from pathlib import Path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(args: argparse.Namespace) -> int:
    candidate_manifest_path = args.candidate_manifest.resolve()
    decisions_path = args.decisions.resolve()
    output = args.output.resolve()
    if output.exists():
        raise ValueError(f"output already exists: {output}")

    candidate_root = candidate_manifest_path.parent
    manifest = json.loads(candidate_manifest_path.read_text(encoding="utf-8"))
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    reviewed_ids = set(decisions["reviewed_ids"])
    fallbacks = decisions["stock_fallback"]
    required_review_ids = {
        entry["id_hex"]
        for entry in manifest["matches"]
        if entry["status"] == "visual-review-required"
    }
    if reviewed_ids != required_review_ids:
        missing = sorted(required_review_ids - reviewed_ids)
        extra = sorted(reviewed_ids - required_review_ids)
        raise ValueError(f"review coverage mismatch; missing={missing}, extra={extra}")
    unknown_fallbacks = set(fallbacks) - reviewed_ids
    if unknown_fallbacks:
        raise ValueError(f"fallback decisions were not reviewed: {sorted(unknown_fallbacks)}")

    pack_root = output / "ext_tex"
    pack_root.mkdir(parents=True)
    final_entries = []
    for entry in manifest["matches"]:
        final = {
            "id": entry["id"],
            "id_hex": entry["id_hex"],
            "match_basis": entry["match_basis"],
            "n64_dimensions": entry.get("n64_dimensions"),
            "xbla_original_dimensions": entry.get("xbla_original_dimensions"),
            "xbla_dimensions": entry.get("xbla_dimensions"),
            "review_reasons": entry.get("review_reasons", []),
        }
        if entry["status"] in ("excluded-empty-n64-slot", "unmatched"):
            final["status"] = entry["status"]
            final_entries.append(final)
            continue
        if entry["id_hex"] in fallbacks:
            final["status"] = "stock-fallback"
            final["decision"] = fallbacks[entry["id_hex"]]
            final_entries.append(final)
            continue
        if entry["status"] == "visual-review-required":
            final["status"] = "verified-visual"
            final["decision"] = "Accepted after sequential side-by-side visual review."
        else:
            final["status"] = "verified-direct"
        source = candidate_root / entry["pack_file"]
        destination = pack_root / f"{entry['id']:04x}.png"
        shutil.copyfile(source, destination)
        final["pack_file"] = f"ext_tex/{destination.name}"
        final["sha256"] = sha256(destination)
        final_entries.append(final)

    status_counts = dict(Counter(entry["status"] for entry in final_entries))
    release_manifest = {
        "format": "Perfect Dark external general texture pack",
        "source_candidate_manifest": str(candidate_manifest_path),
        "review_decisions": str(decisions_path),
        "policy": {
            "included": "visually verified XBLA diffuse replacements for stock N64 general texture slots",
            "excluded": "unrelated/reorganized candidates, empty stock slots, and XBLA-only material or asset slots",
            "fallback": "the port must retain its stock N64 texture when a PNG is absent",
        },
        "texture_count": sum("pack_file" in entry for entry in final_entries),
        "status_counts": status_counts,
        "entries": final_entries,
    }
    (output / "manifest.json").write_text(
        json.dumps(release_manifest, indent=2) + "\n", encoding="utf-8"
    )
    print(
        f"Final pack: {release_manifest['texture_count']} textures; "
        f"status={status_counts}; manifest={output / 'manifest.json'}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate-manifest", type=Path, required=True)
    parser.add_argument("--decisions", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return parser


if __name__ == "__main__":
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
