"""Local MV planning, generation, review and composition entry point."""
import argparse
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import av
import numpy as np
from PIL import Image

import generate_wan22_mv as batch
import generate_wan22_video as wan
import compose_wan22_mv as compositor

SKILL = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write(path, value):
    batch.atomic_json_write(Path(path), value)


def bind(manifest):
    path = Path(manifest).resolve()
    batch.PROJECT_DIR = path.parent / "render"
    from canvas_runtime.submission import bind_project
    bind_project(path.parent)
    return path


def environment():
    template = read(wan.TEMPLATE_PATH)
    folders = {"unet_name": "diffusion_models", "lora_name": "loras", "clip_name": "text_encoders", "vae_name": "vae"}
    models = []
    for node in template.values():
        for key, folder in folders.items():
            if key in node.get("inputs", {}):
                path = wan.ROOT / "models" / folder / node["inputs"][key]
                models.append({"name": path.name, "exists": path.is_file()})
    result = {"comfy_root": str(wan.ROOT), "template": str(wan.TEMPLATE_PATH), "models": models,
              "ffmpeg": shutil.which("ffmpeg"), "ffprobe": shutil.which("ffprobe"), "source_fps": 30}
    try:
        info = wan.api_json("object_info")
        required = {n["class_type"] for n in template.values()} | {"SaveLatent"}
        result["missing_nodes"] = sorted(required - info.keys())
        result["queue"] = {k: len(v) for k, v in wan.api_json("queue").items()}
    except OSError as exc:
        result["server"] = str(exc)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def gui_from_prompt(prompt, info):
    """Build a real flat LiteGraph workflow from a saved API prompt."""
    # Widget order/dynamic fields must be serialized by the actual installed frontend.
    from canvas_runtime.client import call
    from canvas_runtime.runtime_setup import ensure_running
    ensure_running()
    call('select_project',project_dir=os.environ['MV_PROJECT_ROOT'])
    return call('prepare',prompt=prompt,name='Plan_'+hashlib.sha256(json.dumps(prompt,sort_keys=True).encode()).hexdigest()[:10])['workflow']


def plan(path, canvas=False):
    path, manifest, _ = batch.load_manifest(path)
    raw = read(path)
    source_shots = {s["id"]: s for s in raw["shots"]}
    folder = path.parent / "workflows"
    folder.mkdir(exist_ok=True)
    info = wan.api_json("object_info") if canvas else None
    overview = []
    for shot in manifest["shots"]:
        prompt, settings = wan.prepare_prompt(shot["image"], shot["prompt"], manifest["width"], manifest["height"],
                                             shot["seed"] or 1, f"video/mv/{manifest['project']}/{shot['id']}", shot["duration"])
        write(folder / f"{shot['id']}.api.json", prompt)
        if info:
            write(folder / f"{shot['id']}.workflow.json", gui_from_prompt(prompt, info))
        overview.append({"id": shot["id"], "scene": source_shots[shot["id"]].get("scene"), "settings": settings, "edit_duration": shot["edit_duration"], "image": shot["image"]})
    write(path.parent / "plan.json", {"project": manifest["project"], "shots": overview, "generation_fps": 30})
    if info:
        preview = {}
        state_path = batch.project_paths(manifest["project"])[1]
        state = read(state_path) if state_path.exists() else {"shots": {}}
        for index, shot in enumerate(manifest["shots"]):
            key = str(index * 2 + 1)
            saved = state.get("shots", {}).get(shot["id"], {})
            if saved.get("output") and Path(saved["output"]).is_file():
                preview[key] = {"class_type": "VHS_LoadVideoPath", "inputs": {
                    "video": saved["output"], "force_rate": 0, "custom_width": 0, "custom_height": 0,
                    "frame_load_cap": 0, "skip_first_frames": 0, "select_every_nth": 1, "format": "None"}}
                preview[str(index * 2 + 2)] = {"class_type": "PreviewImage", "inputs": {"images": [key, 0]}}
            else:
                _, image_name = wan.resolve_image(shot["image"])
                preview[key] = {"class_type": "LoadImage", "inputs": {"image": image_name}, "_meta": {"title": shot["id"] + " first frame"}}
                preview[str(index * 2 + 2)] = {"class_type": "PreviewImage", "inputs": {"images": [key, 0]}}
        overview_graph = gui_from_prompt(preview, info)
        grouped = {}
        for index, shot in enumerate(manifest["shots"]):
            scene = source_shots[shot["id"]].get("scene", "scene")
            grouped.setdefault(scene, []).extend(overview_graph["nodes"][index * 2:index * 2 + 2])
        group_y = 0
        for scene, group_nodes in grouped.items():
            for index, node in enumerate(group_nodes):
                node["pos"] = [(index % 4) * 380, group_y + (index // 4) * 430]
            height = math.ceil(len(group_nodes) / 4) * 430
            overview_graph["groups"].append({"title": scene, "bounding": [-20, group_y - 50, 1540, height + 50], "color": "#445b70", "font_size": 24})
            group_y += height + 130
        write(folder / "overview.workflow.json", overview_graph)
    print(f"Prepared {len(overview)} shot API graphs; GPU jobs submitted: 0. Files: {folder}")


def image_job(spec_path, dry_run=False):
    spec_path = Path(spec_path).resolve()
    spec = read(spec_path)
    destination = (spec_path.parent / spec["output"]).resolve()
    references = [(spec_path.parent / p).resolve() for p in spec.get("references", [])]
    provider = Path(os.environ.get("MV_IMAGE_GEN_DIR", str(Path.home() / ".codex/skills/image-gen")))
    prompt = spec["prompt"].replace("\r", " ").replace("\n", " ").strip()
    if not prompt:
        raise ValueError("Image prompt is empty")
    if destination.exists():
        with Image.open(destination) as im:
            im.verify()
        print(f"Existing image preserved: {destination}")
        return
    if references:
        for ref in references:
            if not ref.is_file():
                raise FileNotFoundError(ref)
    if len(references) > 1:
        args = [str(provider / "generate_multi_ref.py"), prompt, *map(str, references)]
    elif references:
        args = [str(provider / "generate_with_ref.py"), str(references[0]), prompt]
    else:
        args = [str(provider / "generate.py"), prompt]
    if not Path(args[0]).is_file():
        raise FileNotFoundError(args[0])
    if dry_run:
        print(json.dumps({"provider_script": args[0], "reference_count": len(references), "output": str(destination), "network_requests": 0}, ensure_ascii=False))
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="imagegen-", dir=destination.parent) as tmp:
        result = subprocess.run([sys.executable, *args], cwd=tmp, capture_output=True, timeout=6600)
        # Provider responses may contain sensitive request details; do not copy stdout into the package or logs.
        images = list(Path(tmp).glob("gen*.png"))
        if result.returncode or len(images) != 1:
            raise RuntimeError(f"Image provider failed (exit {result.returncode}); no automatic resubmission. Inspect privately before retrying.")
        with Image.open(images[0]) as im:
            im.verify()
        shutil.copy2(images[0], destination)
    write(destination.with_suffix(".image.json"), {"prompt": prompt, "references": list(map(str, references)), "output": str(destination),
                                                  "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()})
    print(f"Image saved: {destination}")


def qa(path, output):
    path, output = Path(path).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    curve, indices, previous, max_pair, max_delta, count = [], [], None, None, -1, 0
    with av.open(str(path)) as video:
        fps = float(video.streams.video[0].average_rate)
        for frame in video.decode(video=0):
            rgb = frame.to_ndarray(format="rgb24")
            y = rgb[..., 0].mean() * .2126 + rgb[..., 1].mean() * .7152 + rgb[..., 2].mean() * .0722
            curve.append(float(y))
            if previous is not None and abs(y - curve[-2]) > max_delta:
                max_delta, max_pair = abs(y - curve[-2]), (previous, rgb.copy(), count)
            previous = rgb.copy()
            if count % max(1, round(fps)) == 0:
                Image.fromarray(rgb).save(output / f"frame_{count:05}.jpg", quality=94)
            count += 1
    if not count:
        raise ValueError("Video has no frames")
    if max_pair:
        Image.fromarray(max_pair[0]).save(output / "largest_jump_before.png")
        Image.fromarray(max_pair[1]).save(output / "largest_jump_after.png")
    report = {"video": str(path), "decoded_frames": count, "fps": fps, "mean_luma": curve,
              "largest_adjacent_mean_luma_jump": float(max_delta), "largest_jump_frame": max_pair[2] if max_pair else None,
              "limitation": "Luma is a diagnostic clue, not a perceptual verdict. Review actual light effects, motion, background regions and playback."}
    write(output / "quality.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "mean_luma"}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("environment")
    canvas=sub.add_parser('canvas',help='Install/start reusable live canvas and select this MV project (no GPU work)')
    canvas.add_argument('--project-dir',required=True)
    canvas.add_argument('--project-id')
    for name in ("plan", "generate", "compose"):
        s = sub.add_parser(name)
        s.add_argument("--manifest", required=True)
        if name == "plan":
            s.add_argument("--canvas", action="store_true")
        if name == "generate":
            s.add_argument("--resume", action="store_true")
            s.add_argument("--dry-run", action="store_true")
        if name == "compose":
            s.add_argument("--output", required=True)
            s.add_argument("--width", type=int)
            s.add_argument("--height", type=int)
            s.add_argument("--fit", choices=["cover", "contain"], default="cover")
    s = sub.add_parser("image")
    s.add_argument("--spec", required=True)
    s.add_argument("--dry-run", action="store_true")
    s = sub.add_parser("qa")
    s.add_argument("--video", required=True)
    s.add_argument("--output-dir", required=True)
    a = p.parse_args()
    if a.command == "environment":
        environment()
    elif a.command=='canvas':
        from canvas_runtime.runtime_setup import setup
        print(json.dumps(setup(a.project_dir,a.project_id),ensure_ascii=False,indent=2))
    elif a.command == "image":
        image_job(a.spec, a.dry_run)
    elif a.command == "qa":
        qa(a.video, a.output_dir)
    else:
        path = bind(a.manifest)
        if a.command == "plan":
            plan(path, a.canvas)
        elif a.command == "generate":
            raise SystemExit(batch.run_manifest(path, a.resume, a.dry_run))
        elif a.command == "compose":
            compositor.compose(path, a.output, a.width, a.height, fps=30, fit=a.fit)


if __name__ == "__main__":
    main()
