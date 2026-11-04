# 文件夹最新图像实时显示

Windows 桌面程序，使用 Python + PySide6 + watchdog。直接运行 `image_viewer.py`，无须打包。

## 安装与运行

建议使用 **64 位 Python 3.11 或 3.12**（代码要求 Python 3.10+）。在项目目录打开 PowerShell：

```powershell
python -m pip install -r requirements.txt
python image_viewer.py
```

也可以直接安装依赖：

```powershell
python -m pip install PySide6 watchdog
```

启动后点击“选择文件夹”，或粘贴路径后点击“开始监听”/按回车。也支持启动时传入路径：

```powershell
python image_viewer.py "C:\Users\mechmind_\Desktop\22个\22个原图"
```

## 在 VS Code 中运行

1. 安装 Python，以及 VS Code 的 **Python（Microsoft）** 扩展。
2. 在 VS Code 中选择“文件 → 打开文件夹”，打开本项目目录。
3. 按 `Ctrl+Shift+P`，执行 `Python: Select Interpreter`，选择安装的 Python 3.11/3.12 解释器。
4. 新建 VS Code 终端，执行 `python -m pip install -r requirements.txt`。确保终端使用的 Python 与所选解释器一致；可用 `python -c "import sys; print(sys.executable)"` 确认路径。
5. 打开 `image_viewer.py`，点击右上角“运行 Python 文件”，或在终端执行 `python image_viewer.py`。
6. 在弹出的窗口中选择图像文件夹。之后将新图像复制到该文件夹即可自动更新。

如使用虚拟环境，可以先在终端执行 `python -m venv .venv`，再通过 `Python: Select Interpreter` 选择 `.venv\Scripts\python.exe`，新建终端后安装依赖。

## “最新”的判断规则

- **首次选择**：只扫描当前层级，按最后修改时间排序；相同时间按文件名排序保证结果稳定。优先加载最新文件；若文件损坏或无法读取，会尝试其他有效图像。
- **运行期间**：新建、移入、重命名为支持格式的文件，按 watchdog 实际观察到的到达事件赋予递增序号，优先显示序号较大的有效图像。因此保留旧修改时间的图片也能自动显示。
- **批量导入**：同一文件后续写入事件保留其到达序号，不会因为较早文件写入较慢而覆盖已经显示的较新文件。批量复制的顺序以操作系统事件顺序为准。
- **同名覆盖**：监听修改事件，当前图片内容发生变化时重新加载；删除后重建、临时文件改名替换也会触发更新。
- **等待写入**：合并短时间重复事件，检查文件大小、修改时间及创建时间稳定至少 0.3 秒，再尝试解码，并在解码后重新检查。通常写入完成后约 0.5–1 秒显示，超大图像或慢速磁盘可能更久。每个候选最多尝试 40 次，间隔约 0.25 秒，失败时继续监听并保留原预览；后续文件事件可以再次触发尝试。

支持 JPG、JPEG、PNG、BMP、TIFF、TIF、WEBP，扩展名不区分大小写；多页 TIFF 和动画格式显示第一帧。图像保持宽高比完整显示，支持中文和空格路径。空目录会持续等待新图像，子目录不会触发更新。

监听和解码在后台完成，通过 Qt 信号更新界面；解码后关闭文件句柄。切换文件夹立即使旧会话失效并通知旧线程停止，旧结果无法更新新界面。关闭窗口时会等待后台解码和监听线程退出。

> 文件系统没有通用的“复制完成”通知，程序采用稳定性检查与解码重试判断文件可用。若写入程序长时间暂停后继续写入，后续修改事件仍会再次刷新图像。

## 自动验证

```powershell
python -m unittest -v test_image_viewer
```

测试使用临时目录和真实文件监听事件，覆盖中文路径、旧修改时间的新图像、同名覆盖、分段写入、批量导入顺序、改名导入、忽略子目录、格式解码、损坏文件、切换目录及线程清理。测试以离屏方式运行，不会打开可见窗口。
