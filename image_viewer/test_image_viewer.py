"""集成测试：使用临时文件夹和真实 Windows 文件系统事件。"""

import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QApplication

import image_viewer as viewer


class ViewerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="图像 空格 ")
        self.folder = Path(self.temp.name)
        self.window = viewer.ImageViewer()
        self.window.show()
        self.frames = []

    def tearDown(self):
        self.window.close()
        self.wait_for(lambda: not self.window.workers)
        self.app.processEvents()
        self.temp.cleanup()

    def wait_for(self, predicate, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return
            time.sleep(0.02)
        self.fail(f"等待超时，状态：{self.window.statusBar().currentMessage()}")

    def pump(self, duration=0.9):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.app.processEvents()
            time.sleep(0.02)

    def save(self, name, color="red", width=80, folder=None):
        path = (folder or self.folder) / name
        image = QImage(width, 60, QImage.Format.Format_RGB32)
        image.fill(QColor(color))
        self.assertTrue(image.save(str(path)))
        return path

    def start(self, folder=None):
        self.window.path_input.setText(str(folder or self.folder))
        self.window.start_folder()
        self.window.workers[-1].image_ready.connect(
            lambda session, path, image: self.frames.append((session, Path(path).name, image.width()))
        )

    def displayed(self, name, width=80):
        return (f"图像：{name}    尺寸：{width} × 60 像素" == self.window.info_label.text())

    def test_initial_then_old_timestamp_import_and_overwrite(self):
        old = self.save("旧图.PNG")
        os.utime(old, (100, 100))
        self.save("最新.png")
        self.start()
        self.wait_for(lambda: self.displayed("最新.png"))
        imported = self.save("导入旧时间.png", "blue")
        os.utime(imported, (50, 50))
        self.wait_for(lambda: self.displayed(imported.name))
        self.save(imported.name, "green", width=100)
        self.wait_for(lambda: self.displayed(imported.name, 100))
        # 已显示图像不再占用文件，支持立即删除。
        imported.unlink()
        self.wait_for(lambda: "已删除" in self.window.statusBar().currentMessage())

    def test_incomplete_batch_rename_and_subdirectory(self):
        self.start()
        self.wait_for(lambda: "暂无图像" in self.window.statusBar().currentMessage())
        self.save("基准.png")
        self.wait_for(lambda: self.displayed("基准.png"))
        earlier = self.folder / "较早.png"
        earlier.write_bytes(b"incomplete")
        self.pump(0.15)
        self.save("较晚.png", "blue")
        self.wait_for(lambda: self.displayed("较晚.png"))
        self.save(earlier.name, "green")
        self.pump()
        self.assertTrue(self.displayed("较晚.png"))
        self.assertNotIn("较早.png", [frame[1] for frame in self.frames])

        partial = self.folder / "分段.png"
        partial.write_bytes(b"incomplete")
        self.pump(1.0)
        self.assertTrue(self.displayed("较晚.png"))
        self.save(partial.name, "yellow")
        self.wait_for(lambda: self.displayed(partial.name))

        child = self.folder / "子文件夹"
        child.mkdir()
        self.save("忽略.png", folder=child)
        self.pump()
        self.assertTrue(self.displayed(partial.name))

        temporary = self.folder / "upload.tmp"
        image = QImage(80, 60, QImage.Format.Format_RGB32)
        image.fill(QColor("cyan"))
        self.assertTrue(image.save(str(temporary), "PNG"))
        temporary.rename(self.folder / "重命名.png")
        self.wait_for(lambda: self.displayed("重命名.png"))

    def test_switch_invalid_path_and_cleanup(self):
        self.save("旧会话.png")
        self.start()
        self.wait_for(lambda: self.displayed("旧会话.png"))
        second = self.folder / "新目录"
        second.mkdir()
        self.save("新会话.png", folder=second)
        self.start(second)
        self.save("旧目录新增.png")
        self.wait_for(lambda: self.displayed("新会话.png"))
        self.pump()
        self.assertTrue(self.displayed("新会话.png"))
        self.wait_for(lambda: len(self.window.workers) == 1)
        self.start(self.folder / "不存在")
        self.wait_for(lambda: "监听失败" in self.window.statusBar().currentMessage())
        self.wait_for(lambda: not self.window.workers)

    def test_supported_formats_and_corrupt_fallback(self):
        for extension in ("jpg", "jpeg", "png", "bmp", "tiff", "tif", "webp"):
            path = self.save(f"格式.{extension}")
            image = viewer.load_image(str(path))
            self.assertEqual((image.width(), image.height()), (80, 60))
            path.unlink()
        good = self.save("有效.png")
        os.utime(good, (100, 100))
        (self.folder / "损坏.png").write_bytes(b"not an image")
        self.start()
        self.wait_for(lambda: self.displayed(good.name))
        self.wait_for(lambda: "无法读取" in self.window.statusBar().currentMessage(), timeout=15)
        self.assertTrue(self.displayed(good.name))
        self.save("恢复.png")
        self.wait_for(lambda: self.displayed("恢复.png"))


if __name__ == "__main__":
    unittest.main()
