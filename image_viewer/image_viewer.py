"""Windows 文件夹最新图像查看器。运行：python image_viewer.py [文件夹路径]"""

from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QFile, QIODevice, QRectF, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QImageReader, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QVBoxLayout, QWidget,
)
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer


EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif", ".webp"}
QUIET_SECONDS = 0.30
RETRY_SECONDS = 0.25
MAX_ATTEMPTS = 40


def path_key(path: str) -> str:
    return os.path.normcase(os.path.abspath(path))


def signature(path: str) -> tuple[int, int, int]:
    stat = os.stat(path)
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def load_image(path: str) -> QImage:
    """解码结束即关闭文件，不让已显示的图像占用源文件。"""
    source = QFile(path)
    if not source.open(QIODevice.OpenModeFlag.ReadOnly):
        raise OSError(source.errorString())
    try:
        reader = QImageReader(source)
        reader.setAutoTransform(True)
        image = reader.read()
        if image.isNull():
            raise ValueError(reader.errorString())
        return image
    finally:
        source.close()


@dataclass
class Candidate:
    path: str
    order: int
    changed_at: float
    next_try: float
    revision: int = 0
    attempts: int = 0
    fingerprint: tuple[int, int, int] | None = None
    stable_since: float = 0.0


class FolderEvents(FileSystemEventHandler):
    """watchdog 回调只投递事件，绝不操作 Qt 控件。"""

    def __init__(self, folder: str, events: queue.Queue):
        super().__init__()
        self.folder = path_key(folder)
        self.events = events

    def post(self, kind: str, path: str) -> None:
        if (path_key(os.path.dirname(path)) == self.folder
                and Path(path).suffix.lower() in EXTENSIONS):
            self.events.put((kind, path))

    def on_created(self, event):
        if not event.is_directory:
            self.post("arrival", event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            self.post("modify", event.src_path)

    def on_moved(self, event):
        if not event.is_directory:
            self.post("delete", event.src_path)
            self.post("arrival", event.dest_path)

    def on_deleted(self, event):
        if not event.is_directory:
            self.post("delete", event.src_path)


class FolderWorker(QThread):
    image_ready = Signal(int, str, QImage)
    status = Signal(int, str)

    def __init__(self, session: int, folder: str, parent=None):
        super().__init__(parent)
        self.session = session
        self.folder = folder
        self.events: queue.Queue = queue.Queue()
        self.stopping = threading.Event()
        self.pending: dict[str, Candidate] = {}
        self.orders: dict[str, int] = {}
        self.sequence = 0
        self.displayed_order = -float("inf")
        self.displayed_key: str | None = None
        self.displayed_fingerprint: tuple[int, int, int] | None = None

    def stop(self) -> None:
        self.stopping.set()
        self.events.put(("wake", ""))

    def say(self, text: str) -> None:
        self.status.emit(self.session, text)

    def scan_initial(self) -> None:
        files = []
        with os.scandir(self.folder) as entries:
            for entry in entries:
                if self.stopping.is_set():
                    return
                if Path(entry.name).suffix.lower() not in EXTENSIONS:
                    continue
                try:
                    if entry.is_file():
                        files.append((entry.stat().st_mtime_ns, entry.name.casefold(), entry.path))
                except OSError:
                    continue
        files.sort()
        now = time.monotonic()
        # 初始图像均使用负序号，监听到的新导入事件始终优先。
        for index, (_, _, path) in enumerate(files):
            key = path_key(path)
            order = index - len(files)
            self.orders[key] = order
            self.pending[key] = Candidate(path, order, now, now)
        if files:
            self.say("正在加载最新图像…")
        else:
            self.say("暂无图像，等待新图像导入")

    def accept_event(self, kind: str, path: str) -> None:
        if kind == "wake":
            return
        key = path_key(path)
        if kind == "delete":
            self.pending.pop(key, None)
            self.orders.pop(key, None)
            if key == self.displayed_key:
                self.say("当前图像已删除或移出；保留预览，等待新图像")
            return
        now = time.monotonic()
        candidate = self.pending.get(key)
        if kind == "arrival":
            # 合并尚未完成的同一路径创建事件；首次扫描得到的候选需提升优先级。
            if candidate is None or candidate.order < 0:
                self.sequence += 1
                self.orders[key] = self.sequence
                candidate = None
        elif key not in self.orders:
            self.sequence += 1
            self.orders[key] = self.sequence
        order = self.orders[key]
        if order < self.displayed_order:
            return
        if candidate is None:
            candidate = Candidate(path, order, now, now + QUIET_SECONDS)
            self.pending[key] = candidate
            self.say("检测到图像变化，等待写入完成…")
        else:
            candidate.changed_at = now
            candidate.next_try = now + QUIET_SECONDS
            candidate.revision += 1

    def drain_events(self) -> None:
        # 限制一次处理数量，连续大量事件时也能响应停止操作。
        for _ in range(2048):
            if self.stopping.is_set():
                break
            try:
                kind, path = self.events.get_nowait()
            except queue.Empty:
                break
            self.accept_event(kind, path)

    def attempt(self, key: str, candidate: Candidate) -> None:
        now = time.monotonic()
        candidate.attempts += 1
        candidate.next_try = now + RETRY_SECONDS
        error = "文件仍在写入或暂时不可访问"
        try:
            before = signature(candidate.path)
            if before != candidate.fingerprint:
                candidate.fingerprint = before
                candidate.stable_since = now
                return
            if now - candidate.stable_since < QUIET_SECONDS:
                return
            if before[0] == 0:
                raise ValueError("图像文件为空")
            if (key == self.displayed_key
                    and before == self.displayed_fingerprint):
                self.pending.pop(key, None)
                self.say("正在监听，等待新图像")
                return
            revision = candidate.revision
            image = load_image(candidate.path)
            if signature(candidate.path) != before:
                candidate.fingerprint = None
                return
            # 解码期间可能有覆盖、删除或切换操作，发送结果前再检查。
            self.drain_events()
            if (self.stopping.is_set() or self.pending.get(key) is not candidate
                    or candidate.revision != revision):
                return
            if candidate.order >= self.displayed_order:
                self.displayed_order = candidate.order
                self.displayed_key = key
                self.displayed_fingerprint = before
                self.image_ready.emit(self.session, candidate.path, image)
                self.say("正在监听，等待新图像")
                # 已显示较新图像后，较旧加载任务不能再覆盖它。
                for old_key, old in list(self.pending.items()):
                    if old.order <= candidate.order:
                        self.pending.pop(old_key, None)
        except (OSError, ValueError) as exc:
            error = str(exc)
        finally:
            if (candidate.attempts >= MAX_ATTEMPTS
                    and self.pending.get(key) is candidate):
                self.pending.pop(key, None)
                self.say(f"无法读取 {Path(candidate.path).name}：{error}；继续监听")

    def run(self) -> None:
        observer = Observer()
        try:
            if not os.path.isdir(self.folder):
                raise NotADirectoryError(f"文件夹不存在：{self.folder}")
            # 先监听再扫描，避免初始化期间漏掉新文件。
            observer.schedule(FolderEvents(self.folder, self.events), self.folder, recursive=False)
            observer.start()
            self.scan_initial()
            next_health_check = time.monotonic() + 2.0
            while not self.stopping.is_set():
                self.drain_events()
                now = time.monotonic()
                ready = sorted(
                    ((key, item) for key, item in self.pending.items() if item.next_try <= now),
                    key=lambda pair: pair[1].order, reverse=True,
                )
                # 每轮最多解码一个候选，事件和停止请求优先处理。
                if ready and self.events.empty():
                    self.attempt(*ready[0])
                if now >= next_health_check:
                    if not os.path.isdir(self.folder):
                        raise OSError("监听文件夹已删除、移动或无法访问，请重新选择文件夹")
                    if not observer.is_alive() or any(not item.is_alive() for item in observer.emitters):
                        raise OSError("文件监听已中断，请重新选择文件夹")
                    next_health_check = now + 2.0
                try:
                    kind, path = self.events.get(timeout=0.05)
                    self.accept_event(kind, path)
                except queue.Empty:
                    pass
        except Exception as exc:
            self.say(f"监听失败：{exc}")
        finally:
            observer.stop()
            if observer.ident is not None:
                observer.join()


class ImageCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(320, 240)
        self.pixmap = QPixmap()
        self.message = "请选择一个图像文件夹"

    def clear(self, message: str) -> None:
        self.pixmap = QPixmap()
        self.message = message
        self.update()

    def set_image(self, image: QImage) -> None:
        self.pixmap = QPixmap.fromImage(image)
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#202124"))
        if self.pixmap.isNull():
            painter.setPen(QColor("#dddddd"))
            painter.drawText(self.rect().adjusted(20, 20, -20, -20),
                             Qt.AlignmentFlag.AlignCenter | Qt.TextFlag.TextWordWrap, self.message)
            return
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        scale = min(self.width() / self.pixmap.width(), self.height() / self.pixmap.height())
        width, height = self.pixmap.width() * scale, self.pixmap.height() * scale
        target = QRectF((self.width() - width) / 2, (self.height() - height) / 2, width, height)
        painter.drawPixmap(target, self.pixmap, QRectF(self.pixmap.rect()))


class ImageViewer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("文件夹最新图像实时显示")
        self.resize(1100, 780)
        self.session = 0
        self.workers: list[FolderWorker] = []
        self.closing = False

        self.path_input = QLineEdit()
        self.path_input.setPlaceholderText(r"输入文件夹路径，例如 C:\Users\mechmind_\Desktop\22个\22个原图")
        self.path_input.returnPressed.connect(self.start_folder)
        self.choose_button = QPushButton("选择文件夹")
        self.choose_button.clicked.connect(self.choose_folder)
        self.start_button = QPushButton("开始监听")
        self.start_button.clicked.connect(self.start_folder)
        toolbar = QHBoxLayout()
        toolbar.addWidget(self.path_input, 1)
        toolbar.addWidget(self.choose_button)
        toolbar.addWidget(self.start_button)

        self.folder_label = QLabel("当前监听路径：未选择")
        self.folder_label.setWordWrap(True)
        self.info_label = QLabel("图像：—    尺寸：—")
        self.info_label.setWordWrap(True)
        for label in (self.folder_label, self.info_label):
            label.setTextFormat(Qt.TextFormat.PlainText)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.canvas = ImageCanvas()
        layout = QVBoxLayout()
        layout.addLayout(toolbar)
        layout.addWidget(self.folder_label)
        layout.addWidget(self.canvas, 1)
        layout.addWidget(self.info_label)
        root = QWidget()
        root.setLayout(layout)
        self.setCentralWidget(root)
        self.statusBar().showMessage("请选择文件夹或输入路径后按回车")

    def choose_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择图像文件夹", self.path_input.text())
        if folder:
            self.path_input.setText(folder)
            self.start_folder()

    def start_folder(self) -> None:
        text = self.path_input.text().strip().strip('"')
        if not text:
            self.statusBar().showMessage("请先输入或选择文件夹路径")
            return
        folder = os.path.abspath(os.path.expandvars(os.path.expanduser(text)))
        self.session += 1
        for worker in self.workers:
            worker.stop()
        self.path_input.setText(folder)
        self.folder_label.setText(f"当前监听路径：{folder}")
        self.canvas.clear("正在检查文件夹并查找最新图像…")
        self.info_label.setText("图像：—    尺寸：—")
        worker = FolderWorker(self.session, folder, self)
        worker.image_ready.connect(self.show_image)
        worker.status.connect(self.show_status)
        worker.finished.connect(self.reap_workers)
        self.workers.append(worker)
        self.statusBar().showMessage("正在启动监听…")
        worker.start()

    def show_image(self, session: int, path: str, image: QImage) -> None:
        if session != self.session or self.closing:
            return
        self.canvas.set_image(image)
        self.info_label.setText(f"图像：{Path(path).name}    尺寸：{image.width()} × {image.height()} 像素")
        self.info_label.setToolTip(path)

    def show_status(self, session: int, text: str) -> None:
        if session != self.session or self.closing:
            return
        self.statusBar().showMessage(text)
        if self.canvas.pixmap.isNull():
            self.canvas.clear(text)

    def reap_workers(self) -> None:
        for worker in self.workers[:]:
            if worker.isFinished():
                self.workers.remove(worker)
                worker.deleteLater()
        if self.closing and not self.workers:
            QTimer.singleShot(0, self.close)

    def closeEvent(self, event) -> None:
        if not self.workers:
            event.accept()
            return
        # 异步等待解码及监听线程自然结束，避免销毁运行中的 QThread。
        self.closing = True
        self.centralWidget().setEnabled(False)
        self.statusBar().showMessage("正在停止监听并清理后台线程…")
        for worker in self.workers:
            worker.stop()
        event.ignore()
        self.reap_workers()


def main() -> int:
    parser = argparse.ArgumentParser(description="实时显示指定文件夹中最新导入的图像")
    parser.add_argument("folder", nargs="?", help="可选：启动时直接监听的文件夹")
    args = parser.parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("实时图像查看器")
    window = ImageViewer()
    window.show()
    if args.folder:
        window.path_input.setText(args.folder)
        QTimer.singleShot(0, window.start_folder)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
