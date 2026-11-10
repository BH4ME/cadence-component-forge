# 本地发布服务

本工具使用 Python 标准库提供发布页面与静态文件，默认只监听 `127.0.0.1:8766`。
它不会生成签名密钥、安装包或发布清单，也不会修改现有工程。

## 文件布局

```text
packaging/release/public/
  manifest.json
  packages/
    <version>/
      <filename>
```

`manifest.json` 为签名工具生成的原始 envelope：
`{manifest: {schema, product, channel, platform, version, artifact, notes, published_at}, key_id, signature}`。
`artifact` 包含 `filename`、`url`、`size`、`sha256`。
当前页面接受 `schema: 1`、`product: "jlc2capture"`、`channel: "stable"`、
`platform: "windows-x64"`。

## 运行

在工程根目录运行：

```powershell
.\.venv-capture\Scripts\python.exe tools/update_server/server.py --root packaging/release/public --host 127.0.0.1 --port 8766
```

打开 <http://127.0.0.1:8766/>。使用 `Ctrl+C` 停止。
未提供真实发布目录时，启动命令会报告缺失目录；存在目录但缺少有效清单时，页面返回 503。

也可以让服务本身在回环地址上终止 TLS。证书和私钥必须是发布者单独管理的
PEM 文件，不能放在 `public/` 目录或安装包中：

```powershell
.\.venv-capture\Scripts\python.exe tools/update_server/server.py `
  --root packaging/release/public --host 127.0.0.1 --port 8766 `
  --certfile D:/PRIVATE-RELEASE-KEYS/site-cert.pem `
  --keyfile D:/PRIVATE-RELEASE-KEYS/site-key.pem
```

这会输出 `https://127.0.0.1:8766/`。服务仍只允许绑定回环地址；公网部署应由
已有 HTTPS 反向代理或静态文件站点提供证书和域名。`certfile` 与 `keyfile` 必须同时
提供，默认不启用 TLS，因此原有本地 HTTP 测试行为不变。

首页读取清单中的真实版本、发布时间、更新说明及签名 key ID，全部动态文字经过 HTML 转义。
只有本地安装包的大小和 SHA256 与清单一致时，首页才显示下载链接。
该检查不替代客户端的签名验证。安装包按清单文件名从本地 `/packages/<version>/<filename>` 下载；
页面不会将清单中的任意 URL 用作超链接。

服务支持 GET 与 HEAD，拒绝目录列表、目录穿越和指向公开目录之外的符号链接。
清单和页面均返回 `Cache-Control: no-store`，不会基于条件请求返回过期清单。
版本包路径应保持不可变；包文件返回长期缓存头。

生产环境建议由反向代理终止 HTTPS，再转发到此 loopback 服务。
将正式域名和证书配置在反向代理，并将清单的 `artifact.url` 写为正式 HTTPS 下载地址。
正式地址尚未确定时不要编造公网 URL，也不要直接把本服务绑定到公网。

## 验证

```powershell
.\.venv-capture\Scripts\python.exe -m unittest tools.update_server.test_server -v
```

测试只在临时目录写入明确标记的测试清单与字节数据，使用随机 loopback 端口，结束时关闭服务和线程。
