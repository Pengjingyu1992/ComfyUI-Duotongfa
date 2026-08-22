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
