# pic-clear

在**离线、无 Python 环境**的 Windows（例如堡垒机里的跳板机）里，扫描目录、找出**接近相同**的图片并删除，仅保留一张。

## 场景

- 通过堡垒机登录到一台 Windows 虚机
- D 盘里有一堆图片（`.jpg` / `.jpeg` / `.png` 等）
- 目标机**不能联网、没有 Python**
- 堡垒机**允许上传文件、不允许下载文件**

## 方案

1. 用 Python 写扫描/去重脚本 `dedupe_pic.py`
2. 通过 GitHub Actions 的 Windows runner 打包成**单文件 exe**（自带 Python 运行时和 Pillow）
3. 从 Actions 下载 `dedupe_pic.exe` → 上传到堡垒机 Win 机 → 命令行运行

**为什么不在 Mac 上打包？** PyInstaller 不支持交叉编译；macOS 打不出 Windows exe。

## 算法

- 每张图算 **dHash**（8×8 差分感知哈希，64 bit）
- 组内两两算 **Hamming 距离**，`<= threshold` 视为"接近相同"
- Union-Find 聚类
- 每组保留一张（可选：最大 / 最早 / 路径最短），其它删除
- 默认扫描 `jpg,jpeg,png,bmp,gif,webp`；用 `--ext all` 可忽略扩展名扫所有文件

## 使用步骤

### 1. 拿到 exe

推荐用 GitHub Actions：

```bash
git init
git remote add origin git@github.com:xflyhack/pic-clear.git
git add .
git commit -m "init"
git push -u origin main
```

推送后进入仓库的 **Actions** → **Build Windows EXE** → 最新一次运行 → **Artifacts** → 下载 `dedupe_pic-windows-exe.zip`，解压得到 `dedupe_pic.exe`（约 20~30 MB）。

如果本地有 Windows 机器也可以直接打包：

```powershell
pip install -r requirements.txt pyinstaller
pyinstaller --onefile --console --name dedupe_pic dedupe_pic.py
# 产物: dist\dedupe_pic.exe
```

### 2. 上传到堡垒机 Win 机

通过堡垒机的"文件上传"功能把 `dedupe_pic.exe` 传到 `D:\tools\` 之类的目录。

### 3. 先跑 dry-run（不删任何东西，只出报告）

```cmd
cd /d D:\tools
dedupe_pic.exe D:\pics --threshold 5 --strategy largest
```

在当前目录产出 `dedupe_report.csv`，字段：

- `group_id` 组编号
- `action` `KEEP` 或 `DELETE`
- `path` 文件路径
- `size_bytes` 文件大小
- `mtime` 修改时间
- `phash_hex` 感知哈希

用记事本 / Excel 打开这个 CSV **人工抽查**几组，确认没误伤。

### 4. 正式删除

确认无误后，加 `--apply`：

```cmd
:: 软删除：把重复文件移到 D:\_dedupe_trash\（推荐，可回滚）
dedupe_pic.exe D:\pics --threshold 5 --apply --trash-dir D:\_dedupe_trash

:: 或者直接永久删除
dedupe_pic.exe D:\pics --threshold 5 --apply --hard-delete
```

### 5. 报告拿不出来怎么办？

由于机器"不能下载文件"，报告需要在机器内查看：

- 直接在 Win 机的记事本 / Excel 里打开 CSV 看
- 或者 `type dedupe_report.csv | more` 分屏看
- 如果堡垒机允许**剪贴板文本单向传出**（很多堡垒机的默认策略），可以 `clip < dedupe_report.csv` 把 CSV 内容复制到剪贴板带出来
- 实在不行：截图带出

## 命令行参数

```
dedupe_pic.exe ROOT [options]

  ROOT                       要扫描的根目录，如 D:\ 或 D:\pics
  --ext EXT                  扫描的扩展名（逗号分隔），传 all 则不过滤
                             默认: jpg,jpeg,png,bmp,gif,webp
  --threshold N              Hamming 距离阈值（0=完全相同，越大越宽松）
                             默认 5，建议范围 3~10
  --strategy S               保留策略：largest / oldest / shortest-path
                             默认 largest
  --report PATH              报告输出路径，默认 ./dedupe_report.csv
  --failed-report PATH       无法解码的文件清单
  --apply                    真正删除（默认 dry-run）
  --trash-dir DIR            软删除目录（不指定则永久删除）
  --hard-delete              强制永久删除
```

## 阈值怎么选？

| threshold | 效果 |
|-----------|------|
| 0 | 只删完全一样（等价于精确哈希）|
| 3 | 几乎肉眼一样（推荐起步值）|
| 5 | 轻微压缩 / 裁边 / 水印仍视为相同（默认）|
| 10 | 构图相似就会被合并，**可能误杀**，慎用 |

**首次使用强烈建议先 `--threshold 3` 跑一遍看看效果，再逐步放宽。**

## 性能

- dHash 计算：单张几毫秒，主要耗时在磁盘 IO
- 聚类：O(N²)，N=1 万时约几秒，N=10 万时约几分钟；如果你的量级远超 10 万再告诉我，改成 BK-Tree

## 风险提示

- **务必先 dry-run 抽查 CSV**，感知哈希不是万能的，特别是纯色/相似构图的截图容易误判
- 首次执行**强烈建议用 `--trash-dir` 软删除**，验证一段时间后再清空回收目录
- 脚本本身不会碰 `--trash-dir` 里的东西，回滚就是把文件 move 回去
