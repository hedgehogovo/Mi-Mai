<p align="center">
  <img src="assets/logo.png" width="160" alt="Mi Mai Logo">
</p>

<p align="center">
  <a href="https://github.com/hedgehogovo/Mi-Mai">
    <img src="https://img.shields.io/github/stars/hedgehogovo/Mi-Mai?style=flat-square&label=Give%20me%20a%20star" alt="Give me a star">
  </a>
  <img src="https://img.shields.io/github/languages/top/hedgehogovo/Mi-Mai?style=flat-square" alt="Top language">
  <img src="https://img.shields.io/badge/license-GPL--3.0--or--later-2f7d32?style=flat-square" alt="License">
  <img src="https://img.shields.io/github/last-commit/hedgehogovo/Mi-Mai?style=flat-square&label=last%20commit" alt="Last commit">
  <img src="https://img.shields.io/badge/MaiBot-%E2%89%A51.4.5-4b8bbe?style=flat-square" alt="MaiBot compatibility">
  <img src="https://img.shields.io/badge/latest%20plugin%20version-1.0.0-ef6c00?style=flat-square" alt="Latest plugin version 1.0.0">
</p>

<h1 align="center">Mi Mai - 让麦麦控制你的米家设备</h1>

<p align="center">
  MaiBot 的第三方米家控制插件：扫码登录米家云端，查看设备状态，并在明确授权后控制设备与场景。
</p>

## 功能

- 米家账号扫码登录、二维码刷新和退出登录。
- 刷新并查看设备状态，只读设备列表和只读状态工具默认开启。
- 为设备设置别名和类别，设备与场景分别加入控制白名单。
- 在侧栏中查看已授权设备和场景，并执行需要确认的手动操作。
- 设备属性控制、MIoT 动作参数控制和场景控制。
- 自然语言设备控制和场景控制默认关闭，开启时由 MaiBot 宿主显示风险确认。
- 用户白名单默认开启；关闭后会先显示风险确认，设备和场景白名单仍然有效。
- 可选联动 MaiWeave：明确处于忙碌或睡眠状态时暂缓控制请求。
- 插件失败、权限不足或工具关闭时，面向对话保持自然口吻，不把内部错误直接暴露给用户。

## 安装

本仓库根目录就是插件目录。将本仓库内容直接放入 MaiBot 的 `plugins/<plugin-name>/` 目录，并安装米家 API 依赖：

```powershell
python -m pip install -r requirements.txt
```

重启 MaiBot 后，在侧栏打开 **Mi Mai**。首次使用时点击“扫码登录”获取二维码；登录后点击“读取设备状态”，为需要控制的设备填写别名和类别，再加入白名单。场景需要单独加入白名单。

## 使用指令

```text
/mimai 列表
/mimai 状态 <设备别名或名称>
/mimai 开 <设备别名>
/mimai 关 <设备别名>
/mimai 操作 <设备别名> <动作名> [JSON参数]
/mimai 场景 <场景名称>
```

开关指令只适用于设备规格中存在可写 `on`、`power`、`switch` 属性，或描述为“开关”的设备。更细粒度的属性和 MIoT 动作可通过侧栏页面或 LLM 工具使用。

## 安全边界

只读工具与控制工具分开管理。控制工具默认关闭，开启时需要确认可能产生真实设备副作用的提示；手动操作同样需要确认。后端会再次检查工具开关、用户白名单、设备/场景白名单和 MaiWeave 状态，不依赖页面状态作为唯一防线。

米家登录令牌和插件设置只保存在 MaiBot 为插件提供的数据目录中，不写入 MaiBot 主程序配置文件，也不会放入 WebUI 查询结果或 LLM 工具结果。

## 兼容性

- MaiBot：`1.4.5` 及以后版本（清单当前声明上限为 `1.4.99`）。
- maibot-plugin-sdk：清单当前声明 `2.22.1` 至 `2.23.99`。
- Python：以 MaiBot 当前运行环境为准。

清单的最高版本用于 MaiBot 的兼容性校验，不代表未来版本已经完成验收。升级 MaiBot 或 SDK 后，建议重新执行扫码、设备刷新、白名单、属性控制和场景控制验收。

## 测试

在仓库根目录运行：

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
node -e "const fs=require('fs');const h=fs.readFileSync('webui/index.html','utf8');const s=h.indexOf('<script>'),e=h.indexOf('</script>',s);new Function(h.slice(s+8,e));console.log('webui script syntax ok')"
```

真实扫码和米家云端控制需要在已安装 MaiBot 的实例中验收；页面脚本还应通过项目已有的 WebUI 测试流程检查。

## 目录

```text
.
├── _manifest.json       # 插件元信息
├── plugin.py            # SDK 入口与工具、指令、事件
├── core.py              # 米家 API、权限和状态逻辑
├── webui.json           # 侧栏页面与 API 声明
├── webui/index.html     # 侧栏配置向导
├── assets/logo.png      # 插件 logo
└── tests/               # 单元测试
```

## 作者与许可

作者：**Orxil.V**

本项目使用 `GPL-3.0-or-later` 许可，许可声明见 `_manifest.json`。

## Star 趋势

<p align="center">
  <a href="https://star-history.com/#hedgehogovo/Mi-Mai&Date">
    <img src="https://api.star-history.com/svg?repos=hedgehogovo/Mi-Mai&type=Date" alt="Mi Mai Star History Chart">
  </a>
</p>
