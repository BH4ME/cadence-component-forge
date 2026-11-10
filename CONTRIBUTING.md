# Contributing

感谢参与 JLC2Cadence。提交前请确认：

1. 不提交 Cadence 工程、下载缓存、STEP/OLB/DRA/PSM/PAD 大文件、私钥或生产更新地址。
2. 新增转换逻辑时保留源数据哈希和失败原因，不用占位几何掩盖缺失数据。
3. 为修复添加可重复的 Python/Tcl/JavaScript 回归测试。
4. 在本地运行与改动相关的测试，并在 Pull Request 中说明 Cadence 版本和未验证的范围。

推荐的最小检查：

```powershell
python -m unittest discover -s tests -p "test_*.py"
python -m unittest discover -s tools/update_server -p "test_server.py"
npm --prefix capture_plugin/web ci --ignore-scripts
npm --prefix capture_plugin/web test
```

