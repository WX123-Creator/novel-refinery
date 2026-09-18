"""
小说提炼工坊 - Gradio 主界面
===========================
极简单页、成本防护、断点续跑、实时费用显示
"""

import os
import threading
import time
from pathlib import Path

import gradio as gr
from gradio import FileData

from config import ALL_TIERS, get_tier_models_for_dropdown, MAX_TOTAL_TOKENS
from novel_processor import process_novel, load_checkpoint, clear_checkpoint


# ============================================================
# 全局状态
# ============================================================

class AppState:
    """应用状态管理"""
    def __init__(self):
        self.processing = False
        self.cancel_requested = False
        self.result = None
        self.status_history = []
        self.progress = 0
        self.total_blocks = 0
        self.processed_blocks = 0

    def reset(self):
        self.processing = False
        self.cancel_requested = False
        self.result = None
        self.status_history = []
        self.progress = 0
        self.total_blocks = 0
        self.processed_blocks = 0


app_state = AppState()


# ============================================================
# 处理回调
# ============================================================

def progress_callback(title: str, meta: dict):
    """每块处理完成的回调"""
    app_state.processed_blocks += 1
    pct = int(app_state.processed_blocks / max(app_state.total_blocks, 1) * 100)
    app_state.progress = pct


def status_callback(msg: str):
    """状态更新回调"""
    app_state.status_history.append(msg)
    # 保留最近 50 条
    if len(app_state.status_history) > 50:
        app_state.status_history = app_state.status_history[-50:]


# ============================================================
# 核心处理函数（线程中运行）
# ============================================================

def run_processing(text: str, tier_key: str, model_id: str, budget_tokens: int):
    """在后台线程中运行处理"""
    try:
        app_state.processing = True
        app_state.cancel_requested = False
        app_state.progress = 0
        app_state.processed_blocks = 0
        app_state.status_history = []

        # 临时覆盖预算
        import config as cfg
        original_budget = cfg.MAX_TOTAL_TOKENS
        cfg.MAX_TOTAL_TOKENS = budget_tokens

        try:
            result = process_novel(
                text=text,
                tier_key=tier_key,
                model_id=model_id,
                progress_callback=progress_callback,
                status_callback=status_callback,
                budget_check=True,
                book_name=book_name,
            )
            app_state.result = result
            status_callback("✅ 处理完成！")
        finally:
            cfg.MAX_TOTAL_TOKENS = original_budget

    except Exception as e:
        status_callback(f"❌ 处理出错: {str(e)}")
        # 降级：出错时从断点检查点抢救已提炼成果导出，避免成果丢失
        try:
            from novel_processor import load_checkpoint, export_from_blocks
            _ck = load_checkpoint()
            _blocks = _ck.get("blocks", []) if _ck else []
            _br = [r for r in (_ck.get("block_results", []) or []) if r]
            if _br:
                _files = export_from_blocks(_blocks, _br, book_output_dir(book_name))
                app_state.result = {
                    "files": _files,
                    "overview": "【降级导出】处理中途出错，已直接从已提炼的分块成果抢救导出（未触发 AI 汇总）。\n\n错误：%s" % str(e),
                    "cost_summary": "（降级导出，未完成 AI 汇总）",
                    "total_tokens": 0,
                    "total_cost": 0.0,
                }
            else:
                app_state.result = {"error": str(e)}
        except Exception:
            app_state.result = {"error": str(e)}
    finally:
        app_state.processing = False


# ============================================================
# Gradio 界面
# ============================================================

def build_ui():
    """构建 Gradio 界面"""
    with gr.Blocks(title="小说提炼工坊") as demo:
        # 标题
        gr.Markdown(
            """
            <div class="app-header">
                <h1>📖 小说提炼工坊</h1>
                <p>上传小说 TXT → 自动分卷/章 → AI 提炼 → 导出节拍级故事结构报告</p>
            </div>
            """
        )

        # 检查点提示
        checkpoint_md = gr.Markdown(_get_checkpoint_text())

        with gr.Row():
            with gr.Column(scale=1):
                # 左侧：输入区
                gr.Markdown("### 📂 输入")
                file_input = gr.File(
                    label="上传小说 TXT 文件",
                    file_types=[".txt"],
                    file_count="single",
                )

                with gr.Row():
                    tier_dropdown = gr.Dropdown(
                        label="模型档位",
                        choices=[
                            ("🆓 免费测试档", "free"),
                            ("⚖️ 性价比档", "balance"),
                            ("🏆 质量档", "quality"),
                        ],
                        value="free",
                        interactive=True,
                    )

                    model_dropdown = gr.Dropdown(
                        label="具体模型",
                        choices=get_tier_models_for_dropdown("free"),
                        value=get_tier_models_for_dropdown("free")[0][1] if get_tier_models_for_dropdown("free") else "glm-4-7-251222",
                        interactive=True,
                        allow_custom_value=True,
                    )

                # Token 预算（上限动态读取 config.MAX_TOTAL_TOKENS）
                budget_slider = gr.Slider(
                    label=f"Token 预算上限",
                    minimum=100_000,
                    maximum=MAX_TOTAL_TOKENS,
                    value=min(1_000_000, MAX_TOTAL_TOKENS),
                    step=100_000,
                    info=f"当前系统上限: {MAX_TOTAL_TOKENS:,}",
                )

                # 启动按钮
                with gr.Row():
                    start_btn = gr.Button("🚀 开始提炼", variant="primary", size="lg")
                    cancel_btn = gr.Button("⏹ 停止", variant="stop", size="lg", interactive=False)

                # 隐私提示
                gr.Markdown(
                    """
                    <div style="background:#f0fdf4; border:1px solid #bbf7d0; border-radius:6px; padding:8px; margin-top:8px; font-size:0.85rem; color:#166534;">
                    🔒 <strong>隐私安全</strong>：所有数据仅在本地处理，不上传至任何外部服务器。
                    </div>
                    """
                )

            with gr.Column(scale=2):
                # 右侧：状态与输出
                gr.Markdown("### 📊 处理状态")

                with gr.Row():
                    cost_box = gr.HTML(
                        value="""
                        <div class="cost-box">
                            <strong>💰 费用统计</strong><br>
                            调用次数: 0 | 总 Tokens: 0 | 费用: ¥0.000000
                        </div>
                        """
                    )

                progress_bar = gr.HTML(
                    value="""
                    <div style="background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:12px;">
                        <strong>📈 处理进度</strong><br>
                        <span id="progress-text">等待开始...</span>
                    </div>
                    """
                )

                status_output = gr.Markdown("等待开始...")

                # 输出区
                gr.Markdown("### 📦 导出文件")
                file_links = gr.HTML(
                    value="<p style='color:#9ca3af;'>处理完成后，节拍级故事结构报告将在此显示</p>"
                )

                # 文本预览区（仅保留全书故事结构一份输出）
                overview_box = gr.Markdown("等待处理...")

        gr.Markdown(
            """
            <footer>
                小说提炼工坊 · 本地运行 · 数据安全 · 基于 AI 大模型
            </footer>
            """
        )

        # ============================================================
        # 事件绑定（必须在 gr.Blocks 上下文内）
        # ============================================================

        def on_tier_change(tier_key):
            """档位切换时更新模型列表"""
            models = get_tier_models_for_dropdown(tier_key)
            default = models[0][1] if models else "glm-4-7-251222"
            return gr.Dropdown(choices=models, value=default)

        tier_dropdown.change(
            fn=on_tier_change,
            inputs=[tier_dropdown],
            outputs=[model_dropdown],
        )

        def on_start(file_data, tier_key, model_id, budget):
            """开始处理"""
            if file_data is None:
                raise gr.Error("请先上传 TXT 文件")

            # 读取文件 - Gradio 6.x 返回 FileData 对象
            file_path = file_data.path if isinstance(file_data, FileData) else file_data
            text = Path(file_path).read_text("utf-8", errors="replace")
            book_name = (Path(file_data.orig_name).stem if isinstance(file_data, FileData) and getattr(file_data, "orig_name", None) else Path(file_path).stem).strip()
            if len(text) < 100:
                raise gr.Error("文件内容过短，请上传完整的小说文本（至少 100 字）")

            # 检查是否有检查点
            checkpoint = load_checkpoint()
            checkpoint_msg = ""
            if checkpoint:
                completed = len(checkpoint.get("completed_indices", []))
                total = len(checkpoint.get("blocks", []))
                if completed > 0 and completed < total:
                    checkpoint_msg = f"⚠️ 发现未完成的检查点（{completed}/{total}），将从断点处继续处理"

            app_state.reset()

            # 启动后台线程
            thread = threading.Thread(
                target=run_processing,
                args=(text, tier_key, model_id, budget, book_name),
                daemon=True,
            )
            thread.start()

            return (
                gr.Button(interactive=False),
                gr.Button(interactive=True),
                "<div class='cost-box'><strong>💰 费用统计</strong><br>⏳ 处理中...</div>",
                f"🚀 开始处理...{checkpoint_msg}",
                "<div style='background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:12px;'><strong>📈 处理进度</strong><br>⏳ 处理中... (0%)</div>",
            )

        def on_cancel():
            """取消处理"""
            app_state.cancel_requested = True
            status_callback("⏹ 用户请求停止...")
            return (
                gr.Button(interactive=True),
                gr.Button(interactive=False),
            )

        def refresh_status():
            """轮询刷新状态"""
            if not app_state.processing:
                # 处理完成或出错
                if app_state.result:
                    result = app_state.result
                    if "error" in result:
                        return _build_error_output(result)
                    return _build_success_output(result)
                return _build_idle_output()

            # 处理中
            progress_val = app_state.progress / 100.0
            status_text = "\n".join(app_state.status_history[-10:])
            status_md = f"```\n{status_text}\n```"
            return _build_processing_output(progress_val, status_md)

        # 状态轮询
        poll_trigger = gr.Timer(value=0.5, active=True)

        start_btn.click(
            fn=on_start,
            inputs=[file_input, tier_dropdown, model_dropdown, budget_slider],
            outputs=[start_btn, cancel_btn, cost_box, status_output, progress_bar],
        )

        cancel_btn.click(
            fn=on_cancel,
            inputs=[],
            outputs=[start_btn, cancel_btn],
        )

        poll_trigger.tick(
            fn=refresh_status,
            inputs=[],
            outputs=[cost_box, status_output, progress_bar, file_links, overview_box, start_btn, cancel_btn],
        )

    return demo


def _get_checkpoint_text() -> str:
    """获取检查点提示文本"""
    checkpoint = load_checkpoint()
    if checkpoint:
        completed = len(checkpoint.get("completed_indices", []))
        total = len(checkpoint.get("blocks", []))
        if completed > 0 and completed < total:
            return f"⚠️ **发现未完成的处理进度**（{completed}/{total} 块），重新开始后将从断点续跑"
    return ""


def _build_idle_output():
    """空闲状态输出"""
    return (
        "<div class='cost-box'><strong>💰 费用统计</strong><br>调用次数: 0 | 总 Tokens: 0 | 费用: ¥0.000000</div>",
        "等待开始...",
        "<div style='background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:12px;'><strong>📈 处理进度</strong><br>等待开始...</div>",
        "<p style='color:#9ca3af;'>处理完成后，节拍级故事结构报告将在此显示</p>",
        "等待处理...",
        gr.Button(interactive=True),
        gr.Button(interactive=False),
    )


def _build_processing_output(progress_val, status_md):
    """处理中输出"""
    progress_pct = int(progress_val * 100)
    progress_html = f"<div style='background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:12px;'><strong>📈 处理进度</strong><br><div style='background:#e0e7ff; border-radius:4px; height:20px; width:100%;'><div style='background:#6366f1; border-radius:4px; height:20px; width:{progress_pct}%; text-align:center; color:white; font-size:12px; line-height:20px;'>{progress_pct}%</div></div></div>"
    return (
        "<div class='cost-box'><strong>💰 费用统计</strong><br>⏳ 处理中...</div>",
        status_md,
        progress_html,
        "<p style='color:#9ca3af;'>处理中，请稍候...</p>",
        "等待处理...",
        gr.Button(interactive=False),
        gr.Button(interactive=True),
    )


def _build_success_output(result):
    """成功输出"""
    # 费用统计
    cost_html = f"""
    <div class='cost-box'>
        <strong>💰 费用统计</strong><br>
        调用次数: {result.get('total_tokens', 0) and '?'} |
        总 Tokens: {result.get('total_tokens', 0):,} |
        费用: ¥{result.get('total_cost', 0):.6f}
    </div>
    """

    # 状态信息
    status_text = "\n".join(app_state.status_history[-5:])
    status_md = f"```\n{status_text}\n```\n\n**{result.get('cost_summary', '')}**"

    # 文件链接 - 使用 Gradio 的 /file= 路径
    files = result.get("files", {})
    if files:
        file_html = "<div style='display:flex; flex-wrap:wrap; gap:8px;'>"
        for name, path in files.items():
            abs_path = os.path.abspath(path)
            file_html += f'<a href="/file={abs_path}" target="_blank" class="file-link" style="background:#f0f9ff; border:1px solid #bae6fd; border-radius:6px; padding:8px 16px; text-decoration:none; color:#0369a1;">📄 {name}</a>'
        file_html += "</div>"
    else:
        file_html = "<p style='color:#9ca3af;'>文件生成失败</p>"

    # 全书故事
    overview = result.get("overview", "无内容")
    if len(overview) > 2000:
        overview = overview[:2000] + "\n\n...（内容较长，请下载完整文件查看）"
    overview_md = f"```\n{overview}\n```"

    return (
        cost_html,
        status_md,
        "<div style='background:#f0f9ff; border:1px solid #bae6fd; border-radius:8px; padding:12px;'><strong>📈 处理进度</strong><br>✅ 已完成 (100%)</div>",
        file_html,
        overview_md,
        gr.Button(interactive=True),
        gr.Button(interactive=False),
    )


def _build_error_output(result):
    """错误输出"""
    return (
        "<div class='cost-box'><strong>💰 费用统计</strong><br>❌ 处理出错</div>",
        f"❌ **错误**: {result.get('error', '未知错误')}",
        "<div style='background:#fef2f2; border:1px solid #fecaca; border-radius:8px; padding:12px;'><strong>📈 处理进度</strong><br>❌ 处理失败</div>",
        "<p style='color:#ef4444;'>处理失败</p>",
        "处理失败",
        gr.Button(interactive=True),
        gr.Button(interactive=False),
    )


# ============================================================
# 启动入口
# ============================================================

if __name__ == "__main__":
    import sys
    sys.dont_write_bytecode = True

    # Fix Windows GBK encoding issue for console output
    # Reconfigure stdout/stderr to UTF-8 to avoid 'gbk' codec errors
    # when printing emoji or special characters
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass  # Older Python versions may not support reconfigure

    port = int(os.environ.get("DEPLOY_RUN_PORT", 5000))

    demo = build_ui()
    demo.queue(default_concurrency_limit=5)
    demo.launch(
        server_name="0.0.0.0",
        server_port=port,
        allowed_paths=["output", "checkpoints"],
        theme=gr.themes.Soft(
            primary_hue="blue",
            secondary_hue="slate",
            font=gr.themes.GoogleFont("Noto Sans SC"),
        ),
        css="""
        .app-header { text-align: center; margin-bottom: 1.5rem; }
        .app-header h1 { font-size: 1.8rem; font-weight: 700; margin-bottom: 0.3rem; }
        .app-header p { color: #6b7280; font-size: 0.95rem; }
        .cost-box { background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px; padding: 12px; }
        .status-box { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 12px; max-height: 300px; overflow-y: auto; }
        .file-link { display: inline-block; margin: 0.25rem 0.5rem; }
        footer { text-align: center; color: #9ca3af; font-size: 0.8rem; margin-top: 2rem; }
        """,
    )