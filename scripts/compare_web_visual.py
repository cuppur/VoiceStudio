"""Compare two page captures pixel by pixel.

Used to prove the WebEngine shell matches the Chromium baseline:

    python scripts/compare_web_visual.py <a.png> <b.png>
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PySide6.QtGui import QImage


def _load(path: Path):
    """Return an HxWx3 int16 array, using Pillow+numpy when available."""
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None
    with Image.open(path) as image:
        return np.asarray(image.convert("RGB"), dtype=np.int16)


def _compare_fast(left, right, threshold: int) -> tuple[int, int, tuple[int, int], int, int]:
    import numpy as np

    height = min(left.shape[0], right.shape[0])
    width = min(left.shape[1], right.shape[1])
    delta = np.abs(left[:height, :width] - right[:height, :width]).sum(axis=2)
    mask = delta > threshold
    different = int(mask.sum())
    worst_index = int(delta.argmax())
    return different, worst_index, (width, height), int(delta.max()), height * width


def compare(left_path: Path, right_path: Path, threshold: int = 0) -> int:
    fast_left, fast_right = _load(left_path), _load(right_path)
    if fast_left is not None and fast_right is not None:
        if fast_left.shape != fast_right.shape:
            print(f"尺寸不同（{fast_left.shape[1]}x{fast_left.shape[0]} vs {fast_right.shape[1]}x{fast_right.shape[0]}），比较交集")
        different, worst_index, (width, height), worst, total = _compare_fast(fast_left, fast_right, threshold)
        print(
            f"{left_path.name} vs {right_path.name}: {different}/{total} 像素不同 "
            f"({different / total * 100:.3f}%)，最大通道差 {worst} @ ({worst_index % width}, {worst_index // width})"
        )
        return 0 if different == 0 else 1

    left, right = QImage(str(left_path)), QImage(str(right_path))
    if left.isNull() or right.isNull():
        print("无法读取图像")
        return 2
    if left.size() != right.size():
        width = min(left.width(), right.width())
        height = min(left.height(), right.height())
        print(f"尺寸不同（{left.size().toTuple()} vs {right.size().toTuple()}），比较交集 {width}x{height}")
        left = left.copy(0, 0, width, height)
        right = right.copy(0, 0, width, height)
    different = 0
    worst = 0
    worst_point = (0, 0)
    for y in range(left.height()):
        for x in range(left.width()):
            first, second = left.pixel(x, y), right.pixel(x, y)
            if first == second:
                continue
            delta = (
                abs(((first >> 16) & 255) - ((second >> 16) & 255))
                + abs(((first >> 8) & 255) - ((second >> 8) & 255))
                + abs((first & 255) - (second & 255))
            )
            if delta > threshold:
                different += 1
                if delta > worst:
                    worst, worst_point = delta, (x, y)
    total = left.width() * left.height()
    print(
        f"{left_path.name} vs {right_path.name}: {different}/{total} 像素不同 "
        f"({different / total * 100:.3f}%)，最大通道差 {worst} @ {worst_point}"
    )
    return 0 if different == 0 else 1


def grid(left_path: Path, right_path: Path, columns: int = 12, rows: int = 8) -> int:
    """Print a coarse per-cell average-colour diff so offsets are easy to spot."""
    fast_left, fast_right = _load(left_path), _load(right_path)
    if fast_left is not None and fast_right is not None:
        import numpy as np

        height = min(fast_left.shape[0], fast_right.shape[0])
        width = min(fast_left.shape[1], fast_right.shape[1])
        delta = np.abs(fast_left[:height, :width] - fast_right[:height, :width]).sum(axis=2).astype("float64") / 3.0
        cell_h, cell_w = max(1, height // rows), max(1, width // columns)
        print(f"网格 {columns}x{rows}，单元格 {cell_w}x{cell_h}px，平均通道差：")
        for row in range(rows):
            values = []
            for column in range(columns):
                cell = delta[row * cell_h:(row + 1) * cell_h, column * cell_w:(column + 1) * cell_w]
                values.append(f"{cell.mean():5.1f}" if cell.size else "  0.0")
            print(" ".join(values))
        return 0

    left, right = QImage(str(left_path)), QImage(str(right_path))
    if left.isNull() or right.isNull():
        print("无法读取图像")
        return 2
    width = min(left.width(), right.width())
    height = min(left.height(), right.height())
    cell_w, cell_h = max(1, width // columns), max(1, height // rows)
    print(f"网格 {columns}x{rows}，单元格 {cell_w}x{cell_h}px，平均通道差：")
    for row in range(rows):
        line = []
        for column in range(columns):
            x0, y0 = column * cell_w, row * cell_h
            total = [0, 0, 0]
            count = 0
            for y in range(y0, min(y0 + cell_h, height), max(1, cell_h // 12)):
                for x in range(x0, min(x0 + cell_w, width), max(1, cell_w // 12)):
                    a, b = left.pixelColor(x, y), right.pixelColor(x, y)
                    total[0] += abs(a.red() - b.red())
                    total[1] += abs(a.green() - b.green())
                    total[2] += abs(a.blue() - b.blue())
                    count += 1
            delta = sum(total) / (3 * max(1, count))
            line.append(f"{delta:5.1f}")
        print(" ".join(line))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("left")
    parser.add_argument("right")
    parser.add_argument("--threshold", type=int, default=0, help="忽略小于该通道差的像素")
    parser.add_argument("--grid", type=int, default=0, help="输出 NxN 网格平均色差")
    options = parser.parse_args()
    if options.grid:
        return grid(Path(options.left), Path(options.right), options.grid, max(2, options.grid // 2))
    return compare(Path(options.left), Path(options.right), options.threshold)


if __name__ == "__main__":
    raise SystemExit(main())
