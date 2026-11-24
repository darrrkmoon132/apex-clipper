import os
import re
import glob
import zipfile
import subprocess
import logging
from typing import Generator, List, Tuple, Optional

import numpy as np
from rapidocr_onnxruntime import RapidOCR
import gradio as gr
import yt_dlp

# ====================== ЛОГИРОВАНИЕ ======================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger("apex-clipper")

# ====================== НАСТРОЙКИ ======================
CROP = (600, 200, 660, 620)          # width, height, x, y  (1080p)
FPS = 3
DEBOUNCE = 4.0
MERGE_GAP = 20.0
PRE_SECONDS_DEFAULT = 10
POST_SECONDS_DEFAULT = 15
OUTPUT_DIR = "/app/clips"

KEYWORDS = [
    "НОКАУТ", "HOKAVT", "HOKAYT", "HOKA",
    "УБИЙСТВО", "УНИЧТОЖЕНИЕ", "YHMYTOXKEHME", "YHM4TOXKEHE",
    "YHM4", "YHMY", "НОКДАУН", "HOKAAYH", "УНИЧТОЖ", "YHMYTO"
]

os.makedirs(OUTPUT_DIR, exist_ok=True)


# ====================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ======================
def get_video_info(url: str) -> Tuple[float, Optional[str], str]:
    """Надёжно получает длительность, прямой URL потока и название VOD."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "format": "best[height<=1080]/best",
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)

        duration = float(info.get("duration") or 0)
        title = str(info.get("title") or "vod")[:60]

        # Самый надёжный способ получить URL
        stream_url = None
        formats = info.get("formats") or []

        # Предпочитаем mp4 / 1080p / 720p
        preferred = []
        for f in formats:
            if f.get("vcodec") in (None, "none"):
                continue
            if not f.get("url"):
                continue
            height = f.get("height") or 0
            ext = f.get("ext") or ""
            preferred.append((height, ext == "mp4", f["url"]))

        if preferred:
            preferred.sort(key=lambda x: (x[0], x[1]), reverse=True)
            stream_url = preferred[0][2]

        if not stream_url:
            stream_url = info.get("url")

        return duration, stream_url, title


def sec_to_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def has_keyword(text: str) -> bool:
    cleaned = text.upper().replace(" ", "").replace("\n", "")
    return any(kw in cleaned for kw in KEYWORDS)


def cut_clip(stream_url: str, start: float, end: float, out_path: str) -> bool:
    """Вырезает клип с -c copy. Возвращает True если файл успешно создан."""
    duration = max(0.1, end - start)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}",
        "-i", stream_url,
        "-t", f"{duration:.3f}",
        "-c", "copy",
        "-avoid_negative_ts", "make_zero",
        "-movflags", "+faststart",
        out_path
    ]
    try:
        result = subprocess.run(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=120
        )
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 5000:
            return True
        else:
            logger.warning(f"Не удалось создать клип: {out_path}")
            if os.path.exists(out_path):
                os.remove(out_path)
            return False
    except Exception as e:
        logger.error(f"Ошибка при нарезке клипа: {e}")
        return False


def clear_output_dir():
    for f in glob.glob(os.path.join(OUTPUT_DIR, "*")):
        try:
            os.remove(f)
        except Exception:
            pass


# ====================== ГЛАВНАЯ ФУНКЦИЯ ======================
def process_vod(
    url: str,
    pre_sec: int,
    post_sec: int,
    progress=gr.Progress()
) -> Generator[Tuple[str, List[str]], None, None]:

    if not url or "twitch.tv" not in url.lower():
        yield "❌ Вставь правильную ссылку на Twitch VOD", []
        return

    clear_output_dir()

    progress(0.0, desc="Получаем информацию о VOD...")
    yield "🔍 Получаем информацию о VOD...", []

    try:
        duration, stream_url, title = get_video_info(url)
    except Exception as e:
        logger.exception("Ошибка yt-dlp")
        yield f"❌ Ошибка получения информации: {e}", []
        return

    if not stream_url:
        yield "❌ Не удалось получить поток видео. Попробуй другую ссылку.", []
        return

    if duration <= 0:
        yield "❌ Не удалось определить длительность VOD", []
        return

    safe_title = re.sub(r'[^\w\-_\.]', '_', title)[:40]
    logger.info(f"Начинаем обработку: {title} ({sec_to_time(duration)})")

    ocr = RapidOCR()

    # crop → fps (правильный порядок)
    crop_filter = f"crop={CROP[0]}:{CROP[1]}:{CROP[2]}:{CROP[3]},fps={FPS}"

    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-an",                      # звук не нужен
        "-i", stream_url,
        "-vf", crop_filter,
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "-"
    ]

    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=10**8
    )

    frame_w, frame_h = CROP[0], CROP[1]
    frame_size = frame_w * frame_h * 3

    frame_idx = 0
    last_event_time = -999.0
    series_start: Optional[float] = None
    series_end: Optional[float] = None
    saved_files: List[str] = []
    log_lines = [f"🚀 Старт: {title}", f"⏱ Длительность: {sec_to_time(duration)}"]

    def flush_series():
        nonlocal series_start, series_end
        if series_start is None:
            return

        clip_start = max(0.0, series_start - pre_sec)
        clip_end = min(duration, series_end + post_sec)

        fname = f"{safe_title}_clip{len(saved_files)+1:02d}_{sec_to_time(clip_start).replace(':', '-')}.mp4"
        out_path = os.path.join(OUTPUT_DIR, fname)

        if cut_clip(stream_url, clip_start, clip_end, out_path):
            saved_files.append(out_path)
            log_lines.append(f"✅ Клип #{len(saved_files)}: {sec_to_time(clip_start)} → {sec_to_time(clip_end)}")
            logger.info(f"Сохранён клип: {fname}")

        series_start = None
        series_end = None

    try:
        while True:
            raw = proc.stdout.read(frame_size)
            if len(raw) < frame_size:
                break

            frame = np.frombuffer(raw, dtype=np.uint8).reshape((frame_h, frame_w, 3))
            current_sec = frame_idx / FPS

            # Серия закончилась → сразу режем
            if series_end is not None and (current_sec - series_end > MERGE_GAP):
                flush_series()

            # OCR
            result, _ = ocr(frame)
            text = " ".join([line[1] for line in result]) if result else ""

            if has_keyword(text):
                if current_sec - last_event_time >= DEBOUNCE:
                    last_event_time = current_sec
                    log_lines.append(f"🎯 Событие @ {sec_to_time(current_sec)}")

                    if series_start is None:
                        series_start = current_sec
                    series_end = current_sec

            frame_idx += 1

            # Обновление UI каждые 3 секунды стрима
            if frame_idx % (FPS * 3) == 0:
                pct = min(0.98, current_sec / duration)
                progress(
                    pct,
                    desc=f"{sec_to_time(current_sec)} / {sec_to_time(duration)} | Клипов: {len(saved_files)}"
                )
                header = (
                    f"⏱ {sec_to_time(current_sec)} / {sec_to_time(duration)} "
                    f"({pct*100:.1f}%) | Клипов: {len(saved_files)}\n"
                    + "=" * 52
                )
                yield header + "\n" + "\n".join(log_lines[-16:]), saved_files.copy()

    finally:
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        flush_series()

    # Создаём ZIP
    if saved_files:
        zip_name = f"{safe_title}_ALL_CLIPS.zip"
        zip_path = os.path.join(OUTPUT_DIR, zip_name)
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fpath in saved_files:
                zf.write(fpath, arcname=os.path.basename(fpath))
        saved_files.append(zip_path)
        log_lines.append(f"📦 Создан архив: {zip_name}")

    final_text = (
        f"🏁 ГОТОВО!\n\n"
        f"VOD: {title}\n"
        f"Длительность: {sec_to_time(duration)}\n"
        f"Найдено клипов: {len(saved_files) - (1 if saved_files and saved_files[-1].endswith('.zip') else 0)}\n\n"
        + "\n".join(log_lines)
    )

    progress(1.0, desc="Готово!")
    yield final_text, saved_files


# ====================== ИНТЕРФЕЙС ======================
with gr.Blocks(
    title="Apex Legends Auto-Clipper",
    css="""
    .gradio-container {max-width: 1200px !important}
    """
) as demo:

    gr.Markdown(
        """
        # 🎯 Apex Legends Auto-Clipper
        Автоматическая нарезка ноков и киллов из Twitch VOD (русский интерфейс Apex).
        Клипы сохраняются сразу + общий ZIP-архив.
        """
    )

    with gr.Row():
        with gr.Column(scale=3):
            url_input = gr.Textbox(
                label="Ссылка на Twitch VOD",
                placeholder="https://www.twitch.tv/videos/2834835087",
                max_lines=1
            )
            with gr.Row():
                pre_slider = gr.Slider(
                    minimum=5, maximum=30, value=PRE_SECONDS_DEFAULT, step=1,
                    label="Секунд ДО события"
                )
                post_slider = gr.Slider(
                    minimum=5, maximum=30, value=POST_SECONDS_DEFAULT, step=1,
                    label="Секунд ПОСЛЕ события"
                )
            start_btn = gr.Button("🚀 Запустить анализ", variant="primary", size="lg")
            output_log = gr.Textbox(
                label="Процесс работы",
                lines=20,
                interactive=False,
                show_copy_button=True
            )

        with gr.Column(scale=2):
            files_output = gr.File(
                label="📥 Готовые клипы + ZIP",
                file_count="multiple",
                height=420
            )

    start_btn.click(
        fn=process_vod,
        inputs=[url_input, pre_slider, post_slider],
        outputs=[output_log, files_output],
        show_progress="full"
    )

    gr.Markdown(
        """
        ---
        **Как это работает:**  
        Анализируется только центральная нижняя плашка (нокаут / уничтожение).  
        Дебаунс 4 сек · склейка серий ≤ 20 сек · нарезка с `-c copy`.
        """
    )


# ====================== ЗАПУСК ======================
if __name__ == "__main__":
    demo.queue(default_concurrency_limit=1).launch(
        server_name="0.0.0.0",
        server_port=7860,
        theme=gr.themes.Soft(),
        show_error=True
    )
