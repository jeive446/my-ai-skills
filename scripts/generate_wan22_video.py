#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
Wan 2.2 (14B) I2V Automated Video Generator for RTX 5070 (12GB) + ComfyUI
Interfaces with local ComfyUI API (http://127.0.0.1:8188)
"""

import argparse
import json
import hashlib
import shutil
import sys
import time
import urllib.request
import urllib.error
import random
import math
import os
from pathlib import Path
from PIL import Image

ROOT = Path(os.environ.get("WAN_COMFY_ROOT", "D:/AI_Video/ComfyUI"))
INPUT_DIR = ROOT / "input"
OUTPUT_DIR = ROOT / "output"
TEMPLATE_PATH = Path(__file__).resolve().parents[1] / "assets/wan22_720p30_api.json"
SERVER_URL = os.environ.get("WAN_COMFY_URL", "http://127.0.0.1:8188").rstrip("/")
GENERATION_FPS = 30
DEFAULT_DURATION = 2.0
MIN_DURATION = 1.0
MAX_DURATION = 5.0


def validate_duration(duration):
    if isinstance(duration, bool):
        raise ValueError("duration must be a number")
    try:
        duration = float(duration)
    except (TypeError, ValueError) as exc:
        raise ValueError("duration must be a number") from exc
    if not math.isfinite(duration) or not MIN_DURATION <= duration <= MAX_DURATION:
        raise ValueError(f"duration must be between {MIN_DURATION:g} and {MAX_DURATION:g} seconds")
    return duration


def generation_frames(duration):
    return 4 * math.ceil(validate_duration(duration) * GENERATION_FPS / 4) + 1


def get_optimal_resolution(image_path: Path, max_area=1280 * 720):
    """
    Fit the source aspect ratio within a 720p pixel budget, aligned to 16.
    """
    try:
        with Image.open(image_path) as img:
            w, h = img.size
    except (OSError, ValueError):
        w, h = 16, 9

    scale = math.sqrt(max_area / (w * h))
    target_w = max(16, int(w * scale) // 16 * 16)
    target_h = max(16, int(h * scale) // 16 * 16)

    return target_w, target_h


def api_json(endpoint, payload=None):
    req = urllib.request.Request(
        f"{SERVER_URL}/{endpoint}",
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"ComfyUI HTTP {exc.code}: {detail}") from exc


def queue_prompt(prompt_data, prompt_id=None):
    if os.environ.get('MV_CANVAS_MODE','visible') != 'headless':
        from canvas_runtime.submission import queue
        return queue(prompt_data,api_json,prompt_id)
    payload = {"prompt": prompt_data}
    if prompt_id:
        payload["prompt_id"] = prompt_id
    result = api_json("prompt", payload)
    if result.get("error") or not result.get("prompt_id"):
        raise RuntimeError(f"ComfyUI rejected prompt: {result}")
    return result


def get_history(prompt_id):
    return api_json(f"history/{prompt_id}")


def wait_for_completion(prompt_id, poll_interval=5.0, timeout=7200):
    start_time = time.time()
    print(f"[*] Task queued. Prompt ID: {prompt_id}")
    print(f"[*] Waiting for Wan 2.2 14B sampling to complete...", flush=True)

    dots = 0
    while time.time() - start_time < timeout:
        try:
            data = get_history(prompt_id)
            if prompt_id in data:
                item = data[prompt_id]
                status = item.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI execution error: {status.get('messages')}")
                if status.get("completed", False):
                    elapsed = time.time() - start_time
                    print(f"\n[+] Render completed in {elapsed:.1f}s!")
                    return item
        except urllib.error.URLError:
            pass

        time.sleep(poll_interval)
        dots = (dots + 1) % 10
        print(".", end="" if dots > 0 else "\n[*] Still rendering...", flush=True)

    raise TimeoutError(f"Generation timed out after {timeout} seconds.")


def resolve_image(image):
    input_img_path = Path(image).expanduser().resolve()
    if not input_img_path.is_file():
        fallback_path = INPUT_DIR / image
        if fallback_path.is_file():
            input_img_path = fallback_path.resolve()
        else:
            raise FileNotFoundError(f"Image not found: {input_img_path}")

    with Image.open(input_img_path) as img:
        img.verify()
    if input_img_path.is_relative_to(INPUT_DIR.resolve()):
        return input_img_path, input_img_path.relative_to(INPUT_DIR.resolve()).as_posix()
    digest = hashlib.sha256(input_img_path.read_bytes()).hexdigest()[:16]
    dest_path = INPUT_DIR / "wan22_mv" / f"{digest}_{input_img_path.name}"
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    if not dest_path.exists():
        shutil.copy2(input_img_path, dest_path)
    return dest_path, dest_path.relative_to(INPUT_DIR).as_posix()


def load_template():
    if not TEMPLATE_PATH.is_file():
        raise FileNotFoundError(f"Template missing: {TEMPLATE_PATH}")
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def prepare_prompt(image, prompt, width=0, height=0, seed=0, prefix="video/Wan2.2_5070_Codex", duration=DEFAULT_DURATION):
    duration = validate_duration(duration)
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("prompt must be non-empty text")
    dest_path, image_filename = resolve_image(image)
    if width > 0 and height > 0:
        w, h = int(width), int(height)
    else:
        w, h = get_optimal_resolution(dest_path)
    w = (w // 16) * 16
    h = (h // 16) * 16
    if w <= 0 or h <= 0:
        raise ValueError("width and height must be positive")

    seed = int(seed) if int(seed) > 0 else random.randint(10000000, 9999999999)
    prompt_data = load_template()
    for node in ("97", "129:93", "129:98", "129:161", "129:162", "129:86", "108"):
        if node not in prompt_data:
            raise ValueError(f"Template is missing node {node}")
    prompt_data["129:162"]["inputs"]["value"] = GENERATION_FPS
    frame_count = generation_frames(duration)
    prompt_data["129:98"]["inputs"]["length"] = frame_count
    # Keep spatial tiles; decode the entire shot in one temporal window.
    prompt_data["129:87"]["inputs"].update(temporal_size=max(64, 4 * math.ceil(frame_count / 4)), temporal_overlap=16)
    prompt_data["save_latent"] = {"class_type": "SaveLatent", "inputs": {
        "samples": ["129:85", 0], "filename_prefix": prefix + "_latent"}}
    if "97" in prompt_data:
        prompt_data["97"]["inputs"]["image"] = image_filename
    if "129:93" in prompt_data:
        prompt_data["129:93"]["inputs"]["text"] = prompt
    if "129:98" in prompt_data:
        prompt_data["129:98"]["inputs"]["width"] = w
        prompt_data["129:98"]["inputs"]["height"] = h
    if "129:161" not in prompt_data:
        raise KeyError("Template is missing duration node 129:161")
    prompt_data["129:161"]["inputs"]["value"] = duration
    if "129:86" in prompt_data:
        prompt_data["129:86"]["inputs"]["noise_seed"] = seed
    if "108" in prompt_data:
        prompt_data["108"]["inputs"]["filename_prefix"] = prefix
    return prompt_data, {"image": str(dest_path), "image_filename": image_filename, "width": w, "height": h, "seed": seed, "duration": duration, "prefix": prefix, "generation_fps": GENERATION_FPS, "frames": frame_count, "temporal_size": prompt_data["129:87"]["inputs"]["temporal_size"]}


def output_path_from_history(result):
    outputs = result.get("outputs", {})
    video_node = outputs.get("108", {})
    items = video_node.get("images", []) or video_node.get("gifs", []) or video_node.get("videos", [])
    if not items:
        return None
    info = items[0]
    filename = info.get("filename")
    if not filename:
        return None
    path = (OUTPUT_DIR / info.get("subfolder", "") / filename).resolve()
    if not path.is_relative_to(OUTPUT_DIR.resolve()) or path.suffix.lower() != ".mp4":
        raise RuntimeError(f"Invalid MP4 output path returned by ComfyUI: {path}")
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"MP4 output missing or empty: {path}")
    return path


def generate_video(image, prompt, width=0, height=0, seed=0, prefix="video/Wan2.2_5070_Codex", duration=DEFAULT_DURATION):
    prompt_data, settings = prepare_prompt(image, prompt, width, height, seed, prefix, duration)
    print("==================================================")
    print("🎬 Wan 2.2 (14B) I2V Video Generation (RTX 5070)")
    print("==================================================")
    print(f"  Input Image : {settings['image_filename']}")
    print(f"  Resolution  : {settings['width']} x {settings['height']}")
    print(f"  Duration    : {settings['duration']:g}s")
    print(f"  Seed        : {settings['seed']}")
    print(f"  Prompt      : {prompt}")
    print("==================================================")
    try:
        res = queue_prompt(prompt_data)
        prompt_id = res["prompt_id"]
    except (OSError, ValueError, RuntimeError) as exc:
        raise RuntimeError(f"Failed to queue prompt on {SERVER_URL}: {exc}") from exc
    result = wait_for_completion(prompt_id)
    path = output_path_from_history(result)
    if path is None:
        raise RuntimeError("Video rendered, but output node did not return filename")
    print("\n🎉 Video generated successfully!")
    print(f"📁 Output file path: {path}")
    return path, prompt_id, settings


def main():
    parser = argparse.ArgumentParser(description="Generate Wan 2.2 I2V Video via ComfyUI API")
    parser.add_argument("--image", required=True, help="Path to input image file")
    parser.add_argument("--prompt", required=True, help="Motion prompt text")
    parser.add_argument("--width", type=int, default=0, help="Video width (multiple of 16; default auto, 720p pixel budget)")
    parser.add_argument("--height", type=int, default=0, help="Video height (multiple of 16; default auto, 720p pixel budget)")
    parser.add_argument("--seed", type=int, default=0, help="Random seed (0 for random)")
    parser.add_argument("--prefix", default="video/Wan2.2_5070_Codex", help="Output filename prefix")
    parser.add_argument("--duration", type=float, default=DEFAULT_DURATION, help="Video duration in seconds (1-5, default 2)")
    args = parser.parse_args()
    try:
        generate_video(args.image, args.prompt, args.width, args.height, args.seed, args.prefix, args.duration)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
