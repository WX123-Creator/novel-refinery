# 小说提炼工坊 - AGENTS.md

## 项目概览
Python + Gradio 单页 Web 工具，用于上传小说 TXT 文件，自动分卷/章，调用 LLM 提炼分析，导出 6 份结构化分析报告。

## 技术栈
- **后端**: Python 3.12
- **前端**: Gradio 6.26.0
- **LLM  SDK**: coze-coding-dev-sdk
- **包管理**: pip

## 项目结构
```
.
├── app.py              # Gradio 主界面
├── config.py           # 模型配置、定价、Token 预算
├── llm_service.py      # LLM 服务封装（调用、重试、费用追踪）
├── novel_processor.py  # 核心逻辑（分块、提炼、汇总、导出）
├── requirements.txt    # 依赖
├── .coze               # 运行配置
├── checkpoints/        # 检查点目录（断点续跑）
└── output/             # 导出文件目录
```

## 构建和运行命令
- **安装依赖**: `pip install -r requirements.txt`
- **运行**: `python app.py`（默认端口 5000，或通过 DEPLOY_RUN_PORT 环境变量指定）
- **构建**: pip install 即构建

## 模型配置
模型配置集中在 `config.py` 中，分为三档：
- `free`: 免费测试档（GLM-4.7）
- `balance`: 性价比档（GLM-4.7 / 豆包 Lite / MiniMax M2.5）
- `quality`: 质量档（豆包 Pro / Qwen3.5 Plus / GLM-5）

## 核心功能
1. **文件上传**: 上传小说 TXT 文件
2. **自动分块**: 按「第X卷」「第X章」等正则识别切分
3. **逐块提炼**: 每个块调用 LLM 提取人物、设定、事件、好词好句
4. **卷级汇总**: 同一卷的多块结果汇总
5. **全书总览**: 多卷结果生成全书总览
6. **导出 6 份文件**: 全书总览、各卷拆解、人物档案、设定体系、情节事件线、好词好句

## 成本防护机制
- Token 预算硬上限（config.MAX_TOTAL_TOKENS）
- 实时费用显示（累计调用次数、tokens、费用）
- 断点续跑（JSON 检查点保存/恢复）
- 单块重试上限 2 次

## 代码风格
- 全中文输出（面向用户界面）
- 类型注解（Python typing）
- 函数职责单一
- 全局状态通过 AppState 类管理