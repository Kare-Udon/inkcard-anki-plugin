[中文](README.md) | [English](README.en.md)

# InkCards Anki 插件

在 Anki Desktop 与[阅星曈 X4](https://www.xteink.cn/) 设备之间同步文本卡片与复习记录。

当前版本：`0.2.0-preview.10`。

支持 Anki：`26.08.1`、`26.9.3`。

## 安装步骤

1. 从 [Releases](https://github.com/Kare-Udon/inkcard-anki-plugin/releases) 下载 `.ankiaddon` 安装包。
2. 在 Anki 中打开「工具 → 插件 → 从文件安装」，选择安装包，然后重启 Anki。
3. 打开要同步的用户配置，进入「工具 → InkCards…」。
4. 按下方「局域网访问」设置电脑与 X4 的连接。
5. 在插件中生成配对码，输入 X4 完成配对。
6. 在插件中选择牌组，执行预检并准备快照，然后在 X4 上同步。同步时保持 Anki 和目标用户配置处于打开状态。

## 局域网访问

1. 将电脑与 X4 连接到同一局域网，在电脑的系统网络设置中查看局域网 IP，例如 `192.168.1.10`。
2. 在「工具 → InkCards…」中勾选「允许局域网设备连接」。插件会将监听地址从 `127.0.0.1` 改为 `0.0.0.0`，允许通过电脑的局域网地址连接；重启 Anki 后生效。
3. 在系统防火墙中允许来自局域网的 Anki TCP `8766` 入站连接。
4. 在 X4 的「电脑地址」中填写电脑的实际局域网 IP 和端口，例如 `192.168.1.10:8766`。

也可以在「工具 → 插件」中选中 InkCards，打开「配置」，手动设置：

```json
{
  "device_server": {
    "host": "0.0.0.0",
    "port": 8766
  }
}
```

若要只监听某张网卡，将 `host` 填为电脑该网卡的实际局域网 IP，例如 `192.168.1.10`。保存配置后重启 Anki。

## 开发

开发环境：Python 3.10+，以及上述支持版本的 Anki Desktop。

在源码根目录构建安装包：

```sh
python3 tools/package_ankiaddon.py
```

产物：`dist/InkCards-Anki-Bridge-0.2.0-preview.10.ankiaddon`。将其安装到 Anki，重启并打开目标用户配置，即可通过「工具 → InkCards…」启动界面。修改源码后重新构建、安装并重启。

运行打包、配对认证、存储和插件界面测试：

```sh
PYTHONPATH=. python3 -m unittest tests.test_package_ankiaddon tests.test_device_auth tests.test_storage tests.test_plugin_ui -v
```
