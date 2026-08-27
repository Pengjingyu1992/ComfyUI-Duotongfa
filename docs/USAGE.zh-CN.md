# 使用说明

把 ComfyTV Local LLM 或其他本地 OpenAI-compatible 客户端设置为 `http://127.0.0.1:1234/v1`。Codex、Local LLM、图生文客户端都可以继续切换，前提是它们走同一个本地网关，而不是绕过网关直连后端。

检查状态：

```bash
python duotongfa_gateway.py check
python duotongfa_gateway.py status
```

渲染交接顺序为：`render.prepare` 停止新 LLM 请求并释放资源；ComfyUI 入队取得 `prompt_id`；`render.commit` 绑定任务；长任务定期 `render.heartbeat`；成功、失败、取消或中断时 `render.release`。

恢复策略：

- `on-demand`（推荐）：渲染后保持 LLM 冷态，下次对话再启动。
- `restore`：恢复渲染前状态。
- `never`：不自动恢复。

渲染期间，模型列表可以使用只读缓存；聊天与 embedding 请求返回 HTTP 423，不会被转发到后端，也不会唤醒模型。

## 为什么渲染期间卸载模型反而更快

LLM 常驻会和生图模型争用显存或 Apple 统一内存。渲染前卸载模型可以减少资源争用，实际速度和内存收益需要在所用模型与硬件上测量。

多通阀先排空 LLM 请求，验证模型资源已释放，再允许 ComfyUI 渲染。渲染结束后释放锁，按配置选择立即恢复或在下一次识图、聊天时恢复后端。

## 强制使用指定模型

当前部署需要锁定一个已知模型时，可在启动网关前设置：

```bash
export DUOTONGFA_FORCE_MODEL="your-model-id"
python duotongfa_gateway.py serve
```

网关会覆盖 `/chat/completions`、`/completions` 和 `/responses` 请求中的 `model`，并返回 `X-Duotongfa-Model-Policy: FORCED`。`/embeddings` 不会被改写，因为 embedding 通常需要独立模型。配置为空时完全禁用该策略，恢复由客户端选择模型的通用模式。

模型名必须使用 LM Studio `/models` 返回的实际 ID；下载目录名或界面显示名不一定相同。客户端可以继续保存自己的对话历史，因此强制模型或以后更换模型不会另建一份上下文。

## 冷态检查注意事项

不要把 `lms ps`、`lms ls` 或 `lms server status` 当作被动健康检查；部分 LM Studio 版本执行这些命令时会唤醒后台服务。请使用 `python duotongfa_gateway.py status`，或直接检查网关状态、后端端口和进程。模型缓存仅在后端 READY 时成功访问 `/models` 后刷新，新下载模型要等下一次后端启动并刷新后才会出现在冷态列表里。
