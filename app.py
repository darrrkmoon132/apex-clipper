# ====================== ИНТЕРФЕЙС ======================
with gr.Blocks(title="Apex Legends Auto-Clipper") as demo:

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
                interactive=False
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
        css=".gradio-container {max-width: 1200px !important}",
        show_error=True
    )
