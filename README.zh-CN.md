# 多通阀

多通阀是一个本机资源交接插件/伴随服务，面向 ComfyUI、ComfyTV 与本地 LLM 共用显存或统一内存的场景。它不创建虚构节点，不修改 ComfyUI 原生逻辑，也不替代 ComfyTV；核心职责是让“LLM 让出资源 → 验证释放 → ComfyUI 渲染 → 恢复或保持冷态”成为可验证的状态机。

![多通阀架构](docs/images/architecture.svg)

## 灵感与动机

设计灵感来自热泵系统的多通阀，以及特斯拉八通阀“用一个协调部件为多个热源/负载选择流路”的思路。这里借用的是资源调度思想：GPU 显存或 Apple 统一内存像共享介质，Local LLM 与图像生成是不同负载，多通阀负责选择安全流路。项目与特斯拉无任何隶属或合作关系。

之所以要做，是因为很多本地 LLM 的“关闭”并不代表模型工作进程和内存已经真正释放；模型刷新、健康检查或 GUI 还可能再次唤醒后台。ComfyUI 随后入队会变慢、交换内存、503，甚至长期卡在低进度。多通阀将端口、进程、内存和 ComfyUI 任务状态放进同一个交接逻辑。

## 为什么渲染期间卸载模型反而会提速

LLM 和生图模型会争用显存或 Apple 统一内存。渲染前卸载 LLM 可以减少内存压力和换页；实际收益取决于模型、运行后端和硬件。

交接顺序是排空 LLM 请求、卸载并验证模型资源、执行 ComfyUI 渲染、释放锁。使用按需恢复策略时，下一次 LLM 请求会在需要时重新启动后端。

## 隐私边界

- 不内置、不保存 API Key。
- 不记录提示词、图片、音视频、请求正文或 Authorization 请求头。
- 只在当前用户的本机状态目录保存锁状态和模型列表缓存。
- 默认只监听 `127.0.0.1`；非回环监听必须配置控制令牌。
- 不下载、删除或上传模型与工作流。

## 快速安装

```bash
git clone https://github.com/Pengjingyu1992/ComfyUI-Duotongfa.git
cd ComfyUI-Duotongfa
python tools/install_duotongfa.py install --start
```

把 ComfyTV Local LLM 或其他本地 OpenAI-compatible 客户端地址设置为：

```text
http://127.0.0.1:1234/v1
```

完整步骤见 [安装教程](docs/INSTALL.zh-CN.md)，使用方法见 [使用说明](docs/USAGE.zh-CN.md)，架构见 [架构说明](docs/ARCHITECTURE.md)。

## 当前测试范围

真机验证曾覆盖一台 Apple Silicon macOS 设备，使用 Comfy Desktop、PyTorch MPS 和本地 LM Studio。最新软件组合及其他硬件配置仍需完成完整的资源交接验证。

macOS、Windows 和 Linux 的安装器与协调逻辑已有自动化测试；Windows 和 Linux 尚未完成真机验证。欢迎提交兼容性反馈，分享前请删除密钥、私人提示词、媒体、模型文件及个人目录。

项目处于 Alpha 阶段，采用 GPL-3.0 开源。
