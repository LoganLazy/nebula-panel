# Nebula Panel

[项目仓库](https://github.com/LoganLazy/nebula-panel) · [问题反馈](https://github.com/LoganLazy/nebula-panel/issues)

> Nebula Deploy 的本机可视化控制台：服务器与项目管理、部署计划审阅、异步任务及日志。

Nebula Panel v0.2.0 是 Nebula Deploy 的本机可视化控制台。它管理服务器、项目和异步任务，但不在远端服务器常驻任何面板服务。

## 启动

    git clone https://github.com/LoganLazy/nebula-deploy.git
    git clone https://github.com/LoganLazy/nebula-panel.git
    cd nebula-panel
    export DEPLOY_AI_KEY='兼容 AI 接口密钥'
    python3 server.py

浏览器打开 http://127.0.0.1:8765。

首版支持：

- 保存多个 SSH 服务器与本地项目配置。
- 检查服务器系统、内存、磁盘和运行环境。
- 调用独立的 Nebula Deploy 生成部署计划。
- 在界面展开并完整审阅 AI 计划后发起部署。
- 部署前明确选择临时 swap 和 AI 自动修复次数。
- 实时查看、取消后台任务，并在重启面板后恢复任务历史与日志。

所有数据只写入本机 data 目录，权限为当前用户可读写；API Key 只从启动进程的环境变量读取。面板强制监听本机地址，并使用会话令牌保护写操作。

关闭面板时会终止其启动的在途任务，防止 SSH 子进程脱离面板继续运行；再次启动后未完成记录会标记为“已中断”。部署服务器之前应先审阅计划，自动修复开启后产生的新计划会继续经过 Nebula Deploy 的安全校验。

## 环境与配套工具

需要 Python 3.10+，面板本身只使用标准库。默认查找同级目录 `nebula-deploy/nebula_deploy.py`；放在其他位置时，启动前设置 `NEBULA_DEPLOY_PATH` 为该文件的绝对路径。实际部署还需要 OpenSSH 和可用的 AI 接口。

使用 `python3 server.py --port 8766` 可更换端口。面板只适合本机访问，需要远程查看时使用 SSH 端口转发：`ssh -L 8765:127.0.0.1:8765 user@host`。

`data/` 包含服务器地址、项目配置和任务日志，是本地运行数据，不随源码发布。当前仓库尚未指定项目代码许可证。

## 验证

`python3 -m unittest discover -s tests -v` 使用临时目录和本机临时 HTTP 服务，检查会话、写操作令牌、服务器配置增删、文件权限及静态目录越界拒绝；不连接真实 SSH 或 AI。2026-11-06 通过。修复了静态目录越界请求原先返回500的问题，现返回404。
