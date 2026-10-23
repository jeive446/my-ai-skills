"""Overlay checked transparent title PNGs after image processing."""
import argparse
import json
import math
import subprocess
from pathlib import Path

from PIL import Image


def overlay(video, titles, output):
    video, spec_path, output = map(lambda p: Path(p).resolve(), (video, titles, output))
    if output == video or output.exists():
        raise FileExistsError(f"Choose a new output: {output}")
    result = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=width,height,avg_frame_rate,duration", "-of", "json", str(video)],
                            capture_output=True, text=True, check=True)
    meta = json.loads(result.stdout)["streams"][0]
    duration = float(meta["duration"])
    items = json.loads(spec_path.read_text(encoding="utf-8-sig"))
    if not items:
        raise ValueError("Title list is empty")
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(video)]
    filters, previous = [], "0:v"
    for i, item in enumerate(items, 1):
        path = (spec_path.parent / item["image"]).resolve()
        start, end = float(item["start"]), float(item["end"])
        if not 0 <= start < end <= duration + 1e-5:
            raise ValueError(f"Invalid title time: {start}, {end}")
        width, x, y = int(item["width"]), int(item["x"]), int(item["y"])
        if width <= 0:
            raise ValueError("Title width must be positive")
        with Image.open(path) as image:
            if "A" not in image.getbands() or image.getchannel("A").getextrema()[0] == 255:
                raise ValueError(f"Title needs actual transparency: {path}")
        fade = min(max(0, float(item.get("fade", .15))), (end - start) / 2)
        command.extend(["-loop", "1", "-framerate", meta["avg_frame_rate"], "-i", str(path)])
        layer = f"[{i}:v]format=rgba,scale={width}:-2,trim=duration={end-start},setpts=PTS-STARTPTS"
        if fade:
            layer += f",fade=t=in:st=0:d={fade}:alpha=1,fade=t=out:st={end-start-fade}:d={fade}:alpha=1"
        layer += f",setpts=PTS+{start}/TB[t{i}]"
        filters.append(layer)
        filters.append(f"[{previous}][t{i}]overlay={x}:{y}:enable='between(t,{start},{end})':eof_action=pass:repeatlast=0[o{i}]")
        previous = f"o{i}"
    command.extend(["-filter_complex", ";".join(filters), "-map", f"[{previous}]", "-map", "0:a?", "-t", str(duration),
                    "-c:v", "libx264", "-preset", "medium", "-crf", "16", "-pix_fmt", "yuv420p",
                    "-c:a", "copy", "-movflags", "+faststart", str(output)])
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(output), "-f", "null", "-"], check=True)
    print(f"Title overlay complete: {output}")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--video", required=True)
    p.add_argument("--titles", required=True)
    p.add_argument("--output", required=True)
    a = p.parse_args()
    overlay(a.video, a.titles, a.output)
