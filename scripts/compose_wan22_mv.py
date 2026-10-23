#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Normalize and compose generated Wan MV shots with audio and optional LRC."""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from fractions import Fraction
from fontTools.ttLib import TTFont, TTLibError

from generate_wan22_mv import (DEFAULT_FPS, DEFAULT_HEIGHT, DEFAULT_WIDTH, atomic_json_write,
                               load_manifest, project_paths, project_lock, validate_state, output_is_valid)

LRC_LINE = re.compile(r"^\s*((?:\[\d{1,3}:\d{2}(?:[.:]\d{1,3})?\])+)(.*)$")
LRC_TIME = re.compile(r"\[(\d{1,3}):(\d{2})(?:[.:](\d{1,3}))?\]")
META_LINE = re.compile(r"^(?:(?:作詞|作词|詞|词|作曲|曲|編曲|编曲|原唱|出品|製作人|制作人|吉他|弦樂|弦乐|和聲|和声|混音(?:/母带)?|母帶|母带|監製|监制|封面|統籌|统筹|制作公司)\s*[:：]|【此版本)")
FONT_PATHS = {
    "Microsoft YaHei": (Path("C:/Windows/Fonts/msyh.ttc"), Path("C:/Windows/Fonts/msyhbd.ttc")),
    "SimHei": (Path("C:/Windows/Fonts/simhei.ttf"),),
    "DengXian": (Path("C:/Windows/Fonts/Deng.ttf"),),
    "SimSun": (Path("C:/Windows/Fonts/simsun.ttc"),),
}


def run(command):
    try:
        return subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"{Path(command[0]).name} exited {exc.returncode}: {exc.stderr[-4000:]}") from exc


def require_tool(name):
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"{name} is required on PATH")
    return path


def probe_json(ffprobe, path, *args):
    result = run([ffprobe, "-v", "error", *args, "-of", "json", str(path)])
    return json.loads(result.stdout)


def media_duration(ffprobe, path, selector=None):
    args = ["-show_entries", "format=duration"]
    if selector:
        args = ["-select_streams", selector, "-show_entries", "stream=duration:format=duration"]
    data = probe_json(ffprobe, path, *args)
    values = [float(stream["duration"]) for stream in data.get("streams", []) if stream.get("duration") not in (None, "N/A")]
    if selector and not data.get("streams"):
        raise ValueError(f"Missing {selector} stream: {path}")
    value = values[0] if values else float(data.get("format", {}).get("duration", 0) or 0)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"could not read duration: {path}")
    return value


def parse_lrc(path, song_duration):
    entries = []
    source = Path(path).read_text(encoding="utf-8-sig")
    offsets = re.findall(r"\[offset:([+-]?\d+)\]", source, re.I)
    offset = int(offsets[-1]) / 1000 if offsets else 0
    for line in source.splitlines():
        match = LRC_LINE.match(line)
        if not match:
            continue
        text = match.group(2).strip()
        if META_LINE.match(text):
            continue
        for timestamp in LRC_TIME.finditer(match.group(1)):
            minutes, seconds, fraction = timestamp.groups()
            if int(seconds) >= 60:
                continue
            if fraction is None:
                fractional = 0
            elif len(fraction) == 1:
                fractional = int(fraction) / 10
            elif len(fraction) == 2:
                fractional = int(fraction) / 100
            else:
                fractional = int(fraction) / 1000
            start = max(0, int(minutes) * 60 + int(seconds) + fractional + offset)
            if 0 <= start < song_duration:
                entries.append((start, text))
    return sorted(dict(entries).items())


def ass_timestamp(seconds):
    centiseconds = max(0, round(seconds * 100))
    hours, centiseconds = divmod(centiseconds, 360000)
    minutes, centiseconds = divmod(centiseconds, 6000)
    whole_seconds, centiseconds = divmod(centiseconds, 100)
    return f"{hours}:{minutes:02d}:{whole_seconds:02d}.{centiseconds:02d}"


def font_glyphs(font_path):
    with TTFont(str(font_path), fontNumber=0) as font:
        return set(font.getBestCmap() or {})


def find_font(text, preferred=None):
    names = [preferred] if preferred else list(FONT_PATHS)
    required = {ord(char) for char in text if not char.isspace()}
    errors = []
    for name in names:
        paths = FONT_PATHS.get(name, ())
        for path in paths:
            if not path.is_file():
                continue
            try:
                if required.issubset(font_glyphs(path)):
                    return name, path
            except (TTLibError, OSError) as exc:
                errors.append(f"{path}: {exc}")
    missing = "".join(chr(code) for code in sorted(required))
    detail = "; ".join(errors)
    raise RuntimeError(f"No installed CJK font covers all lyric characters: {missing}. {detail}")


def ass_escape(text):
    return text.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")


def write_ass(path, entries, song_duration, width, height, font_name):
    font_size = max(12, round(width * 0.055))
    margin = max(8, round(width * 0.05))
    bottom = round(height * 0.09)
    outline = max(1, round(width / 360))
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Lyric,{font_name},{font_size},&H00FFFFFF,&H0000FFFF,&H00111120,&H80000000,0,0,0,0,100,100,0,0,1,{outline},1,2,{margin},{margin},{bottom},1", "",
        "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for index, (start, text) in enumerate(entries):
        if not text:
            continue
        end = entries[index + 1][0] if index + 1 < len(entries) else song_duration
        end = min(song_duration, start + 8.0, end)
        if end <= start:
            continue
        lines.append(f"Dialogue: 0,{ass_timestamp(start)},{ass_timestamp(end)},Lyric,,0,0,0,,{{\\fad(100,120)}}{ass_escape(text)}")
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8-sig")


def ffmpeg_subtitle_path(path):
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def load_outputs(manifest, state_path, digest):
    if not state_path.is_file():
        raise FileNotFoundError(f"generation state not found: {state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    validate_state(manifest, digest, state)
    outputs = []
    for shot in manifest["shots"]:
        entry = state.get("shots", {}).get(shot["id"], {})
        path = Path(entry.get("output") or "")
        if entry.get("status") != "completed" or not path.is_file():
            raise FileNotFoundError(f"missing completed output for shot {shot['id']}: {path}")
        outputs.append((shot, path))
    return outputs


def compose(manifest_path, output=None, width=None, height=None, fps=None, font=None, fit="cover"):
    _, manifest, _ = load_manifest(manifest_path)
    folder, _ = project_paths(manifest["project"])
    with project_lock(folder):
        return compose_locked(manifest_path, output, width, height, fps, font, fit)


def compose_locked(manifest_path, output=None, width=None, height=None, fps=None, font=None, fit="cover"):
    ffmpeg, ffprobe = require_tool("ffmpeg"), require_tool("ffprobe")
    manifest_path, manifest, digest = load_manifest(manifest_path)
    folder, state_path = project_paths(manifest["project"])
    shots = load_outputs(manifest, state_path, digest)
    video_width = manifest["width"] if width is None else width
    video_height = manifest["height"] if height is None else height
    target_fps = manifest["fps"] if fps is None else fps
    if video_width <= 0 or video_height <= 0 or target_fps < 30 or video_width % 2 or video_height % 2:
        raise ValueError("output dimensions must be positive even integers; fps must be at least 30")
    if fit not in {"cover", "contain"}:
        raise ValueError("fit must be cover or contain")

    audio = Path(manifest["audio"])
    audio_meta = probe_json(ffprobe, audio, "-select_streams", "a:0", "-show_entries", "stream=codec_name,sample_rate,channels:format=duration")
    if not audio_meta.get("streams"):
        raise ValueError(f"audio has no audio stream: {audio}")
    song_duration = media_duration(ffprobe, audio, "a:0")
    planned_duration = sum(shot["edit_duration"] for shot, _ in shots)
    if planned_duration + 1e-6 < song_duration:
        raise ValueError(f"shot durations total {planned_duration:.3f}s, shorter than audio {song_duration:.3f}s")

    for shot, path in shots:
        actual = media_duration(ffprobe, path, "V:0")
        if not output_is_valid(path, shot["duration"], manifest["width"], manifest["height"]):
            raise ValueError(f"Shot {shot['id']} failed generation output validation: {path}")
        if actual + 1e-6 < shot["edit_duration"]:
            raise ValueError(f"shot {shot['id']} is too short ({actual:.3f}s, expected {shot['duration']:.3f}s)")

    folder.mkdir(parents=True, exist_ok=True)
    final_path = Path(output).expanduser().resolve() if output else (folder / f"{manifest['project']}_final.mp4").resolve()
    inputs = {manifest_path, state_path, audio, *[p.resolve() for _, p in shots]}
    if manifest["lrc"]:
        inputs.add(Path(manifest["lrc"]))
    if final_path in inputs or final_path.suffix.lower() != ".mp4":
        raise ValueError("output must be a separate .mp4 file")
    final_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="wan22_mv_", dir=final_path.parent) as temp_dir:
        temp_dir = Path(temp_dir)
        master = temp_dir / "master.wav"
        run([ffmpeg, "-y", "-v", "error", "-i", str(audio), "-map", "0:a:0", "-vn", "-c:a", "pcm_s24le", str(master)])
        song_duration = media_duration(ffprobe, master, "a:0")
        if planned_duration + 1e-6 < song_duration:
            raise ValueError(f"shot durations total {planned_duration:.6f}s, shorter than decoded audio {song_duration:.6f}s")
        normalized = []
        elapsed = 0.0
        previous_frames = 0
        for index, (shot, source) in enumerate(shots):
            target = temp_dir / f"{index:04d}.mp4"
            elapsed += shot["edit_duration"]
            frames_at_end = math.ceil(elapsed * target_fps - 1e-9)
            frame_count = frames_at_end - previous_frames
            previous_frames = frames_at_end
            video_filter = f"fps={target_fps},scale={video_width}:{video_height}:force_original_aspect_ratio=increase,crop={video_width}:{video_height},setsar=1,format=yuv420p"
            if fit == "contain":
                video_filter = f"fps={target_fps},scale={video_width}:{video_height}:force_original_aspect_ratio=decrease:force_divisible_by=2,pad={video_width}:{video_height}:(ow-iw)/2:(oh-ih)/2:color=0x070d24,setsar=1,format=yuv420p"
            run([ffmpeg, "-y", "-v", "error", "-i", str(source), "-map", "0:V:0", "-an", "-vf", video_filter, "-frames:v", str(frame_count), "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(target)])
            normalized_info = probe_json(ffprobe, target, "-select_streams", "V:0", "-count_frames", "-show_entries", "stream=nb_read_frames")
            actual_frames = int(normalized_info["streams"][0]["nb_read_frames"])
            if actual_frames != frame_count:
                raise ValueError(f"shot {shot['id']} cannot supply {frame_count} output frames")
            normalized.append(target)

        concat_file = temp_dir / "concat.txt"
        concat_file.write_text("".join(f"file '{str(path).replace(chr(39), chr(39)+chr(92)+chr(39)+chr(39))}'\n" for path in normalized), encoding="utf-8")
        silent_video = temp_dir / "silent.mp4"
        run([ffmpeg, "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(concat_file), "-map", "0:v:0", "-an", "-c", "copy", str(silent_video)])
        if media_duration(ffprobe, silent_video, "V:0") + 1e-6 < song_duration:
            raise ValueError("Normalized video is shorter than audio; add another shot")

        candidate = temp_dir / "final.mp4"
        command = [ffmpeg, "-y", "-v", "error", "-i", str(silent_video), "-i", str(master), "-map", "0:V:0", "-map", "1:a:0", "-t", f"{song_duration:.6f}"]
        ass_file = None
        if manifest["lrc"]:
            lyrics = parse_lrc(manifest["lrc"], song_duration)
            if any(text for _, text in lyrics):
                lyric_text = "".join(text for _, text in lyrics)
                font_name, font_path = find_font(lyric_text, font)
                ass_file = folder / "lyrics.ass"
                write_ass(ass_file, lyrics, song_duration, video_width, video_height, font_name)
                # Run from the temporary directory so subtitle filter paths need no shell/filter escaping.
                shutil.copy2(ass_file, temp_dir / "lyrics.ass")
                (temp_dir / "fonts").mkdir()
                shutil.copy2(font_path, temp_dir / "fonts" / font_path.name)
                command.extend(["-vf", "ass=filename=lyrics.ass:fontsdir=fonts"])
                print(f"Lyrics: {sum(bool(text) for _, text in lyrics)} lines; font {font_name} ({font_path})")
            else:
                print("LRC contains no timed lyric lines; continuing without subtitles")
        command.extend(["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart", str(candidate)])
        result = subprocess.run(command, cwd=temp_dir, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if result.returncode:
            raise RuntimeError(f"ffmpeg failed: {result.stderr[-4000:]}")
        report = verify_output(ffmpeg, ffprobe, candidate, song_duration, video_width, video_height, target_fps, master)
        candidate.replace(final_path)
        report["output"] = str(final_path)
        atomic_json_write(folder / "verification.json", report)

    print(f"Output: {final_path}")
    return final_path


def silence_intervals(ffmpeg, path, duration):
    result = run([ffmpeg, "-hide_banner", "-i", str(path), "-map", "0:a:0", "-af", "silencedetect=noise=-45dB:d=2", "-f", "null", "-"])
    intervals = []
    start = None
    for kind, value in re.findall(r"silence_(start|end):\s*([\d.]+)", result.stderr):
        if kind == "start":
            start = float(value)
        elif start is not None:
            intervals.append([start, float(value)])
            start = None
    if start is not None:
        intervals.append([start, duration])
    return intervals


def verify_output(ffmpeg, ffprobe, path, expected_duration, width, height, fps, master=None):
    data = probe_json(ffprobe, path, "-show_entries", "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels")
    streams = data.get("streams", [])
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    if len(videos) != 1 or not audios:
        raise RuntimeError("final output must contain one video stream and at least one audio stream")
    video = videos[0]
    if video.get("width") != width or video.get("height") != height:
        raise RuntimeError(f"final dimensions are {video.get('width')}x{video.get('height')}, expected {width}x{height}")
    if abs(float(Fraction(video["r_frame_rate"])) - fps) > 0.001:
        raise RuntimeError(f"Unexpected final FPS: {video['r_frame_rate']}")
    actual_duration = float(data.get("format", {}).get("duration", 0))
    if abs(actual_duration - expected_duration) > 1 / fps + 0.03:
        raise RuntimeError(f"final duration is {actual_duration:.3f}s, expected {expected_duration:.3f}s")
    audio_duration = media_duration(ffprobe, path, "a:0")
    if abs(audio_duration - expected_duration) > 0.03:
        raise RuntimeError(f"Final audio length differs from master: {audio_duration} vs {expected_duration}")
    silence = silence_intervals(ffmpeg, path, expected_duration)
    original = silence_intervals(ffmpeg, master, expected_duration) if master else []
    unexpected = [(a, b) for a, b in silence if not any(abs(a - x) < 0.15 and abs(b - y) < 0.15 for x, y in original)]
    if unexpected:
        raise RuntimeError(f"New silence longer than 2 seconds: {unexpected}")
    print(f"Verified: {actual_duration:.3f}s, {width}x{height}, {video.get('r_frame_rate')} fps, audio {audios[0].get('codec_name')}; no added silence >2s")
    return {"duration": actual_duration, "audio_duration": audio_duration, "master_duration": expected_duration,
            "width": width, "height": height, "fps": fps, "source_silence": original, "output_silence": silence}


def main():
    parser = argparse.ArgumentParser(description="Compose Wan 2.2 MV shots with audio and optional LRC")
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--output")
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--fps", type=int)
    parser.add_argument("--fit", choices=["cover", "contain"], default="cover", help="cover crops to fill; contain preserves the full image with padding")
    parser.add_argument("--font", help="Preferred installed CJK font family")
    args = parser.parse_args()
    try:
        compose(args.manifest, args.output, args.width, args.height, args.fps, args.font, args.fit)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
