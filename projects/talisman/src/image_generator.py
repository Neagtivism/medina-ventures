#!/usr/bin/env python3
"""fal.ai image generation client for Project Talisman.

Zero external dependencies (urllib only) to match queue_controller.py.
Requires FAL_KEY in the environment. FAL_MODEL_ID and FAL_LORA_URL are
optional overrides — verify the current model slug against fal.ai's docs
before relying on the default, endpoint identifiers change over time.

Usage:
    FAL_KEY=... python3 src/image_generator.py generate --prompt "..." --out data/images/test.png
    FAL_KEY=... python3 src/image_generator.py seed --count 20
    FAL_KEY=... python3 src/image_generator.py fill-queue --db data/talisman.db
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Optional, TypeVar

DEFAULT_MODEL_ID = "fal-ai/flux/dev"
DEFAULT_IMAGE_DIR = Path(__file__).resolve().parent.parent / "data" / "images"
METADATA_PATH = Path(__file__).resolve().parent.parent / "metadata.json"

RETRY_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 1.0
RETRY_MAX_DELAY_SECONDS = 20.0

T = TypeVar("T")


class FalAPIError(RuntimeError):
    pass


def _is_retryable_http_error(error: urllib.error.HTTPError) -> bool:
    return error.code == 429 or 500 <= error.code < 600


def _with_retry(func: Callable[[], T]) -> T:
    """Retry transient network failures (timeouts, connection errors, 429/5xx)
    with exponential backoff. Auth/validation errors (4xx other than 429) fail
    immediately — retrying those just burns quota for no benefit."""
    last_error: Optional[BaseException] = None
    for attempt in range(RETRY_ATTEMPTS):
        try:
            return func()
        except urllib.error.HTTPError as e:
            if not _is_retryable_http_error(e) or attempt == RETRY_ATTEMPTS - 1:
                raise
            last_error = e
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            if attempt == RETRY_ATTEMPTS - 1:
                raise
            last_error = e
        delay = min(RETRY_MAX_DELAY_SECONDS, RETRY_BASE_DELAY_SECONDS * (2**attempt))
        time.sleep(delay + random.uniform(0, 0.5))
    raise last_error  # pragma: no cover — loop always returns or raises above


class ImageGenerator:
    def __init__(
        self,
        api_key: Optional[str] = None,
        model_id: Optional[str] = None,
        lora_url: Optional[str] = None,
        timeout: int = 120,
    ):
        self.api_key = api_key or os.environ.get("FAL_KEY")
        if not self.api_key:
            raise FalAPIError("FAL_KEY is not set. Get one at https://fal.ai/dashboard/keys")
        self.model_id = model_id or os.environ.get("FAL_MODEL_ID", DEFAULT_MODEL_ID)
        self.lora_url = lora_url or os.environ.get("FAL_LORA_URL")
        self.timeout = timeout

    def _request(self, payload: dict) -> dict:
        url = f"https://fal.run/{self.model_id}"
        body = json.dumps(payload).encode("utf-8")

        def do_request() -> dict:
            req = urllib.request.Request(
                url,
                data=body,
                method="POST",
                headers={
                    "Authorization": f"Key {self.api_key}",
                    "Content-Type": "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))

        try:
            return _with_retry(do_request)
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            raise FalAPIError(f"fal.ai request failed ({e.code}): {detail}") from e
        except urllib.error.URLError as e:
            raise FalAPIError(f"fal.ai request failed (network error): {e}") from e

    def _download(self, url: str, out_path: Path) -> None:
        def do_download() -> bytes:
            with urllib.request.urlopen(url, timeout=self.timeout) as resp:
                return resp.read()

        try:
            data = _with_retry(do_download)
        except (urllib.error.URLError, urllib.error.HTTPError) as e:
            raise FalAPIError(f"image download failed: {e}") from e
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)

    def generate_image(self, prompt: str, out_path: Path) -> Path:
        payload: dict = {"prompt": prompt}
        if self.lora_url:
            payload["loras"] = [{"path": self.lora_url, "scale": 1.0}]
        result = self._request(payload)
        images = result.get("images") or []
        if not images:
            raise FalAPIError(f"No images returned: {result}")
        self._download(images[0]["url"], out_path)
        return out_path


def _load_prompt_anchors() -> list[str]:
    metadata = json.loads(METADATA_PATH.read_text())
    return metadata["visual_profile"]["image_prompt_anchors"]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Project Talisman image generator (fal.ai)")
    parser.add_argument("--model-id", default=None)
    parser.add_argument("--lora-url", default=None)
    sub = parser.add_subparsers(dest="command", required=True)

    p_gen = sub.add_parser("generate", help="Generate a single image")
    p_gen.add_argument("--prompt", required=True)
    p_gen.add_argument("--out", type=Path, required=True)

    p_seed = sub.add_parser(
        "seed", help="Generate a seed image set from metadata.json anchors (for LoRA training)"
    )
    p_seed.add_argument("--count", type=int, default=20)
    p_seed.add_argument("--out-dir", type=Path, default=DEFAULT_IMAGE_DIR / "seed")

    p_fill = sub.add_parser(
        "fill-queue", help="Generate images for every queued item that's missing one"
    )
    p_fill.add_argument("--db", type=Path, required=True)

    args = parser.parse_args()

    try:
        generator = ImageGenerator(model_id=args.model_id, lora_url=args.lora_url)
    except FalAPIError as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.command == "generate":
        path = generator.generate_image(args.prompt, args.out)
        print(f"saved {path}")

    elif args.command == "seed":
        anchors = _load_prompt_anchors()
        args.out_dir.mkdir(parents=True, exist_ok=True)
        for i in range(args.count):
            prompt = anchors[i % len(anchors)]
            out_path = args.out_dir / f"seed_{i:03d}_{uuid.uuid4().hex[:8]}.png"
            saved = generator.generate_image(prompt, out_path)
            print(f"[{i + 1}/{args.count}] saved {saved}")

    elif args.command == "fill-queue":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from queue_controller import QueueController

        controller = QueueController(args.db)
        pending = controller.list_pending_without_image()
        image_dir = args.db.parent / "images"
        failures = 0
        for item in pending:
            out_path = image_dir / f"{item.id}_{uuid.uuid4().hex[:8]}.png"
            try:
                saved = generator.generate_image(item.image_prompt, out_path)
            except FalAPIError as e:
                failures += 1
                print(f"content_id={item.id} FAILED (will retry next run): {e}", file=sys.stderr)
                continue
            controller.set_image_path(item.id, str(saved))
            print(f"content_id={item.id} -> {saved}")
        if failures:
            print(f"{failures}/{len(pending)} items failed after retries", file=sys.stderr)


if __name__ == "__main__":
    _cli()
