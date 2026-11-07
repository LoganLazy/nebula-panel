# Nebula Panel

[项目仓库](https://github.com/LoganLazy/nebula-panel) · [问题反馈](https://github.com/LoganLazy/nebula-panel/issues)

> Nebula Deploy 的本机可视化控制台：服务器与项目管理、部署计划审阅、异步任务及日志。

Nebula Panel v0.3.0 是 Nebula Deploy 的本机可视化控制台。它管理服务器、项目和异步任务，但不在远端服务器常驻任何面板服务。

## 启动

    git clone https://github.com/LoganLazy/nebula-deploy.git
    git clone https://github.com/LoganLazy/nebula-panel.git
    cd nebula-panel
    export DEPLOY_AI_KEY='兼容 AI 接口密钥'
    python3 server.py

浏览器打开 http://127.0.0.1:8765。启动时终端会打印一次访问令牌，在浏览器里输入一次即可进入（令牌只保存在当前标签页，关闭即失效；也可以启动前设置 `NEBULA_PANEL_TOKEN` 指定令牌，这时终端不再打印）。

当前版本支持：

- 保存多个 SSH 服务器与本地项目配置，支持编辑与重复去重。
- 检查服务器系统、内存、磁盘和运行环境；上次检查结果会显示在服务器卡片上。
- 调用独立的 Nebula Deploy 生成部署计划。
- 在界面展开并完整审阅 AI 计划后发起部署；失败的部署可以按同一计划重试，失败的计划可以重新生成。
- 部署前明确选择临时 swap 和 AI 自动修复次数。
- 实时查看、取消、删除后台任务，并在重启面板后恢复任务历史与日志；任务日志按 1MB 截断，最多保留 100 个任务。
- 操作审计：服务器/项目增删改、任务创建/取消/重试/删除都会记入 `data/audit.log`，在任务中心可见。

所有数据只写入本机 data 目录，权限为当前用户可读写；API Key 只从启动进程的环境变量读取。面板强制监听本机地址，所有 `/api` 接口（包括读接口）都要访问令牌；令牌不对时一律返回 403。

关闭面板时会终止其启动的在途任务（先 SIGTERM，等 5 秒还不退出就 SIGKILL），防止 SSH 子进程脱离面板继续运行；再次启动后未完成记录会标记为“已中断”。部署服务器之前应先审阅计划，自动修复开启后产生的新计划会继续经过 Nebula Deploy 的安全校验。

## 环境与配套工具

需要 Python 3.10+，面板本身只使用标准库。默认查找同级目录 `nebula-deploy/nebula_deploy.py`；放在其他位置时，启动前设置 `NEBULA_DEPLOY_PATH` 为该文件的绝对路径。实际部署还需要 OpenSSH 和可用的 AI 接口。

使用 `python3 server.py --port 8766` 可更换端口。面板只适合本机访问，需要远程查看时使用 SSH 端口转发：`ssh -L 8765:127.0.0.1:8765 user@host`。

`data/` 包含服务器地址、项目配置和任务日志，是本地运行数据，不随源码发布。当前仓库尚未指定项目代码许可证。

## 验证

`python3 -m unittest discover -s tests -v` 使用临时目录和本机临时 HTTP 服务，检查令牌鉴权（无令牌读接口 403、会话接口不返回令牌）、严格校验（端口 bool/小数、远端目录白名单、AI 地址）、重复去重、编辑、任务删除、取消时 SIGTERM 忽略子进程的 SIGKILL 升级、检查结果持久化、任务摘要与详情、审计日志；不连接真实 SSH 或 AI。2026-11-07 通过（14 个测试）。
