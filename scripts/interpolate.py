"""Optional RIFE 30-to-60fps branch for one silent, uncut source shot."""
import argparse
import json
import math
import subprocess
import uuid
from fractions import Fraction
from pathlib import Path

import generate_wan22_video as wan
from generate_wan22_mv import project_lock, atomic_json_write


def interpolate(video, workdir, dry_run=False):
    video, workdir = Path(video).resolve(), Path(workdir).resolve()
    from canvas_runtime.submission import bind_project
    bind_project(workdir)
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "stream=codec_type,avg_frame_rate,nb_frames", "-of", "json", str(video)],
                            check=True, capture_output=True, text=True)
    streams = json.loads(result.stdout)["streams"]
    stream = next(s for s in streams if s["codec_type"] == "video")
    if any(s["codec_type"] == "audio" for s in streams):
        raise ValueError("Use an individual silent shot; add the master music at final composition")
    if abs(float(Fraction(stream["avg_frame_rate"])) - 30) > .001:
        raise ValueError("This optional branch expects a 30fps source shot")
    prefix = f"video/local-2d-anime-mv-rife/{workdir.name}"
    prompt = {
        "1": {"class_type": "VHS_LoadVideoPath", "inputs": {
            "video": str(video), "force_rate": 0, "custom_width": 0, "custom_height": 0,
            "frame_load_cap": 0, "skip_first_frames": 0, "select_every_nth": 1, "format": "None"}},
        "2": {"class_type": "RIFE VFI", "inputs": {
            "ckpt_name": "rife49.pth", "frames": ["1", 0], "clear_cache_after_n_frames": 10,
            "multiplier": 2, "fast_mode": False, "ensemble": True, "scale_factor": 1.0,
            "dtype": "float32", "torch_compile": False, "batch_size": 1}},
        "3": {"class_type": "CreateVideo", "inputs": {"images": ["2", 0], "fps": 60.0,
            "bit_depth": 8, "color_space": "sRGB", "codec": "none"}},
        "108": {"class_type": "SaveVideo", "inputs": {"video": ["3", 0], "filename_prefix": prefix,
            "format": "mp4", "format.codec": "h264", "format.codec.encoding": "re-encode",
            "format.codec.encoding.crf": 16.0, "codec": "auto"}},
    }
    workdir.mkdir(parents=True, exist_ok=True)
    atomic_json_write(workdir / "rife.api.json", prompt)
    if dry_run:
        print(f"Prepared 30-to-60fps RIFE graph; GPU jobs submitted: 0. {workdir}")
        return
    with project_lock(workdir):
        state_file = workdir / "rife.state.json"
        if state_file.exists():
            state = json.loads(state_file.read_text(encoding="utf-8"))
            if state.get("source") != str(video) or state.get("source_mtime_ns") != video.stat().st_mtime_ns:
                raise ValueError("Source changed; use a new workdir revision")
            if state.get("output") and Path(state["output"]).is_file():
                print(state["output"])
                return
            task = state["prompt_id"]
            history = wan.get_history(task)
            queue = wan.api_json("queue")
            active = any(item[1] == task for item in queue.get("queue_running", []) + queue.get("queue_pending", []))
            if task not in history and not active:
                raise RuntimeError("Saved RIFE task absent from queue/history; inspect before new submission")
        else:
            queue = wan.api_json("queue")
            if queue.get("queue_running") or queue.get("queue_pending"):
                raise RuntimeError("GPU queue is occupied")
            task = str(uuid.uuid4())
            state = {"source": str(video), "source_mtime_ns": video.stat().st_mtime_ns, "prompt_id": task}
            atomic_json_write(state_file, state)
            wan.queue_prompt(prompt, task)
        history = wan.wait_for_completion(task)
        output = wan.output_path_from_history(history)
        if output is None:
            raise RuntimeError("Missing RIFE video output")
        subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], check=True)
        state["output"] = str(output)
        state["duration_note"] = "RIFE adds frames between endpoints: 2N-1 frames at 60fps. Keep endpoint/timeline handling explicit at edit."
        atomic_json_write(state_file, state)
        print(output)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", required=True)
    p.add_argument("--workdir", required=True)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    interpolate(a.video, a.workdir, a.dry_run)
