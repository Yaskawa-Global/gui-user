"""X11 screenshot capture via ImageMagick import."""

import io
import logging
import os
import subprocess

from .errors import DisplayError
from .window import WindowTracker

logger = logging.getLogger("gui-user.screenshot")


class ScreenshotCapture:
    """Capture screenshots from an Xvfb display."""

    def __init__(self, display: str, pid: int | None = None):
        self._display = display
        self._window_tracker = WindowTracker(display, pid) if pid is not None else None

    def capture(self, region: tuple[int, int, int, int] | None = None) -> bytes:
        """Return PNG bytes of the screen (active window, or full screen fallback).

        Args:
            region: Optional (x, y, width, height) to crop the screenshot.
        """
        env = {**os.environ, "DISPLAY": self._display}

        png = None

        if self._window_tracker is not None:
            window_id = self._window_tracker.get_preferred_window_id()
            if window_id:
                png = self._import_window(window_id, env)
                if png:
                    logger.debug(f"Captured target window {window_id} ({len(png)} bytes)")

        if png is None:
            # Try active window first
            try:
                wid_result = subprocess.run(
                    ["xdotool", "getactivewindow"],
                    env=env, capture_output=True, text=True, timeout=5,
                )
                if wid_result.returncode == 0 and wid_result.stdout.strip():
                    window_id = wid_result.stdout.strip()
                    png = self._import_window(window_id, env)
                    if png:
                        logger.debug(f"Captured active window {window_id} ({len(png)} bytes)")
            except Exception as e:
                logger.debug(f"Active window capture failed: {e}")

        if png is None:
            # Fallback: full screen
            png = self._import_window("root", env)
            if not png:
                raise DisplayError("Screenshot capture failed: no output from import")
            logger.debug(f"Captured root window ({len(png)} bytes)")

        if region is not None:
            png = self._crop(png, region)

        return png

    def capture_to_file(self, path: str) -> str:
        """Save PNG to file, return the path."""
        png = self.capture()
        with open(path, "wb") as f:
            f.write(png)
        return path

    @staticmethod
    def _crop(png_bytes: bytes, region: tuple[int, int, int, int]) -> bytes:
        """Crop PNG bytes to (x, y, width, height) using Pillow."""
        from PIL import Image
        x, y, w, h = region
        img = Image.open(io.BytesIO(png_bytes))
        cropped = img.crop((x, y, x + w, y + h))
        buf = io.BytesIO()
        cropped.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def ocr(png_bytes: bytes, min_confidence: int = 40) -> list[dict]:
        """Run Tesseract OCR on PNG bytes and return text elements with bounding boxes.

        Runs OCR twice — once on the original image (for dark-on-light text)
        and once on an inverted version (for light-on-dark text) — then merges
        results, keeping the higher-confidence detection when duplicates overlap.

        Args:
            png_bytes: PNG image data.
            min_confidence: Minimum confidence (0-100) to include a result.

        Returns a list of dicts with keys: text, bounds (x,y,w,h), center (x,y), confidence.
        """
        import shutil

        tesseract = shutil.which("tesseract")
        if not tesseract:
            logger.warning("tesseract not found; OCR not available. Install: sudo apt install tesseract-ocr")
            return []

        from PIL import Image, ImageEnhance, ImageOps

        gray = Image.open(io.BytesIO(png_bytes)).convert("L")

        # Pass 1: raw grayscale (let tesseract use its own adaptive thresholding)
        elements_raw = ScreenshotCapture._run_tesseract(tesseract, gray, min_confidence)

        # Pass 2: binarized (dark text on light background)
        binarized = gray.point(lambda p: 255 if p > 180 else 0)
        elements_bin = ScreenshotCapture._run_tesseract(tesseract, binarized, min_confidence)

        # Pass 3: contrast-enhanced + binarized (finds text that contrast boost reveals)
        enhanced = ImageEnhance.Contrast(gray).enhance(2.5)
        enhanced_bin = enhanced.point(lambda p: 255 if p > 180 else 0)
        elements_enh = ScreenshotCapture._run_tesseract(tesseract, enhanced_bin, min_confidence)

        merged = ScreenshotCapture._merge_ocr_results(elements_raw, elements_bin)
        return ScreenshotCapture._merge_ocr_results(merged, elements_enh)

    @staticmethod
    def _run_tesseract(tesseract: str, img, min_confidence: int) -> list[dict]:
        """Run tesseract on a PIL Image and return parsed elements."""
        import csv
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            img.save(f, format="PNG")
            tmp_path = f.name

        try:
            result = subprocess.run(
                [tesseract, tmp_path, "stdout", "--psm", "11", "tsv"],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode != 0:
                logger.warning(f"tesseract failed: {result.stderr.strip()[:200]}")
                return []
        finally:
            os.unlink(tmp_path)

        elements = []
        reader = csv.DictReader(result.stdout.strip().splitlines(), delimiter="\t")
        for row in reader:
            text = (row.get("text") or "").strip()
            if not text:
                continue
            try:
                conf = int(float(row.get("conf", "-1")))
            except (ValueError, TypeError):
                continue
            if conf < min_confidence:
                continue
            try:
                x = int(row["left"])
                y = int(row["top"])
                w = int(row["width"])
                h = int(row["height"])
            except (KeyError, ValueError):
                continue
            elements.append({
                "text": text,
                "bounds": [x, y, w, h],
                "center": [x + w // 2, y + h // 2],
                "confidence": conf,
            })
        return elements

    @staticmethod
    def _merge_ocr_results(a: list[dict], b: list[dict]) -> list[dict]:
        """Merge two OCR result lists, deduplicating overlapping detections.

        When two elements have overlapping boxes:
        - If the text is similar (one contains the other), it's a duplicate —
          keep the longer text, or higher confidence if same length.
        - If the text is different, keep both (different content at nearby positions).
        - If one box fully contains a smaller single-char detection, the larger
          detection is preferred.
        """
        merged = list(a)
        for elem_b in b:
            dup_index = None
            for i, elem_a in enumerate(merged):
                if not ScreenshotCapture._boxes_overlap(elem_a["bounds"], elem_b["bounds"]):
                    continue
                # Boxes overlap — check text similarity
                ta = elem_a["text"].lower()
                tb = elem_b["text"].lower()
                if ta == tb or ta in tb or tb in ta:
                    # Same or substring — it's a duplicate
                    dup_index = i
                    break
            if dup_index is not None:
                existing = merged[dup_index]
                # Prefer longer text (more complete read), then higher confidence
                if (len(elem_b["text"]) > len(existing["text"]) or
                    (len(elem_b["text"]) == len(existing["text"]) and
                     elem_b["confidence"] > existing["confidence"])):
                    merged[dup_index] = elem_b
            else:
                merged.append(elem_b)
        return merged

    @staticmethod
    def _boxes_overlap(a: list[int], b: list[int], threshold: float = 0.5) -> bool:
        """Check if two [x, y, w, h] boxes overlap by more than threshold of the smaller area."""
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        ix = max(0, min(ax + aw, bx + bw) - max(ax, bx))
        iy = max(0, min(ay + ah, by + bh) - max(ay, by))
        intersection = ix * iy
        if intersection == 0:
            return False
        smaller_area = min(aw * ah, bw * bh)
        if smaller_area == 0:
            return False
        return intersection / smaller_area > threshold

    @staticmethod
    def _import_window(window: str, env: dict) -> bytes | None:
        """Use ImageMagick import to capture a window, return PNG bytes or None."""
        try:
            result = subprocess.run(
                ["import", "-window", window, "png:-"],
                env=env, capture_output=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout
            if result.stderr:
                logger.debug(f"import stderr: {result.stderr.decode(errors='replace')[:200]}")
            return None
        except subprocess.TimeoutExpired:
            logger.warning("import command timed out")
            return None
