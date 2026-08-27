# 安装教程

要求：Python 3.10+、已安装的 ComfyUI、一个本地 OpenAI-compatible LLM 后端。默认网关端口为 `1234`，后端端口为 `1235`。

```bash
git clone https://github.com/Pengjingyu1992/ComfyUI-Duotongfa.git
cd ComfyUI-Duotongfa
python tools/install_duotongfa.py install --start
```

`0.2.4` 可以在安装用户服务时持久化模型与并发配置，例如：

```bash
python tools/install_duotongfa.py install --start \
  --max-concurrent-requests 2 \
  --lm-studio-context-length 32768 \
  --lm-studio-parallel 2 \
  --lm-studio-model-ttl-seconds 360 \
  --force-model bot-model
```

仓库可以放在任意目录，也可以放进 `ComfyUI/custom_nodes`。它是伴随服务，故意不注册画布节点。安装器会把两个无第三方依赖的运行文件复制到稳定的当前用户目录，再创建 macOS LaunchAgent、Linux systemd 用户服务或 Windows 当前用户启动脚本。

LM Studio 低内存机器的完整接管示例：

```bash
python tools/install_duotongfa.py install --start \
  --provider lm-studio \
  --upstream http://127.0.0.1:1235 \
  --force-app-exit \
  --allow-external-stop \
  --require-process-exit
```

这三个接管选项会允许多通阀关闭外部 LM Studio 进程，执行前请保存 LM Studio 中的工作。默认情况下，多通阀只停止自己启动的后端。

卸载：

```bash
python tools/install_duotongfa.py uninstall --start
```
