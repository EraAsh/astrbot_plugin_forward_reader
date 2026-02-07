# AstrBot 插件打包指南

## 问题诊断

你遇到的错误:
```
NotADirectoryError: [Errno 20] Not a directory: '/AstrBot/data/plugins/astrbot_plugin_forward_reade/LICENSE'
```

**根本原因**: ZIP 压缩包的目录结构不正确。

## 正确的目录结构

AstrBot 要求插件 ZIP 文件解压后必须有**顶层目录**,结构应该是:

```
astrbot_plugin_forward_reader.zip
└── astrbot_plugin_forward_reader/    ← 必须有这个顶层目录!
    ├── main.py
    ├── metadata.yaml
    ├── _conf_schema.json
    ├── README.md
    └── LICENSE
```

**错误的结构** (直接打包文件):
```
astrbot_plugin_forward_reader.zip
├── main.py                 ← 错误!没有顶层目录
├── metadata.yaml
├── _conf_schema.json
└── ...
```

## 打包步骤

### 方法1: 使用命令行 (推荐)

#### Windows (PowerShell):
```powershell
# 进入插件目录的上一级
cd Astrbot/chajiankaifa/插件

# 打包整个目录
Compress-Archive -Path astrbot_plugin_forward_reader -DestinationPath astrbot_plugin_forward_reader.zip
```

#### Windows (cmd):
```cmd
cd Astrbot\chajiankaifa\插件
tar -a -c -f astrbot_plugin_forward_reader.zip astrbot_plugin_forward_reader
```

#### Linux/Mac:
```bash
cd Astrbot/chajiankaifa/插件
zip -r astrbot_plugin_forward_reader.zip astrbot_plugin_forward_reader/
```

### 方法2: 使用图形界面

1. **进入上一级目录**: `Astrbot/chajiankaifa/插件`
2. **右键点击文件夹**: `astrbot_plugin_forward_reader`
3. **选择**: "发送到" → "压缩(zipped)文件夹"
4. **重要**: 确保压缩包里包含顶层文件夹

### 方法3: 使用 7-Zip (Windows)

1. 进入 `Astrbot/chajiankaifa/插件` 目录
2. 右键点击 `astrbot_plugin_forward_reader` 文件夹
3. 选择 "7-Zip" → "添加到压缩包..."
4. 压缩包名称: `astrbot_plugin_forward_reader.zip`
5. 压缩格式: `zip`
6. **确保不要勾选** "创建自解压档案"
7. 点击 "确定"

## 验证打包是否正确

解压你创建的 ZIP 文件,应该看到:
```
解压后/
└── astrbot_plugin_forward_reader/    ← 必须首先看到这个文件夹!
    ├── main.py
    ├── metadata.yaml
    └── ...
```

**如果直接看到 main.py、metadata.yaml 等文件,说明打包错误!**

## 检查清单

打包前检查:
- [ ] `metadata.yaml` 中的 `name` 字段是 `astrbot_plugin_forward_reader`
- [ ] 顶层目录名与 `name` 字段一致
- [ ] ZIP 文件包含顶层目录
- [ ] 所有必需文件都在顶层目录内:
  - [ ] `main.py`
  - [ ] `metadata.yaml`
  - [ ] `_conf_schema.json` (可选)
  - [ ] `README.md` (推荐)
  - [ ] `LICENSE` (推荐)

## 快速修复命令

如果你已经有一个错误的 ZIP 文件,可以这样修复:

```powershell
# 1. 解压现有 ZIP
Expand-Archive -Path astrbot_plugin_forward_reader.zip -DestinationPath temp

# 2. 创建正确的结构
New-Item -ItemType Directory -Path temp2/astrbot_plugin_forward_reader
Move-Item -Path temp/* -Destination temp2/astrbot_plugin_forward_reader/

# 3. 重新打包
Compress-Archive -Path temp2/astrbot_plugin_forward_reader -DestinationPath astrbot_plugin_forward_reader_fixed.zip

# 4. 清理临时文件
Remove-Item -Recurse temp, temp2
```

## 安装测试

打包完成后,通过 AstrBot WebUI 上传测试:
1. 登录 AstrBot WebUI
2. 进入 "插件管理"
3. 点击 "上传插件"
4. 选择你打包的 ZIP 文件
5. 如果没有报错,说明打包成功!

## 常见错误

| 错误信息 | 原因 | 解决方案 |
|---------|------|---------|
| `NotADirectoryError` | 缺少顶层目录 | 重新打包,确保包含顶层目录 |
| `插件名不匹配` | 目录名与 metadata.yaml 的 name 不一致 | 修改目录名或 metadata.yaml |
| `找不到 main.py` | main.py 不在正确位置 | 确保 main.py 在顶层目录下 |

## 参考资料

- [AstrBot 插件开发文档](https://astrbot.app)
- [插件模板](https://github.com/Soulter/helloworld)
