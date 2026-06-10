# AI RPA Starter

一个省心版 AI + RPA 项目骨架：

- Robot Framework 负责可审计任务入口。
- Python 负责结构化 workflow、AI 决策、执行器和测试。
- Playwright 负责真实浏览器操作。
- Mimo 负责截图/DOM 辅助识别、节点生成和失败修复。

## 快速开始

```bash
cd ai-rpa-starter
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
python -m ai_rpa.cli run workflows/dianxiaomi_draft_demo.json
```

也可以用 Robot Framework 入口：

```bash
robot -d reports robots/main.robot
```

本地开发常用命令：

```bash
make test
make validate
make run-local
make run
make robot
make app
make desktop
```

`make run-local` 会打开系统 Chrome 跑本地登录 demo，用来验证真实浏览器执行链路和验证码保护。

`make app` 会启动本地前台应用：

```text
http://127.0.0.1:8765
```

`make desktop` 会启动桌面版窗口，内部仍使用同一套本地 Web 应用和 workflow 执行器。

## 打包桌面应用

桌面版使用 pywebview 打开原生窗口，用 PyInstaller 打包。macOS 和 Windows 需要分别在对应系统上打包：

```bash
# macOS
make package-mac

# Windows
make package-windows
```

Windows 没有 `make` 时，用 PowerShell：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\build_windows.ps1
```

Windows 桌面打包会安装 `requirements-desktop.txt`。这个依赖集不包含
`robotframework-browser`，可以避开 Windows 上常见的 `grpcio` wheel 冲突。
只有要跑 Robot Framework 示例时才需要完整的 `requirements.txt`。

Windows 版产物：

```text
dist\AI RPA Starter\AI RPA Starter.exe
```

打包产物在 `dist/`。首次启动桌面版时，应用会把内置 workflow 复制到用户可写目录：

- macOS: `~/Library/Application Support/AI RPA Starter/`
- Windows: `%APPDATA%\AI RPA Starter\`

`.env` 和 `browser-profile/` 不会被打进安装包。请在用户数据目录或启动目录放置 `.env`。

打包命令会设置 `PLAYWRIGHT_BROWSERS_PATH=0`，把 Playwright Chromium 一起放进桌面应用依赖里。若 `AI_RPA_BROWSER_EXECUTABLE` 留空，应用会使用随包 Chromium；若配置了浏览器路径，则优先使用指定的系统 Chrome。

Windows 的用户配置目录是：

```text
%APPDATA%\AI RPA Starter\.env
```

如果 `.env` 是从 macOS 复制过去的，请把浏览器路径改成 Windows 路径，或直接留空让 Playwright 使用自带 Chromium：

```bash
AI_RPA_BROWSER_EXECUTABLE=
# 或
AI_RPA_BROWSER_EXECUTABLE=C:\Program Files\Google\Chrome\Application\chrome.exe
```

## 项目结构

```text
ai-rpa-starter/
  workflows/              # 结构化 RPA 流程
  robots/                 # Robot Framework 任务入口
  prompts/                # 给 Mimo 的 RPA 专用提示词
  src/ai_rpa/             # Python 执行器和 AI 能力
  tests/                  # 本地单元测试
```

## 设计原则

1. Workflow 必须一步一步可审计，避免隐藏宏节点。
2. 执行器必须真实操作浏览器，不允许只写“成功日志”。
3. 验证码默认只检测，不点击、不聚焦、不识别。
4. AI 只能给出结构化建议，最终执行动作必须可记录、可回放、可测试。
5. 失败时优先截图、DOM、当前节点、错误原因一起发给 AI，生成修复建议后从失败节点继续。

## Workflow 示例

```json
{
  "id": "demo",
  "name": "Demo Workflow",
  "nodes": [
    { "id": "open", "type": "web.open", "params": { "url": "https://example.com" } },
    { "id": "wait", "type": "web.wait_for", "params": { "text": "Example Domain" } }
  ]
}
```

支持的基础节点：

- `web.open`
- `web.hover`
- `web.click`
- `web.input`
- `web.wait_for`
- `web.select`
- `web.extract`
- `web.close_modals`
- `ai.ask`（会把截图和压缩后的页面 HTML 一起传给 Mimo，并返回 JSON 判断结果）
- `flow.wait`
- `flow.if`
- `flow.else`
- `flow.end_if`
- `flow.loop`
- `flow.end_loop`

`ai.ask` 的 JSON 结果会保存到运行时变量，默认变量名是节点 ID，也可以用
`params.saveAs` 指定。后续节点可以用 `${变量名.字段}` 引用，例如
`${ai-title-translation.title}`。

`ai.ask` 默认会传入页面截图和 HTML/DOM 片段。可以用 `params.screenshot=false`
关闭截图，用 `params.html=false` 关闭 HTML，或用 `params.htmlMaxChars` 控制
HTML 片段最大字符数。

workflow 可以在顶层定义 `pageObjects`，并在节点参数里用 `params.pageContext`
绑定页面对象。Mimo 判断和失败自修复都会收到对应页面语境，例如列表页节点绑定
`page2`，商品详情页节点绑定 `page3`，避免把列表页操作套到详情页。

## Mimo 环境变量

复制 `.env.example` 为 `.env` 后填写：

```bash
MIMO_API_KEY=你的key
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5
AI_RPA_HEADLESS=false
AI_RPA_BROWSER_EXECUTABLE=/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
AI_RPA_BROWSER_PROFILE=browser-profile
```
