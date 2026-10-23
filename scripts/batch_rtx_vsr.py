#!/usr/bin/env python
"""One-file/one-frame-at-a-time NVIDIA VSR, NVENC and lossless audio remux.

Run with D:/AI_Video/ComfyUI/.venv/Scripts/python.exe. No model downloads at runtime.
PyAV uses FFmpeg's NVENC encoder; ffmpeg.exe remuxes the original audio packets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from fractions import Fraction
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parent
COMFY = Path('D:/AI_Video/ComfyUI')
DEFAULT_MODEL = COMFY / 'models/upscale_models/RealESRGAN_x2plus.pth'
MODEL_SHA256 = '49fafd45f8fd7aa8d31ab2a22d14d91b536c34494a5cfe31eb5d89c2fa266abb'
QUALITY = ('LOW', 'MEDIUM', 'HIGH', 'ULTRA', 'HIGHBITRATE_HIGH', 'HIGHBITRATE_ULTRA')


def run(args, **kwargs):
    p = subprocess.run([str(x) for x in args], capture_output=True, text=True,
                       encoding='utf-8', errors='replace', **kwargs)
    if p.returncode:
        raise RuntimeError(f'{Path(str(args[0])).name} exit {p.returncode}: {p.stderr[-5000:]}')
    return p


def write_json(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def sha256(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def probe(path, count=False):
    args = ['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json']
    if count:
        args += ['-count_frames']
    return json.loads(run(args + [path]).stdout)


def video_stream(info):
    for s in info['streams']:
        if s['codec_type'] == 'video' and not s.get('disposition', {}).get('attached_pic'):
            return s
    raise ValueError('No real video stream (cover art does not count)')


def target_size(width, height, scale=2.0, target_height=None, target_width=None):
    if target_height is not None:
        height_out = target_height
        width_out = max(2, round(width * height_out / height / 2) * 2)
    elif target_width is not None:
        width_out = target_width
        height_out = max(2, round(height * width_out / width / 2) * 2)
    else:
        width_out, height_out = round(width * scale), round(height * scale)
    if min(width_out, height_out) < 160 or max(width_out, height_out) > 8192:
        raise ValueError('NVENC output dimensions must be within 160..8192 for this pipeline')
    if width_out % 2 or height_out % 2:
        raise ValueError('YUV420 NVENC requires even output dimensions; specify --target-width/height')
    return width_out, height_out


def collect_files(input_dir, output_dir, recursive=True):
    source = Path(input_dir).resolve()
    target = Path(output_dir).resolve()
    if source == target or source.is_relative_to(target):
        raise ValueError('Output must not equal or contain the input directory')
    paths = source.rglob('*') if recursive else source.glob('*')
    result = []
    for p in paths:
        if p.is_file() and p.suffix.lower() == '.mp4' and not p.resolve().is_relative_to(target):
            result.append((p, target / p.relative_to(source)))
    return sorted(result)


def audio_hash(path, index):
    return run(['ffmpeg', '-v', 'error', '-i', path, '-map', f'0:a:{index}',
                '-c:a', 'copy', '-f', 'hash', '-hash', 'sha256', '-']).stdout.strip()


def validate_result(source, dest, size, frames, expected_duration):
    src, dst = probe(source), probe(dest, count=True)
    sv, dv = video_stream(src), video_stream(dst)
    if (dv['width'], dv['height']) != tuple(size):
        raise RuntimeError('Encoded dimensions do not match the requested size')
    if int(dv.get('nb_read_frames', 0)) != frames:
        raise RuntimeError(f'Frame loss: expected {frames}, got {dv.get("nb_read_frames")}')
    out_duration = float(dv['duration'])
    if abs(out_duration - expected_duration) > 0.05:
        raise RuntimeError(f'Video timing changed: {out_duration} vs {expected_duration}')
    sa = [s for s in src['streams'] if s['codec_type'] == 'audio']
    da = [s for s in dst['streams'] if s['codec_type'] == 'audio']
    if len(sa) != len(da):
        raise RuntimeError('Audio stream count changed')
    hashes = []
    for i, (a, b) in enumerate(zip(sa, da)):
        if a['codec_name'] != b['codec_name']:
            raise RuntimeError('Audio codec changed instead of stream-copying')
        before, after = audio_hash(source, i), audio_hash(dest, i)
        if before != after:
            raise RuntimeError(f'Audio packet payload changed in stream {i}')
        source_offset = float(a.get('start_time', 0)) - float(sv.get('start_time', 0))
        target_offset = float(b.get('start_time', 0)) - float(dv.get('start_time', 0))
        if abs(source_offset - target_offset) > 0.05:
            raise RuntimeError(f'Audio/video start offset changed in stream {i}')
        hashes.append({'stream': i, 'codec': a['codec_name'], 'packet_sha256': after,
                       'relative_start': target_offset})
    # Streaming full-decode check, including audio, never buffers the whole video.
    run(['ffmpeg', '-v', 'error', '-xerror', '-i', dest, '-map', '0:V:0',
         '-map', '0:a?', '-f', 'null', '-'])
    return {'frames': frames, 'duration': out_duration, 'audio': hashes,
            'audio_lossless': True, 'full_decode': 'passed'}


class RTXBackend:
    def __init__(self, width, height, size, quality, deblur, device):
        import torch
        import nvvfx
        self.torch, self.nvvfx, self.device = torch, nvvfx, device
        self.width, self.height = width, height
        self.size = size
        self.effects = []
        # Replicate edge pixels up to SDK alignment, then crop at the exact scale.
        self.pw, self.ph = math.ceil(width / 8) * 8, math.ceil(height / 8) * 8
        self.ow = math.ceil(self.pw * size[0] / width / 8) * 8
        self.oh = math.ceil(self.ph * size[1] / height / 8) * 8
        self.crop_w = round(width * self.ow / self.pw)
        self.crop_h = round(height * self.oh / self.ph)
        if deblur:
            self.effects.append(self.create(nvvfx.QualityLevel.DEBLUR_HIGH, self.pw, self.ph))
        self.effects.append(self.create(nvvfx.QualityLevel[quality], self.ow, self.oh))
        self.details = {'backend': 'rtx', 'library': nvvfx.__version__,
                        'sdk': str(nvvfx.get_sdk_version()), 'quality': quality,
                        'deblur': 'NVIDIA DEBLUR_HIGH' if deblur else 'off',
                        'sdk_input': [self.pw, self.ph], 'sdk_output': [self.ow, self.oh]}

    def create(self, quality, width, height):
        e = self.nvvfx.VideoSuperRes(quality=quality, device=self.device)
        e.output_width, e.output_height = width, height
        e.load()
        return e

    def process(self, array):
        torch = self.torch
        import torch.nn.functional as F
        with torch.inference_mode():
            x = torch.from_numpy(array).to(device=f'cuda:{self.device}', dtype=torch.float32)
            x = x.permute(2, 0, 1).contiguous().div_(255)
            if self.pw != self.width or self.ph != self.height:
                x = F.pad(x, (0, self.pw-self.width, 0, self.ph-self.height), mode='replicate')
            for effect in self.effects:
                x = torch.from_dlpack(effect.run(x.contiguous()).image).clone()
            x = x[:, :self.crop_h, :self.crop_w]
            if tuple(x.shape[-2:]) != (self.size[1], self.size[0]):
                x = F.interpolate(x.unsqueeze(0), size=(self.size[1], self.size[0]),
                                  mode='bicubic', align_corners=False)[0]
            return x.clamp_(0, 1).mul_(255).round_().to(torch.uint8).permute(1, 2, 0).cpu().numpy()


class ESRGANBackend:
    def __init__(self, model, size, tile, device, deblur):
        import torch
        from spandrel import ModelLoader
        if not Path(model).is_file():
            raise FileNotFoundError(f'RealESRGAN_x2plus weights not installed: {model}')
        if sha256(model) != MODEL_SHA256:
            raise ValueError('RealESRGAN_x2plus SHA256 mismatch; refusing unknown checkpoint')
        self.torch, self.device, self.size = torch, device, size
        self.tile, self.deblur = tile, deblur
        self.model = ModelLoader().load_from_file(model).eval().to(f'cuda:{device}').half()
        if self.model.scale != 2 or not self.model.supports_half:
            raise RuntimeError('Expected FP16-capable RealESRGAN_x2plus')
        self.details = {'backend': 'realesrgan', 'model': str(model), 'model_sha256': MODEL_SHA256,
                        'precision': 'float16', 'tile': tile,
                        'deblur': 'mild unsharp after ESRGAN (not NVIDIA deblur)' if deblur else 'off'}

    def process(self, array):
        import numpy as np
        import cv2
        torch = self.torch
        height, width = array.shape[:2]
        # The only large output is one RGB frame in CPU RAM. CUDA holds one tile.
        result = np.empty((height*2, width*2, 3), dtype=np.uint8)
        overlap = 24
        with torch.inference_mode():
            for y in range(0, height, self.tile):
                for x in range(0, width, self.tile):
                    x1, y1 = min(width, x+self.tile), min(height, y+self.tile)
                    a, b = max(0, x-overlap), max(0, y-overlap)
                    c, d = min(width, x1+overlap), min(height, y1+overlap)
                    crop = np.ascontiguousarray(array[b:d, a:c])
                    tensor = torch.from_numpy(crop).to(f'cuda:{self.device}', dtype=torch.float16)
                    tensor = tensor.permute(2, 0, 1).unsqueeze(0).div_(255)
                    out = self.model(tensor)[0]
                    out = out[:, 2*(y-b):2*(y1-b), 2*(x-a):2*(x1-a)]
                    result[2*y:2*y1, 2*x:2*x1] = out.clamp_(0,1).mul_(255).round_().to(torch.uint8).permute(1,2,0).cpu().numpy()
                    del tensor, out
        if (width*2, height*2) != self.size:
            result = cv2.resize(result, self.size, interpolation=cv2.INTER_LANCZOS4)
        if self.deblur:
            result = cv2.addWeighted(result, 1.15, cv2.GaussianBlur(result, (0,0), 0.8), -0.15, 0)
        return result


def frames_with_duration(container, stream):
    it = iter(container.decode(stream))
    current = next(it, None)
    if current is None:
        raise ValueError('Video has no decodable frames')
    if current.pts is None:
        raise ValueError('Video frames have no timestamps; cannot safely retain sync')
    first_time = Fraction(current.pts) * current.time_base
    for following in it:
        if following.pts is None:
            raise ValueError('Missing frame timestamp')
        timestamp = Fraction(current.pts) * current.time_base
        duration = Fraction(following.pts) * following.time_base - timestamp
        if duration <= 0:
            raise ValueError('Video has non-monotonic presentation timestamps')
        yield current, timestamp-first_time, duration, first_time
        current = following
    timestamp = Fraction(current.pts) * current.time_base
    duration = Fraction(current.duration) * current.time_base if current.duration else None
    if not duration or duration <= 0:
        end = (stream.start_time or 0) + (stream.duration or 0)
        duration = Fraction(end) * stream.time_base - timestamp
    if duration <= 0:
        duration = 1 / (stream.average_rate or stream.guessed_rate or Fraction(30))
    yield current, timestamp-first_time, duration, first_time


def worker(job_path):
    # Import GPU libraries only in the short-lived, per-video subprocess.
    import av
    import torch
    job = json.loads(Path(job_path).read_text(encoding='utf-8'))
    source, dest = Path(job['source']), Path(job['dest'])
    info = probe(source); video = video_stream(info)
    width, height = video['width'], video['height']
    high_bit_depth = max(c.bits for c in av.VideoFormat(video['pix_fmt']).components) > 8
    if video.get('color_transfer') in ('smpte2084', 'arib-std-b67') or high_bit_depth:
        raise ValueError('HDR/10-bit input needs an explicit tone-map/10-bit pipeline; refusing silent SDR clipping')
    if any(abs(float(s.get('rotation',0))) > 0.1 for s in video.get('side_data_list', [])):
        raise ValueError('Rotated video must be orientation-normalized first')
    size = tuple(job['size'])
    torch.cuda.set_device(job['device'])
    torch.set_num_threads(2)
    torch.backends.cudnn.benchmark = True
    started = time.perf_counter()
    if job['backend'] == 'rtx':
        engine = RTXBackend(width, height, size, job['quality'], job['deblur'], job['device'])
    else:
        engine = ESRGANBackend(job['model'], size, job['tile'], job['device'], job['deblur'])
    init_seconds = time.perf_counter()-started
    frame_count = 0
    processing_seconds = 0
    duration_ticks = {}
    first_time = Fraction(0)
    tick = Fraction(1, 90000)
    peak_allocated = 0
    with tempfile.TemporaryDirectory(prefix='.vsr-', dir=dest.parent) as temp:
        temp = Path(temp)
        silent = temp/'enhanced_video.mp4'
        candidate = temp/'muxed.mp4'
        with av.open(str(source)) as decoded, av.open(str(silent), 'w', options={'movflags': '+faststart'}) as encoded:
            vs = next(s for s in decoded.streams.video if s.index == video['index'])
            vs.thread_type = 'SLICE'
            vs.codec_context.thread_count = 2
            rate = vs.average_rate or vs.guessed_rate or Fraction(30)
            output = encoded.add_stream(job['encoder'], rate=rate)
            output.width, output.height = size
            output.pix_fmt = 'yuv420p'
            output.time_base = tick
            output.codec_context.time_base = tick
            output.codec_context.max_b_frames = 0
            output.codec_context.color_range = 1
            output.codec_context.colorspace = 1
            output.codec_context.color_primaries = 1
            output.codec_context.color_trc = 1
            output.options = {'preset': 'p4', 'rc': 'vbr', 'cq': str(job['cq']), 'b': '0', 'bf': '0'}
            last_pts = -1

            def mux_packets(packets):
                for packet in packets:
                    key = round(Fraction(packet.pts)*packet.time_base/tick)
                    if key not in duration_ticks:
                        raise RuntimeError('Encoder returned an unexpected timestamp')
                    packet.duration = max(1, round(duration_ticks.pop(key)*tick/packet.time_base))
                    encoded.mux(packet)

            for frame, timestamp, duration, first_time in frames_with_duration(decoded, vs):
                if (frame.width, frame.height) != (width, height):
                    raise ValueError('Resolution changes inside a video are unsupported')
                pts = round(timestamp/tick)
                if pts <= last_pts:
                    raise ValueError('Frame rate exceeds the output timestamp precision')
                last_pts = pts
                t = time.perf_counter()
                enhanced = engine.process(frame.to_ndarray(format='rgb24'))
                torch.cuda.synchronize()
                processing_seconds += time.perf_counter()-t
                vf = av.VideoFrame.from_ndarray(enhanced, format='rgb24')
                vf = vf.reformat(format='yuv420p', src_colorspace='ITU709', dst_colorspace='ITU709',
                                 src_color_range='JPEG', dst_color_range='MPEG')
                vf.pts, vf.time_base = pts, tick
                duration_ticks[pts] = max(1, round(duration/tick))
                mux_packets(output.encode(vf))
                frame_count += 1
                expected_duration = float(timestamp+duration)
                del enhanced, vf
                if frame_count % 64 == 0:
                    print(f'frames={frame_count} backend={job["backend"]}', flush=True)
            mux_packets(output.encode(None))
            if duration_ticks:
                raise RuntimeError('Encoder did not flush all frames')
        peak_allocated = torch.cuda.max_memory_allocated()
        # Preserve audio timestamps relative to the first source video PTS, not its cover image.
        run(['ffmpeg', '-y', '-v', 'error', '-copyts', '-i', silent,
             '-itsoffset', str(-float(first_time)), '-i', source,
             '-map', '0:V:0', '-map', '1:a?', '-map_metadata', '1', '-map_chapters', '-1',
             '-c:v', 'copy', '-c:a', 'copy', '-avoid_negative_ts', 'disabled',
             '-movflags', '+faststart', candidate])
        validation = validate_result(source, candidate, size, frame_count, expected_duration)
        candidate.replace(dest)
    backend_details = engine.details
    del engine
    torch.cuda.empty_cache()
    report = {'source': str(source), 'output': str(dest), 'source_size': [width,height],
              'output_size': list(size), 'backend': job['backend'], 'backend_details': backend_details,
              'encoder': job['encoder'], 'cq': job['cq'], 'frames': frame_count,
              'init_seconds': init_seconds, 'frame_processing_seconds': processing_seconds,
              'total_seconds': time.perf_counter()-started, 'validation': validation,
              'torch_peak_allocated_bytes': peak_allocated,
              'source_signature': job['source_signature'], 'options': job['options']}
    write_json(Path(job['report']), report)
    print(json.dumps(report, ensure_ascii=False), flush=True)


def skip_existing(source, dest, signature, options, size, adopt=True):
    report_path = dest.with_suffix('.vsr.json')
    if not dest.is_file() or dest.stat().st_size == 0:
        return False
    try:
        info = probe(dest); v = video_stream(info)
        if (v['width'],v['height']) != tuple(size):
            return False
        if report_path.is_file():
            report = json.loads(report_path.read_text(encoding='utf-8'))
            return (report.get('source_signature') == signature and report.get('options') == options
                    and report.get('validation',{}).get('full_decode') == 'passed'
                    and report.get('output_signature') == {'size':dest.stat().st_size,'mtime_ns':dest.stat().st_mtime_ns})
        src_video = video_stream(probe(source, count=True))
        validation = validate_result(source, dest, size, int(src_video['nb_read_frames']), float(src_video['duration']))
        if adopt:
            write_json(report_path, {'backend': 'unknown-existing', 'source_signature': signature,
                       'options': options, 'validation': validation,
                       'output_signature': {'size': dest.stat().st_size, 'mtime_ns': dest.stat().st_mtime_ns}})
        return True
    except (RuntimeError, json.JSONDecodeError, KeyError):
        return False


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input-dir', type=Path, default=ROOT/'raw_videos')
    p.add_argument('--output-dir', type=Path, default=ROOT/'upscaled_1080p')
    p.add_argument('--scale', type=float, default=2.0, help='2 doubles BOTH dimensions; does not imply 1080p')
    group=p.add_mutually_exclusive_group()
    group.add_argument('--target-height', type=int, help='e.g.1080 for landscape,1920 for portrait')
    group.add_argument('--target-width', type=int, help='e.g.1080 for portrait')
    p.add_argument('--backend', choices=['auto','rtx','realesrgan'], default='auto')
    p.add_argument('--fallback', choices=['realesrgan','none'], default='realesrgan')
    p.add_argument('--quality', choices=QUALITY, default='HIGH')
    p.add_argument('--deblur', action='store_true', help='NVIDIA same-resolution DEBLUR_HIGH before VSR')
    p.add_argument('--encoder', choices=['h264_nvenc','hevc_nvenc'], default='h264_nvenc')
    p.add_argument('--cq', type=int, default=16)
    p.add_argument('--model', type=Path, default=DEFAULT_MODEL)
    p.add_argument('--tile', type=int, default=192, help='RealESRGAN FP16 tile edge, with24px overlap')
    p.add_argument('--device', type=int, default=0)
    p.add_argument('--no-recursive', action='store_true')
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--timeout', type=float, default=0, help='Per-backend process timeout in seconds,0=no limit')
    p.add_argument('--worker-job', type=Path, help=argparse.SUPPRESS)
    return p


@contextmanager
def output_lock(folder):
    if os.name == 'nt':
        import msvcrt
    else:
        import fcntl
    with (folder/'.batch_rtx_vsr.lock').open('a+b') as f:
        if f.tell() == 0:
            f.write(b'0'); f.flush()
        f.seek(0)
        try:
            if os.name == 'nt': msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else: fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f'Another batch already owns {folder}') from exc
        try:
            yield
        finally:
            f.seek(0)
            if os.name == 'nt': msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(f, fcntl.LOCK_UN)


def main(argv=None):
    args = parser().parse_args(argv)
    if args.worker_job:
        worker(args.worker_job)
        return 0
    if not math.isfinite(args.scale) or not 1 <= args.scale <= 4:
        raise ValueError('--scale must be between1 and4')
    if not 0 <= args.cq <= 51 or args.tile < 32:
        raise ValueError('CQ must be0..51 and tile must be at least32')
    if not math.isfinite(args.timeout) or args.timeout < 0:
        raise ValueError('--timeout must be a finite nonnegative value')
    for name in ('ffmpeg','ffprobe'):
        if not shutil.which(name): raise FileNotFoundError(f'{name} not on PATH')
    args.input_dir = args.input_dir.resolve();args.output_dir=args.output_dir.resolve()
    if not args.input_dir.is_dir():
        raise FileNotFoundError(f'Input folder missing: {args.input_dir}')
    files = collect_files(args.input_dir,args.output_dir,not args.no_recursive)
    print(f'Found {len(files)} MP4 file(s); one worker process at a time',flush=True)
    if not args.dry_run: args.output_dir.mkdir(parents=True,exist_ok=True)
    if args.dry_run:
        return process_batch(args, files)
    with output_lock(args.output_dir):
        return process_batch(args, files)


def process_batch(args, files):
    summary=[]
    for source,dest in files:
        try:
            info=probe(source); v=video_stream(info)
            size=target_size(v['width'],v['height'],args.scale,args.target_height,args.target_width)
            stat=source.stat(); signature={'path':str(source.resolve()),'size':stat.st_size,'mtime_ns':stat.st_mtime_ns}
            options={k:getattr(args,k) for k in ('scale','target_height','target_width','backend','fallback','quality','deblur','encoder','cq','tile','device')}
            options['model']=str(args.model.resolve())
            if not args.overwrite and skip_existing(source,dest,signature,options,size,adopt=not args.dry_run):
                print(f'SKIP {source.name}',flush=True);summary.append({'source':str(source),'status':'skipped'});continue
            print(f'{source.name}: {v["width"]}x{v["height"]} -> {size[0]}x{size[1]}',flush=True)
            if args.dry_run: continue
            dest.parent.mkdir(parents=True,exist_ok=True)
            backends=['rtx'] if args.backend in ('auto','rtx') else ['realesrgan']
            if args.backend=='auto' and args.fallback=='realesrgan':backends.append('realesrgan')
            errors=[]; start=time.perf_counter()
            for backend in backends:
                job={'source':str(source),'dest':str(dest),'size':size,'backend':backend,
                     'quality':args.quality,'deblur':args.deblur,'device':args.device,'model':str(args.model),
                     'tile':args.tile,'encoder':args.encoder,'cq':args.cq,'source_signature':signature,
                     'options':options,'report':str(dest.with_suffix('.vsr.json'))}
                with tempfile.TemporaryDirectory(prefix='.job-',dir=dest.parent) as temp:
                    job_path=Path(temp)/'job.json';write_json(job_path,job)
                    log_path=dest.with_suffix(f'.{backend}.log')
                    print(f'START {backend}: {source.name}',flush=True)
                    with log_path.open('w',encoding='utf-8') as log:
                        try:
                            p=subprocess.run([sys.executable,'-u',str(Path(__file__).resolve()),'--worker-job',str(job_path)],stdout=log,stderr=subprocess.STDOUT,env=dict(os.environ,PYTHONIOENCODING='utf-8'),timeout=args.timeout or None)
                            exit_code=p.returncode
                        except subprocess.TimeoutExpired:
                            # subprocess.run kills and waits for this worker before fallback.
                            exit_code=124
                            log.write('\nWorker exceeded --timeout and was stopped.\n')
                if exit_code==0:
                    rp=dest.with_suffix('.vsr.json');report=json.loads(rp.read_text(encoding='utf-8'))
                    report['fallback_errors']=errors
                    report['wall_seconds_including_worker']=time.perf_counter()-start
                    report['output_signature']={'size':dest.stat().st_size,'mtime_ns':dest.stat().st_mtime_ns}
                    write_json(rp,report)
                    print(f'DONE {source.name}: {backend}, {report["frames"]} frames, {report["wall_seconds_including_worker"]:.2f}s, audio={len(report["validation"]["audio"])}',flush=True)
                    summary.append({'source':str(source),'output':str(dest),'status':'complete','backend':backend,'seconds':report['wall_seconds_including_worker']})
                    break
                error={'backend':backend,'exit_code':exit_code,'log':str(log_path)};errors.append(error)
                print(f'FAILED {backend}: {source.name}; see {log_path}',flush=True)
            else:
                raise RuntimeError(f'All requested backends failed: {errors}')
        except (OSError,ValueError,RuntimeError,KeyError) as exc:
            print(f'ERROR {source}: {exc}',file=sys.stderr,flush=True)
            summary.append({'source':str(source),'status':'failed','error':str(exc)})
        if not args.dry_run:write_json(args.output_dir/'batch_summary.json',summary)
    return 1 if any(s['status']=='failed' for s in summary) else 0


if __name__=='__main__':
    try:
        raise SystemExit(main())
    except (OSError,ValueError,RuntimeError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        raise SystemExit(1)
