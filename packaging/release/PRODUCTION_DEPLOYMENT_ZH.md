# JLC2Cadence 正式更新站点部署清单

本项目的更新站点是静态 HTTPS 文件站点。正式发布前，发布者需要在独立签名环境完成签名，再把生成的公开目录上传到站点。

## 站点目录

```text
public/
  manifest.json
  packages/
    0.47.0/
      JLC2Cadence-0.47.0-Setup.exe
```

`manifest.json` 和安装包版本目录必须使用 HTTPS 可访问地址。安装包文件发布后保持不可变，不能用相同路径替换不同字节的文件。

## 生成发布文件

在源码目录外保存生产私钥，例如 `D:\PRIVATE-RELEASE-KEYS\update.pem`。私钥、密码和临时测试密钥不能进入源码、安装包或公开站点。

```powershell
python packaging/release/sign_release.py sign `
  --installer <path-to-setup.exe> `
  --version 0.47.0 `
  --base-url https://YOUR-DOMAIN/jlc2capture `
  --private-key D:/PRIVATE-RELEASE-KEYS/update.pem `
  --key-id production-2026 `
  --output-dir packaging/release/public
```

把 `packaging/release/public/` 的全部内容上传到 HTTPS 站点根目录。签名工具会写出 `manifest.json`，并按 `base-url` 生成安装包下载地址。

正式安装包需要在构建时写入同一个清单地址。得到生产公钥后，把它写入
`packaging/windows/update-public-keys.json`，然后使用以下命令重建安装器：

```powershell
.\packaging\windows\build.ps1 `
  -UpdateManifestUrl 'https://YOUR-DOMAIN/jlc2capture/manifest.json'
```

该参数只接受绝对 `https://` 地址；省略时安装配置中的地址保持为空。

如果不使用已有反向代理，也可以用 `tools/update_server/server.py` 的
`--certfile` 和 `--keyfile` 在回环地址终止 TLS；证书和私钥必须保存在公开目录之外。
公网域名仍应由现有 HTTPS 静态站点或反向代理提供。

## 安装包内的信任公钥

把同一生产公钥写入 `packaging/windows/update-public-keys.json`，格式如下：

```json
{
  "schema": 1,
  "keys": {
    "production-2026": "base64 编码的 32 字节 Ed25519 原始公钥"
  }
}
```

填入公钥后必须重新构建安装包。公钥可以公开，私钥不能公开。客户端不会从更新站点下载新的信任公钥。

## 发布前检查

```powershell
python -m jlc2cadence.updater check `
  --manifest-url https://YOUR-DOMAIN/jlc2capture/manifest.json `
  --current-version 0.46.0 `
  --trusted-keys packaging/windows/update-public-keys.json
```

正式环境不能使用 `--allow-loopback-http`。确认清单签名、版本、文件大小和 SHA-256 均通过后，再把站点地址写入安装配置的 `update_manifest_url`。

当前仓库没有填入公网 URL 或生产公钥；占位值不能用于正式发布。
