# Silvaco Handbook MCP

[English](README.md)

一个 [MCP](https://modelcontextprotocol.io/)（Model Context Protocol）服务器，
让 AI 助手能够快速、限量地检索 **Silvaco TCAD 官方手册** 和
**官方 Deckbuild 案例输入卡**，避免把上千页 PDF 塞进对话上下文。

服务器把 MinerU 转换后的手册 Markdown 索引到本地 SQLite FTS5 数据库，
通过 stdio 提供上下文友好的搜索/阅读工具。每个手册章节都保留了原始 PDF
页码范围，方便回溯原文。

## 功能特性

- **手册语料** —— Silvaco 手册（Deckbuild、Victory Device、Victory
  Process 等）的章节级索引，保留原始 PDF 页码。
- **案例语料** —— Silvaco 安装目录下官方 `.in` 案例卡
  （`examples/deckbuild/<版本>`）的全文索引。
- **限量输出** —— 所有工具都有硬上限（搜索 ≤ 30 条、单次读取 ≤ 20000
  字符、超长用 `offset` 分页），保证 LLM 上下文精简。
- **增量索引** —— 源文件变更（按大小/mtime 指纹判断）后，索引在服务启动时
  自动增量重建。

## 工具列表

| 工具 | 说明 |
|---|---|
| `handbook_list_manuals` | 列出已索引手册、章节数与源文件路径 |
| `handbook_toc` | 手册章节标题（即目录），支持标题过滤 |
| `handbook_search` | 全文搜索，返回章节 id、标题、PDF 页码范围、高亮摘要 |
| `handbook_read` | 按章节 id 读取全文，超长章节用 `offset` 续读 |
| `examples_list` | 列出官方案例卡（名称/分类/描述） |
| `examples_search` | 对案例名称、描述、全文做全文搜索 |
| `examples_read` | 按名称读取案例卡全文（部分名可匹配，歧义时列出候选） |

## 环境要求

- Python ≥ 3.10
- Python 包：`mcp`、`pymupdf`（见 `requirements.txt`）
- [MinerU Open API CLI](https://github.com/opendatalab/MinerU)
  （`mineru-open-api`）及 API token —— 仅在运行 `convert_mineru.py` 时需要
- Silvaco 手册 PDF；（可选）Silvaco 安装目录，用于索引官方案例

安装依赖：

```bash
pip install -r requirements.txt
```

## 快速上手

### 1. 将手册 PDF 转换为 Markdown

MinerU 精确提取单次限制 200 页，因此 `convert_mineru.py` 按页码分块处理，
在块边界插入 `<!-- pdf pages S-E -->` 注释，最后合并为每本手册一个
Markdown 文件。支持断点续传：已有分块会被跳过。

```bash
export MINERU_TOKEN=<你的-mineru-token>
export SILVACO_PDF_DIR=/path/to/silvaco/handbook/pdfs
export SILVACO_MD_DIR=/path/to/markdown/output

# 转换 SILVACO_PDF_DIR 下所有 PDF（或用 --only 只转一本）
python convert_mineru.py
python convert_mineru.py --only deckbuild_users1 --chunk-size 200
```

输出结构：

```
<SILVACO_MD_DIR>/<manual>/chunks/p001-200.md   # 每个分块一个文件
<SILVACO_MD_DIR>/<manual>/chunks/images/       # 提取的图片
<SILVACO_MD_DIR>/<manual>/<manual>.md          # 合并文件，由服务器索引
```

### 2. 构建索引

```bash
python server.py --build          # 增量构建
python server.py --build --force  # 全量重建
```

服务启动时若检测到源文件变化也会自动增量重建，因此此步骤可选。

### 3. 注册 MCP 服务器

将以下配置加入你的 MCP 客户端。

**Kimi Code / Claude Code（`.kimi-code/mcp.json` 或 `.claude/mcp.json`）：**

```json
{
  "mcpServers": {
    "silvaco-handbook": {
      "command": "python",
      "args": ["/path/to/Silvaco_MCP/server.py"],
      "env": {
        "SILVACO_MD_DIR": "/path/to/markdown/output",
        "SILVACO_EXAMPLES_DIR": "C:/Silvaco/examples/deckbuild/5.2.40.R"
      }
    }
  }
}
```

**Claude Desktop（`claude_desktop_config.json`）：** 使用相同的
`mcpServers` 配置块。

**Cursor：** 设置 → MCP → 添加相同的 command/args/env。

### 4. 开始提问

连接成功后，助手可以带手册出处地回答问题，例如：

- *"Atlas 中 Selberherr 碰撞电离模型怎么配置？"*
  → `handbook_search("impact ionization Selberherr")` → `handbook_read(...)`
- *"找一个量子阱激光器增益仿真的官方案例。"*
  → `examples_search("quantum well laser optical gain")` → `examples_read(...)`

## 配置说明

所有路径均通过环境变量设置：

| 变量 | 默认值 | 用途 |
|---|---|---|
| `SILVACO_MD_DIR` | `<repo>/../../mineru-output/silvaco` | 手册 Markdown 根目录（`<manual>/<manual>.md`） |
| `SILVACO_CACHE_DB` | `<repo>/.cache/handbooks_md.db` | SQLite FTS5 缓存位置 |
| `SILVACO_EXAMPLES_DIR` | *（不设置则禁用案例工具）* | 官方 deckbuild 案例树根目录 |
| `SILVACO_PDF_DIR` | *（vault 目录结构默认值）* | 手册 PDF 目录（`convert_mineru.py` 使用） |
| `SILVACO_MCP_CONFIG` | *（vault 目录结构默认值）* | 用于读取 `MINERU_TOKEN` 的 mcp.json（可选） |
| `MINERU_TOKEN` | — | MinerU Open API token（优先级高于配置文件） |
| `MINERU_CLI` | `mineru-open-api` | MinerU CLI 可执行文件路径/名称 |

## 使用示例

典型的智能体工作流：

```
handbook_list_manuals()
→ [{"manual": "deckbuild_users1", "sections": 481, ...}, ...]

handbook_search("impact ionization Selberherr", manual="victorydevice")
→ [{"section_id": 512, "section": "3.7.4 Impact Ionization Models",
    "pdf_pages": "201-400", "snippet": "... **Selberherr** ..."}, ...]

handbook_read("victorydevice", section_id=512)
→ "===== victorydevice [512] 3.7.4 Impact Ionization Models (pdf p.201-400) =====
   ... 章节全文 ..."

handbook_read("victorydevice", section_id=512, offset=12000)   # 续读超长章节

examples_search("quantum well laser optical gain", category="Opto")
→ [{"name": "optoex14", "category": "Technology/Opto_and_Photonics",
    "description": "Quantum Well Laser ...", "snippet": "..."}, ...]

examples_read("optoex14")
→ "===== example optoex14 [...] =====\n# Quantum Well Laser ...\ngo atlas ..."
```

## 维护

- 手册 PDF 更新后 → 重跑 `python convert_mineru.py`（增量转换）；索引会在
  下次服务启动时自动重建，或手动 `python server.py --build`。
- `SILVACO_EXAMPLES_DIR` 下案例变更 → 启动时自动增量更新（已删除的案例
  会同步移除）。
- `.cache/` 是派生数据，随时可以安全删除。
