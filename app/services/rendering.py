"""FFmpeg-backed MP4 generation for recording clips and topic explainers."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import wave
from pathlib import Path
from typing import Any, Callable

from app.config import settings


def ffmpeg_path() -> str:
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError as exc:
        raise RuntimeError("Install imageio-ffmpeg or add FFmpeg to PATH to render MP4 files.") from exc


def run_ffmpeg(args: list[str], check_cancelled: Callable[[], None] | None = None) -> None:
    if check_cancelled:
        check_cancelled()
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + 1800
    try:
        while True:
            try:
                stdout, stderr = process.communicate(timeout=1)
                break
            except subprocess.TimeoutExpired:
                if check_cancelled:
                    check_cancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError("FFmpeg rendering exceeded 30 minutes.")
        if process.returncode:
            message = (stderr or stdout or "FFmpeg failed").strip()
            raise RuntimeError(message[-2500:])
    except Exception:
        if process.poll() is None:
            process.terminate()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
        raise


def _font_file() -> str | None:
    candidates = [Path("C:/Windows/Fonts/arial.ttf"), Path("C:/Windows/Fonts/segoeui.ttf"), Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")]
    return next((str(p) for p in candidates if p.is_file()), None)


def _escape_drawtext(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\r\n", "\\n").replace("\n", "\\n").replace(":", "\\:").replace("'", "\\'").replace("%", "\\%").replace(",", "\\,").replace("[", "\\[").replace("]", "\\]").replace(";", "\\;")


def _write_filter_graph(path: Path, graph: str) -> None:
    # Keep large caption graphs out of CreateProcess's Windows command-line limit.
    path.write_text(graph, encoding="utf-8", newline="\n")


def _enable_between(start: float, end: float) -> str:
    # Commas inside FFmpeg expressions are separators unless backslash-escaped.
    return f"gte(t\\,{start:.3f})*lt(t\\,{end:.3f})"


def _has_audio(path: Path) -> bool:
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    return bool(re.search(r"Stream #\d+:\d+.*Audio:", probe.stderr))


def _prepare_brand_segment(source: str, output: Path, check_cancelled: Callable[[], None] | None = None) -> None:
    image = Path(source).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
    args = [ffmpeg_path(), "-y"]
    if image:
        args += ["-loop", "1", "-framerate", "30", "-t", "2", "-i", source]
    else:
        args += ["-i", source]
    args += ["-vf", "scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,fps=30,format=yuv420p,tpad=stop_mode=clone:stop_duration=2,trim=duration=2,setpts=PTS-STARTPTS", "-an", "-t", "2", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(output)]
    run_ffmpeg(args, check_cancelled)


def _compose_brand_segments(main: Path, output: Path, brand: dict[str, Any] | None, check_cancelled: Callable[[], None] | None = None) -> Path:
    """Add optional two-second intro/outro assets around the rendered video."""
    brand = brand or {}
    intro, outro = brand.get("intro_path"), brand.get("outro_path")
    if not intro and not outro:
        if main != output:
            main.replace(output)
        return output
    parts: list[Path] = []
    inputs: list[str] = [str(main)]
    refs: list[str] = []
    temporary: list[Path] = []
    try:
        for name, asset in (("intro", intro), ("outro", outro)):
            if not asset or not Path(asset).is_file():
                continue
            part = output.with_name(f"{output.stem}.{name}.mp4")
            _prepare_brand_segment(asset, part, check_cancelled)
            parts.append(part)
            temporary.append(part)
        input_paths = [str(main), *[str(part) for part in parts]]
        intro_index = None
        next_index = 1
        if intro and Path(intro).is_file():
            intro_index = next_index
            next_index += 1
        outro_index = next_index if outro and Path(outro).is_file() else None
        ordered_indexes = ([intro_index] if intro_index else []) + [0] + ([outro_index] if outro_index else [])
        segment_refs = []
        video_filters = []
        for index in ordered_indexes:
            label = f"v{index}"
            video_filters.append(f"[{index}:v]scale=720:1280,setsar=1,fps=30,format=yuv420p,setpts=PTS-STARTPTS[{label}]")
            segment_refs.append(f"[{label}]")
        graph = ";".join(video_filters + ["".join(segment_refs) + f"concat=n={len(segment_refs)}:v=1:a=0[vout]"])
        intro_seconds, outro_seconds = (2 if intro_index else 0), (2 if outro_index else 0)
        audio = _has_audio(main)
        if audio:
            delay = intro_seconds * 1000
            graph += f";[0:a]adelay={delay}|{delay},apad=pad_dur={outro_seconds}[aout]"
        args = [ffmpeg_path(), "-y", "-i", str(main)]
        for part in parts:
            args += ["-i", str(part)]
        args += ["-filter_complex", graph, "-map", "[vout]"]
        if audio:
            args += ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
        else:
            args += ["-an"]
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output)]
        run_ffmpeg(args, check_cancelled)
        return output
    finally:
        main.unlink(missing_ok=True)
        for item in temporary:
            item.unlink(missing_ok=True)


def render_recording_clip(source: str, output: Path, start: float, end: float, title: str, focus_x: float = 0.5, brand: dict[str, Any] | None = None, words: list[dict[str, Any]] | None = None, focus_segments: list[dict[str, Any]] | None = None, check_cancelled: Callable[[], None] | None = None) -> Path:
    if not settings.enable_video_rendering:
        raise RuntimeError("Video rendering is disabled in settings.")
    if not Path(source).is_file():
        raise RuntimeError("The source recording is missing from the uploads folder.")
    output.parent.mkdir(parents=True, exist_ok=True)
    intro_outro = bool((brand or {}).get("intro_path") or (brand or {}).get("outro_path"))
    main_output = output.with_name(f"{output.stem}.main.mp4") if intro_outro else output
    duration = max(1, end - start)
    focus_x = min(1.0, max(0.0, float(focus_x)))
    accent = ((brand or {}).get("caption_style", {}).get("accent") or (brand or {}).get("colors", {}).get("primary", "#B9F66A")).lstrip("#")
    if len(accent) != 6:
        accent = "B9F66A"
    x_expr = f"{focus_x:.4f}*iw-ih*9/32"
    for segment in reversed(sorted(focus_segments or [], key=lambda item: float(item.get("start_seconds", start)))):
        try:
            seg_end = min(duration, max(0, float(segment.get("end_seconds", end)) - start))
            center = min(1, max(0, float(segment.get("center_x", focus_x))))
        except (TypeError, ValueError):
            continue
        x_expr = f"if(lt(t,{seg_end:.3f}),{center:.4f}*iw-ih*9/32,{x_expr})"
    # Commas inside an FFmpeg expression still delimit filters unless escaped.
    # This matters especially for focus_segments, which add nested if()/min()
    # expressions and otherwise make FFmpeg report a misleading "No such filter".
    crop_x = f"max(0,min(iw-ih*9/16,{x_expr}))".replace(",", r"\,")
    crop_width = r"min(iw\,ih*9/16)"
    ass_file = output.with_suffix(".ass")
    caption_font = (brand or {}).get("caption_style", {}).get("font", "Arial")
    _write_clip_ass(words or [], ass_file, start, end, title, ((brand or {}).get("name") or "").strip(), accent, caption_font)
    filters = [f"crop=w='{crop_width}':h=ih:x='{crop_x}':y=0", "scale=720:1280:force_original_aspect_ratio=increase", "crop=720:1280", f"subtitles='{_subtitle_path(ass_file)}'"]
    logo = (brand or {}).get("logo_path")
    has_logo = bool(logo and Path(logo).is_file())
    graph_file = output.with_name(f"{output.stem}.filtergraph")
    graph = f"[0:v]{','.join(filters)}[base];[1:v]scale=105:-1,format=rgba[logo];[base][logo]overlay=x=W-w-24:y=90:shortest=1:format=auto[vout]" if has_logo else f"[0:v]{','.join(filters)}[vout]"
    _write_filter_graph(graph_file, graph)
    args = [ffmpeg_path(), "-y", "-ss", str(start), "-i", source]
    if has_logo:
        args += ["-loop", "1", "-framerate", "30", "-i", str(logo)]
    args += ["-/filter_complex", str(graph_file), "-map", "[vout]", "-t", f"{duration:.3f}", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-af", "loudnorm", "-movflags", "+faststart", str(main_output)]
    try:
        run_ffmpeg(args, check_cancelled)
    finally:
        graph_file.unlink(missing_ok=True)
        ass_file.unlink(missing_ok=True)
    return _compose_brand_segments(main_output, output, brand, check_cancelled)


def _write_ass(captions: list[dict[str, Any]], destination: Path, duration: float, accent: str) -> None:
    def stamp(seconds: float) -> str:
        seconds = max(0, min(duration, seconds))
        whole = int(seconds)
        return f"{whole // 3600}:{(whole % 3600) // 60:02}:{whole % 60:02}.{int((seconds % 1) * 100):02}"
    caption_color = _ass_color(accent)
    lines = ["[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280", "", "[V4+ Styles]", "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding", f"Style: Default,Arial,42,{caption_color},&H00FFFFFF,&H80000000,&H90000000,1,0,1,2,0,2,35,35,125,1", "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for item in captions:
        start, end = float(item.get("start_seconds", item.get("start", 0))), float(item.get("end_seconds", item.get("end", 0)))
        caption = str(item.get("text", "")).replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}").replace("\n", " ")
        if caption and end > start:
            lines.append(f"Dialogue: 0,{stamp(start)},{stamp(end)},Default,,0,0,0,,{caption}")
    destination.write_text("\n".join(lines), encoding="utf-8")


def _ass_escape(value: Any) -> str:
    return str(value).replace("\\", "\\\\").replace("{", r"\{").replace("}", r"\}").replace("\r\n", r"\N").replace("\n", r"\N")


def _ass_color(value: str) -> str:
    rgb = value.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", rgb):
        rgb = "B9F66A"
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2].upper()}"


def _ass_header(accent: str, caption_font: str = "Arial") -> list[str]:
    theme_color = _ass_color("#B9F66A")
    caption_color = _ass_color(accent)
    if caption_font not in {"Arial", "Trebuchet MS", "Courier New"}:
        caption_font = "Arial"
    return [
        "[Script Info]", "ScriptType: v4.00+", "PlayResX: 720", "PlayResY: 1280", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,Arial,40,&H00FFFFFF,&H00FFFFFF,&H80000000,&H90000000,1,0,1,2,0,2,36,36,120,1",
        f"Style: Caption,{caption_font},40,{caption_color},&H00FFFFFF,&H80000000,&H90000000,1,0,1,2,0,2,36,36,120,1",
        f"Style: Title,Arial,32,{theme_color},&H00FFFFFF,&H80000000,&H90000000,1,0,1,2,0,8,40,40,70,1",
        f"Style: Brand,Arial,23,{theme_color},&H00FFFFFF,&H80000000,&H90000000,1,0,1,2,0,3,30,30,48,1",
        f"Style: Scene,Arial,38,&H00FFFFFF,&H00FFFFFF,&H80000000,&H90000000,1,0,1,2,0,7,54,54,310,1",
        f"Style: Code,Courier New,25,{theme_color},&H00FFFFFF,&H80000000,&H90000000,0,0,1,2,0,7,50,50,650,1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]


def _write_clip_ass(words: list[dict[str, Any]], destination: Path, start: float, end: float,
                    title: str, brand_name: str, accent: str, caption_font: str = "Arial") -> None:
    duration = max(0.1, end - start)
    lines = _ass_header(accent, caption_font)
    lines.append(f"Dialogue: 0,0:00:00.00,0:00:05.00,Title,,0,0,0,,{_ass_escape(title[:100])}")
    if brand_name:
        lines.append(f"Dialogue: 0,0:00:00.00,{_ass_stamp(duration)},Brand,,0,0,0,,{_ass_escape(brand_name[:40])}")
    words = words or []
    for offset in range(0, len(words), 5):
        chunk = words[offset:offset + 5]
        for index, word in enumerate(chunk):
            text = " ".join(str(item.get("text", "")) for item in chunk[:index + 1]).strip()
            if not text:
                continue
            try:
                begin = max(0.0, float(word.get("start_seconds", start)) - start)
                next_start = float(chunk[index + 1].get("start_seconds", end)) if index + 1 < len(chunk) else float(chunk[-1].get("end_seconds", end))
            except (TypeError, ValueError):
                continue
            finish = min(duration, max(begin + 0.12, next_start - start))
            if finish > begin:
                lines.append(f"Dialogue: 1,{_ass_stamp(begin)},{_ass_stamp(finish)},Caption,,0,0,0,,{_ass_escape(text)}")
    destination.write_text("\n".join(lines), encoding="utf-8")


def _ass_stamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    whole = int(seconds)
    return f"{whole // 3600}:{(whole % 3600) // 60:02}:{whole % 60:02}.{int((seconds % 1) * 100):02}"


def _subtitle_path(path: Path) -> str:
    return str(path.resolve()).replace("\\", "/").replace(":", r"\:").replace("'", r"\'")


def _write_explainer_ass(captions: list[dict[str, Any]], scenes: list[dict[str, Any]], title: str,
                         brand_name: str, destination: Path, duration: float, accent: str,
                         caption_font: str = "Arial") -> None:
    lines = _ass_header(accent, caption_font)
    lines.append(f"Dialogue: 0,0:00:00.00,{_ass_stamp(min(duration, 5))},Title,,0,0,0,,{_ass_escape(title[:100])}")
    if brand_name:
        lines.append(f"Dialogue: 0,0:00:00.00,{_ass_stamp(duration)},Brand,,0,0,0,,{_ass_escape(brand_name[:40])}")
    ordered = sorted(scenes, key=lambda scene: float(scene.get("at_seconds", 0)))
    for index, scene in enumerate(ordered):
        begin = min(duration, max(0.0, float(scene.get("at_seconds", 0))))
        finish = min(duration, max(begin, float(ordered[index + 1].get("at_seconds", duration)) if index + 1 < len(ordered) else duration))
        if finish <= begin:
            continue
        text = str(scene.get("text", "")).strip()
        if text:
            lines.append(f"Dialogue: 0,{_ass_stamp(begin)},{_ass_stamp(finish)},Scene,,0,0,0,,{_ass_escape(text[:300])}")
        code = str(scene.get("code", "")).strip()[:300]
        if code:
            steps = min(20, max(1, len(code)))
            for step in range(1, steps + 1):
                left = begin + (finish - begin) * (step - 1) / steps
                right = begin + (finish - begin) * step / steps
                prefix = code[:max(1, round(len(code) * step / steps))]
                lines.append(f"Dialogue: 1,{_ass_stamp(left)},{_ass_stamp(right)},Code,,0,0,0,,{_ass_escape(prefix)}")
    for caption in captions:
        begin = float(caption.get("start_seconds", caption.get("start", 0)))
        finish = float(caption.get("end_seconds", caption.get("end", 0)))
        text = str(caption.get("text", "")).strip()
        if text and finish > begin:
            lines.append(f"Dialogue: 2,{_ass_stamp(begin)},{_ass_stamp(min(duration, finish))},Caption,,0,0,0,,{_ass_escape(text)}")
    destination.write_text("\n".join(lines), encoding="utf-8")


def render_explainer(scenes: list[dict[str, Any]], captions: list[dict[str, Any]], title: str, output: Path, duration: int, brand: dict[str, Any] | None = None, audio_path: Path | None = None, check_cancelled: Callable[[], None] | None = None) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    intro_outro = bool((brand or {}).get("intro_path") or (brand or {}).get("outro_path"))
    main_output = output.with_name(f"{output.stem}.main.mp4") if intro_outro else output
    accent = ((brand or {}).get("caption_style", {}).get("accent") or (brand or {}).get("colors", {}).get("primary", "#B9F66A")).lstrip("#")
    if len(accent) != 6:
        accent = "B9F66A"
    subtitle_file = output.with_suffix(".ass")
    caption_font = (brand or {}).get("caption_style", {}).get("font", "Arial")
    _write_explainer_ass(captions, scenes, title, ((brand or {}).get("name") or "").strip(), subtitle_file, float(duration), accent, caption_font)
    filters = [f"subtitles='{_subtitle_path(subtitle_file)}'"]
    args = [ffmpeg_path(), "-y", "-f", "lavfi", "-i", f"color=c=0x101512:s=720x1280:r=30:d={duration}"]
    audio_index = None
    if audio_path and audio_path.is_file():
        args += ["-i", str(audio_path)]
        audio_index = 1
    logo = (brand or {}).get("logo_path")
    has_logo = bool(logo and Path(logo).is_file())
    graph_file = output.with_name(f"{output.stem}.filtergraph")
    if has_logo:
        logo_index = 2 if audio_index is not None else 1
        args += ["-loop", "1", "-framerate", "30", "-i", str(logo)]
        graph = f"[0:v]{','.join(filters)}[base];[{logo_index}:v]scale=110:-1,format=rgba[logo];[base][logo]overlay=x=W-w-24:y=235:shortest=1:format=auto[vout]"
    else:
        graph = f"[0:v]{','.join(filters)}[vout]"
    _write_filter_graph(graph_file, graph)
    args += ["-/filter_complex", str(graph_file), "-map", "[vout]"]
    args += ["-t", str(duration), "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p"]
    if audio_index is not None:
        speed = 1.0
        try:
            with wave.open(str(audio_path), "rb") as audio:
                speech_seconds = audio.getnframes() / audio.getframerate()
            speed = max(0.5, speech_seconds / max(1, duration))
        except (wave.Error, OSError, ZeroDivisionError):
            pass
        tempo_filters = []
        while speed > 2.0:
            tempo_filters.append("atempo=2.0")
            speed /= 2.0
        tempo_filters.append(f"atempo={speed:.4f}")
        args += ["-map", f"{audio_index}:a:0", "-c:a", "aac", "-af", ",".join(tempo_filters)]
    else:
        args += ["-an"]
    args += ["-movflags", "+faststart", str(main_output)]
    try:
        run_ffmpeg(args, check_cancelled)
    finally:
        graph_file.unlink(missing_ok=True)
        subtitle_file.unlink(missing_ok=True)
    return _compose_brand_segments(main_output, output, brand, check_cancelled)
