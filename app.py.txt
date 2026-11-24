import os
import re
import glob
import zipfile
import subprocess
import numpy as np
from rapidocr_onnxruntime import RapidOCR
import gradio as gr
import yt_dlp

# ====================== НАСТРОЙКИ ======================
CROP = (600, 200, 660, 620)          # width, height, x, y
FPS = 3
DEBOUNCE = 4.0
MERGE_GAP = 20.0
PRE_SECONDS = 10
POST_SECONDS = 15
OUTPUT_DIR = "/app/clips"

KEYWORDS = [
    "НОКАУТ", "HOKAVT", "HOKAYT", "HOKA",
    "УБИЙСТВО", "УНИЧТОЖЕНИЕ", "YHMYTOXKEHME", "YHM4TOXKEHE",
    "YHM4", "YHMY", "НОКДАУН", "HOKAAYH", "УНИЧТОЖ", "YHMYTO"
]

os.makedirs(OUTPUT_DIR, exist_ok=True)

def get_video_info(url: str):
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[height<=1080]/best",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration") or 0
        stream_url = info.get("url")
        if not stream_url and info.get("requested_formats"):
            stream_url = info["requested_formats"][0].get("url")
        title = info.get("title", "vod")[:50]
        return duration, stream_url, title

def sec_to_time(s: float) -> str:
    h = int(s // 3600)
    m = int((s % 3600) // 60)
    sec = int(s % 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"

def has_keyword(text: str) -> bool:
    text = text.upper().replace(" ", "")
    return any(kw in text for kw in KEYWORDS)

def cut_clip(stream_url: str, start: float, end: float, out_path: str):
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", str(max(0, start)),
        "-i", stream_url,
        "-t", str(end - start),
        "-c", "copy",
        "-avoid_negative_ts", "1",
        out_path
    ]
    subprocess.run(cmd)

def process_vod(url: str, pre_sec: int, post_sec: int):
    if not url or "twitch.tv" not in url.lower():
        yield "❌ Вставь правильную ссылку на Twitch VOD", []
        return

    # Очищаем папку от старых клипов перед новым запуском
    for old_f in glob.glob(os.path.join(OUTPUT_DIR, "*")):
        try:
            os.remove(old_f)
        except Exception:
            pass

    yield "🔍 Получаем информацию о VOD...", []
    try:
        duration, stream_url, title = get_video_info(url)
    except Exception as e:
        yield f"❌ Ошибка получения информации: {e}", []
        return

    if not stream_url:
        yield "❌ Не удалось получить поток видео", []
        return

    safe_title = re.sub(r'[^\w\-_\.]', '_', title)[:40]
    ocr = RapidOCR()

    crop_filter = f"fps={FPS},crop={CROP[0]}:{CROP[1]}:{CROP[2]}:{CROP[3]}"
    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-an", "-i", stream_url,
        "-vf", crop_filter,
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-"
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )

    frame_size = CROP[0] * CROP[1] * 3
    frame_idx = 0
    last_event_time = -999.0
    series_start = None
    series_end = None
    saved_files = []
    log_lines = [f"🚀 Старт: {title} ({sec_to_time(duration)})"]

    def flush_series():
        nonlocal series_start, series_end
        if series_start is None:
            return
        clip_start = max(0.0, series_start - pre_sec)
        clip_end = min(float(duration), series_end + post_sec) if duration > 0 else (series_end + post_sec)
        fname = f"{safe_title}_clip{len(saved_files)+1:02d}_{sec_to_time(clip_start).replace(':', '-')}.mp4"
        out_path = os.path.join(OUTPUT_DIR, fname)
        cut_clip(stream_url, clip_start, clip_end, out_path)
        if os.path.exists(out_path):
            saved_files.append(out_path)
            log_lines.append(f"✅ Клип #{len(saved_files)}: {sec_to_time(clip_start)} → {sec_to_time(clip_end)}")
        series_start = None
        series_end = None

    try:
        while True:
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break

            frame = np.frombuffer(raw, dtype=np.uint8).reshape((CROP[1], CROP[0], 3))
            current_sec = frame_idx / FPS

            if series_end is not None and (current_sec - series_end > MERGE_GAP):
                flush_series()

            result, _ = ocr(frame)
            text = " ".join([line[1] for line in result]) if result else ""

            if has_keyword(text):
                if current_sec - last_event_time >= DEBOUNCE:
                    last_event_time = current_sec
                    log_lines.append(f"🎯 Событие на {sec_to_time(current_sec)}")

                    if series_start is None:
                        series_start = current_sec
                        series_end = current_sec
                    else:
                        series_end = current_sec

            frame_idx += 1

            # Каждые 3 секунды стрима обновляем прогресс и список готовых файлов прямо на странице
            if frame_idx % (FPS * 3) == 0:
                pct = min(99.9, (current_sec / max(duration, 1)) * 100)
                header = f"⏱ Проверяем: {sec_to_time(current_sec)} / {sec_to_time(duration)} ({pct:.1f}%) | Клипов: {len(saved_files)}\n" + "="*45
                yield header + "\n" + "\n".join(log_lines[-18:]), saved_files

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            pass
        flush_series()

    # Упаковываем все клипы в один ZIP-архив для скачивания в 1 клик
    if saved_files:
        zip_path = os.path.join(OUTPUT_DIR, f"{safe_title}_ALL_CLIPS.zip")
        with zipfile.ZipFile(zip_path, "w") as zf:
            for f in saved_files:
                zf.write(f, os.path.basename(f))
        saved_files.append(zip_path)

    final = (
        f"🏁 ГОТОВО!\n"
        f"VOD: {title}\n"
        f"Длительность: {sec_to_time(duration)}\n"
        f"Сохранено клипов: {len(saved_files) - 1 if saved_files else 0}\n\n"
        + ("\n".join(log_lines) if log_lines else "Клипов не найдено")
    )
    yield final, saved_files

with gr.Blocks(title="Apex Highlights Cutter") as demo:
    gr.Markdown("## 🎯 Apex Legends Auto-Clipper (Twitch VOD)")
    gr.Markdown("Вставь ссылку на стрим → нажми **Запустить анализ**. Готовые клипы и общий ZIP-архив появятся справа по мере нарезки.")

    with gr.Row():
        with gr.Column(scale=2):
            url_input = gr.Textbox(
                label="Ссылка на Twitch VOD",
                placeholder="https://www.twitch.tv/videos/2834835087"
            )
            with gr.Row():
                pre_slider = gr.Slider(5, 30, value=PRE_SECONDS, step=1, label="Секунд ДО")
                post_slider = gr.Slider(5, 30, value=POST_SECONDS, step=1, label="Секунд ПОСЛЕ")
            start_btn = gr.Button("🚀 Запустить анализ", variant="primary")
            output_log = gr.Textbox(label="Процесс работы", lines=18, interactive=False)
        
        with gr.Column(scale=1):
            files_output = gr.File(label="📥 Готовые клипы и ZIP-архив (скачивайте прямо отсюда)")

    start_btn.click(
        fn=process_vod,
        inputs=[url_input, pre_slider, post_slider],
        outputs=[output_log, files_output]
    )

demo.queue(default_concurrency_limit=1).launch(
    server_name="0.0.0.0",
    server_port=7860,
    theme=gr.themes.Soft()
)
