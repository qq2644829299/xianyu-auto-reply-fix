# 鱼智云本地客户端

本地客户端会在电脑上启动完整的闲鱼登录与消息服务，并自动打开本地管理页。

- macOS：当前发布包适用于 Intel Mac；双击镜像内唯一的 `FishCloudLocal.app` 即可运行。
- Windows：构建后得到 `.exe`。
- 账号登录资料、闲鱼连接和本地数据保存在当前用户目录，不会因启动器上传到服务器。

构建：

```bash
python3 -m pip install -r desktop/requirements-build.txt
python3 desktop/build.py
```

第一次打开后访问的地址是 `http://127.0.0.1:18790`。电脑需要保持开机，客户端才能持续接收闲鱼消息。
