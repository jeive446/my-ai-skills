#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Batch Wan 2.2 MV shot generation with resumable local state."""

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from PIL import Image

if os.name == "nt":
    import msvcrt
else:
    import fcntl

from generate_wan22_video import (
    DEFAULT_DURATION,
    GENERATION_FPS,
    generation_frames,
    MAX_DURATION,
    MIN_DURATION,
    OUTPUT_DIR,
    TEMPLATE_PATH,
    api_json,
    validate_duration,
    get_history,
    output_path_from_history,
    prepare_prompt,
    queue_prompt,
    wait_for_completion,
)

DEFAULT_WIDTH = 720
DEFAULT_HEIGHT = 1280
DEFAULT_FPS = 30
PROJECT_DIR = Path(os.environ.get("WAN_PROJECT_DIR", str(OUTPUT_DIR / "local-2d-anime-mv")))
ID_RE = re.compile(r"^[^\\/:*?\"<>|]+$")


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temp.replace(path)


def safe_project_name(value):
    if not isinstance(value, str):
        raise ValueError("project/shot id must be text")
    if not value or value.endswith((".", " ")) or not ID_RE.fullmatch(value) or any(ord(c) < 32 for c in value):
        raise ValueError("project must be a non-empty path-safe name")
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", value.split('.')[0], re.I):
        raise ValueError("project/shot id cannot be a Windows device name")
    return value


def resolve_path(value, base_dir):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("asset path must be non-empty text")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def manifest_digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_dimensions(width, height):
    if type(width) is not int or type(height) is not int:
        raise ValueError("width and height must be integers")
    if width <= 0 or height <= 0 or width % 16 or height % 16:
        raise ValueError("width and height must be positive multiples of 16")
    return width, height


def validate_manifest(raw, manifest_path, check_files=True):
    if not isinstance(raw, dict):
        raise ValueError("manifest root must be an object")
    base_dir = manifest_path.parent
    project = safe_project_name(raw.get("project", ""))
    audio_value = raw.get("audio")
    if not audio_value:
        raise ValueError("manifest requires an audio path")
    audio = resolve_path(audio_value, base_dir)
    lrc = resolve_path(raw["lrc"], base_dir) if raw.get("lrc") else None
    if check_files and not audio.is_file():
        raise FileNotFoundError(f"audio not found: {audio}")
    if check_files and lrc and not lrc.is_file():
        raise FileNotFoundError(f"lrc not found: {lrc}")

    width, height = validate_dimensions(raw.get("width", DEFAULT_WIDTH), raw.get("height", DEFAULT_HEIGHT))
    fps = raw.get("fps", DEFAULT_FPS)
    if fps != 30:
        raise ValueError("Base composition must use 30fps. Create a separately verified interpolated branch for 60fps.")
    shots = raw.get("shots")
    if not isinstance(shots, list) or not shots:
        raise ValueError("manifest requires a non-empty shots array")

    normalized_shots = []
    ids = set()
    for index, shot in enumerate(shots, 1):
        if not isinstance(shot, dict):
            raise ValueError(f"shot {index} must be an object")
        shot_id = safe_project_name(shot.get("id", ""))
        if shot_id.casefold() in ids:
            raise ValueError(f"shot {index} has an invalid or duplicate id")
        ids.add(shot_id.casefold())
        if not shot.get("image"):
            raise ValueError(f"shot {shot_id} requires an image path")
        image = resolve_path(shot["image"], base_dir)
        if check_files and not image.is_file():
            raise FileNotFoundError(f"image not found for {shot_id}: {image}")
        if check_files:
            with Image.open(image) as img:
                img.verify()
        prompt = shot.get("prompt", "")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"shot {shot_id} requires a prompt")
        if "duration" not in shot:
            raise ValueError(f"shot {shot_id} requires duration")
        duration = validate_duration(shot["duration"])
        edit_duration = float(shot.get("edit_duration", duration))
        if not math.isfinite(edit_duration) or not 0 < edit_duration <= duration:
            raise ValueError(f"shot {shot_id} edit_duration must be positive and no longer than duration")
        seed = shot.get("seed")
        if seed is not None:
            if type(seed) is not int or not 0 < seed < 2**64:
                raise ValueError(f"shot {shot_id} seed must be an integer in 1..2^64-1")
        normalized_shots.append({"id": shot_id, "image": str(image), "prompt": prompt, "duration": duration, "edit_duration": edit_duration, "seed": seed})

    return {
        "project": project,
        "audio": str(audio),
        "lrc": str(lrc) if lrc else None,
        "width": width,
        "height": height,
        "fps": fps,
        "shots": normalized_shots,
    }


def load_manifest(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8-sig"))
    return path, validate_manifest(raw, path), manifest_digest(path)


def project_paths(project):
    folder = PROJECT_DIR / safe_project_name(project)
    return folder, folder / "state.json"


def state_for_manifest(manifest, manifest_path, digest):
    shots = {}
    for shot in manifest["shots"]:
        shots[shot["id"]] = {
            "id": shot["id"],
            "image": shot["image"],
            "prompt": shot["prompt"],
            "duration": shot["duration"],
            "edit_duration": shot.get("edit_duration", shot["duration"]),
            "seed": shot["seed"],
            "image_sha256": manifest_digest(Path(shot["image"])),
            "prefix": f"video/mv/{manifest['project']}/{shot['id']}",
            "status": "pending",
            "output": None,
            "prompt_id": None,
            "error": None,
            "updated_at": now(),
        }
    return {
        "project": manifest["project"],
        "manifest": str(manifest_path),
        "manifest_sha256": digest,
        "template_sha256": manifest_digest(TEMPLATE_PATH),
        "audio": manifest["audio"],
        "lrc": manifest["lrc"],
        "width": manifest["width"],
        "height": manifest["height"],
        "fps": manifest["fps"],
        "created_at": now(),
        "updated_at": now(),
        "shots": shots,
        "failures": [],
    }


def load_or_create_state(manifest, manifest_path, digest, resume):
    folder, state_path = project_paths(manifest["project"])
    if state_path.is_file():
        if not resume:
            raise ValueError(f"Project already exists; use --resume or a new project name: {state_path}")
        state = json.loads(state_path.read_text(encoding="utf-8"))
        validate_state(manifest, digest, state)
        return folder, state_path, state
    return folder, state_path, state_for_manifest(manifest, manifest_path, digest)


def validate_state(manifest, digest, state):
    if state.get("project") != manifest["project"] or state.get("manifest_sha256") != digest:
        raise ValueError("state belongs to a different manifest; use a new project name")
    if state.get("template_sha256") != manifest_digest(TEMPLATE_PATH):
        raise ValueError("Workflow template changed since generation; use a new project name")
    for shot in manifest["shots"]:
        entry = state.get("shots", {}).get(shot["id"], {})
        if entry.get("image_sha256") != manifest_digest(Path(shot["image"])):
            raise ValueError(f"Source image changed for {shot['id']}; use a new project name")


def expected_video_duration(duration):
    return generation_frames(duration) / GENERATION_FPS


def output_is_valid(path, expected_duration, width=None, height=None):
    if not path or not Path(path).is_file():
        return False
    try:
        result = shutil.which("ffprobe")
        if not result:
            return False
        probe = subprocess.run([result, "-v", "error", "-select_streams", "V:0", "-show_entries", "stream=duration,width,height,avg_frame_rate:format=duration", "-of", "json", str(path)], capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, timeout=30)
        metadata = json.loads(probe.stdout)
        streams = metadata.get("streams", [])
        if not streams:
            return False
        video = streams[0]
        actual = float(video.get("duration") or metadata.get("format", {}).get("duration", 0))
        numerator, denominator = video.get("avg_frame_rate", "0/1").split("/")
        fps = float(numerator) / float(denominator)
    except (OSError, ValueError, ZeroDivisionError, subprocess.SubprocessError):
        return False
    return (math.isfinite(actual) and abs(actual - expected_video_duration(expected_duration)) < 0.02
            and abs(fps - GENERATION_FPS) < 0.01
            and (width is None or video["width"] == width)
            and (height is None or video["height"] == height))


@contextmanager
def project_lock(folder):
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "project.lock").open("a+b") as lock:
        lock.seek(0, 2)
        if lock.tell() == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Project is already in use: {folder}") from exc
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock, fcntl.LOCK_UN)


def save_state(state_path, state):
    state["updated_at"] = now()
    atomic_json_write(state_path, state)


def run_manifest(manifest_path, resume=False, dry_run=False):
    manifest_path, manifest, digest = load_manifest(manifest_path)
    from canvas_runtime.submission import bind_project
    bind_project(manifest_path.parent)
    if dry_run:
        print(f"Project: {manifest['project']}")
        print(f"Audio: {manifest['audio']}")
        print(f"Resolution: {manifest['width']}x{manifest['height']} @ {GENERATION_FPS} generation FPS; {manifest['fps']} composition FPS")
        print(f"Shots: {len(manifest['shots'])}, planned duration: {sum(s['duration'] for s in manifest['shots']):.3f}s")
        for shot in manifest["shots"]:
            print(f"  {shot['id']}: {shot['duration']:g}s | {shot['image']}")
        return 0

    if not shutil.which("ffprobe"):
        raise FileNotFoundError("ffprobe is required on PATH before generation")
    folder, _ = project_paths(manifest["project"])
    with project_lock(folder):
        _, state_path, state = load_or_create_state(manifest, manifest_path, digest, resume)
        return generate_shots(manifest, state_path, state, resume)


def generate_shots(manifest, state_path, state, resume):

    save_state(state_path, state)
    failures = []
    for shot in manifest["shots"]:
        entry = state["shots"][shot["id"]]
        old_output = entry.get("output")
        if resume and entry.get("status") == "completed" and output_is_valid(old_output, shot["duration"], manifest["width"], manifest["height"]):
            entry["status"] = "completed"
            entry["error"] = None
            entry["updated_at"] = now()
            print(f"[skip] {shot['id']}: {old_output}")
            save_state(state_path, state)
            continue

        try:
            result = None
            active = False
            if resume and entry.get("prompt_id"):
                history = get_history(entry["prompt_id"])
                previous = history.get(entry["prompt_id"])
                if previous and previous.get("status", {}).get("status_str") != "error":
                    candidate = output_path_from_history(previous)
                    if output_is_valid(candidate, shot["duration"], manifest["width"], manifest["height"]):
                        result = previous
                if result is None:
                    queue = api_json("queue")
                    active = any(item[1] == entry["prompt_id"] for item in queue.get("queue_running", []) + queue.get("queue_pending", []))
                    if previous is None and not active:
                        raise RuntimeError("Saved task ID is absent from both history and queue. Inspect service restart or a lost response before explicitly starting a new revision.")
            if active:
                entry["status"] = "running"
                save_state(state_path, state)
                print(f"[resume] waiting for existing task {entry['prompt_id']}")
                result = wait_for_completion(entry["prompt_id"])
            elif result is None:
                queue = api_json("queue")
                if queue.get("queue_running") or queue.get("queue_pending"):
                    raise RuntimeError("ComfyUI queue is occupied; wait before submitting this single-GPU project")
                prefix = f"video/mv/{manifest['project']}/{shot['id']}"
                seed = entry.get("settings", {}).get("seed", shot["seed"] or 0)
                prompt_data, settings = prepare_prompt(shot["image"], shot["prompt"], manifest["width"], manifest["height"], seed, prefix, shot["duration"])
                entry.update({"status": "submitting", "error": None, "output": None, "settings": settings, "prompt_id": str(uuid.uuid4()), "updated_at": now()})
                save_state(state_path, state)
                response = queue_prompt(prompt_data, entry["prompt_id"])
                if response["prompt_id"] != entry["prompt_id"]:
                    raise RuntimeError("ComfyUI did not preserve the saved task ID; stop and inspect the queue")
                entry["status"] = "running"
                save_state(state_path, state)
                print(f"[queue] {shot['id']}: {entry['prompt_id']}")
                result = wait_for_completion(entry["prompt_id"])

            output = output_path_from_history(result)
            if output is None or not output_is_valid(output, shot["duration"], manifest["width"], manifest["height"]):
                raise RuntimeError(f"ComfyUI completed without a valid output for {shot['id']}")
            entry.update({"status": "completed", "output": str(output), "error": None, "updated_at": now()})
            latent_items = result.get("outputs", {}).get("save_latent", {}).get("latents", [])
            if latent_items:
                info = latent_items[0]
                entry["latent"] = str(OUTPUT_DIR / info.get("subfolder", "") / info["filename"])
            print(f"[done] {shot['id']}: {output}")
        except (OSError, ValueError, RuntimeError, KeyError) as exc:
            entry.update({"status": "needs_review", "error": str(exc), "updated_at": now()})
            failures.append(shot["id"])
            print(f"[failed] {shot['id']}: {exc}", file=sys.stderr)
            save_state(state_path, state)
            break
        save_state(state_path, state)

    state["failures"] = failures
    save_state(state_path, state)
    print(f"State file: {state_path}")
    if failures:
        print("Failed shots: " + ", ".join(failures), file=sys.stderr)
        return 1
    return 0


def main():
    parser = argparse.ArgumentParser(description="Batch Wan 2.2 MV shot generation")
    parser.add_argument("--manifest", required=True, help="Path to MV JSON manifest")
    parser.add_argument("--resume", action="store_true", help="Reuse valid outputs and recover in-flight tasks")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the plan without queueing ComfyUI jobs")
    args = parser.parse_args()
    try:
        raise SystemExit(run_manifest(args.manifest, args.resume, args.dry_run))
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
