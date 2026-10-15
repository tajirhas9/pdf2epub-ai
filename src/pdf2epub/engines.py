"""Vision engines behind small interfaces so stages don't depend on a specific toolkit.

Paddle models are loaded lazily, once per process, and reused for every page.
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from functools import cache
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)


@dataclass
class Region:
    label: str  # normalized: title, heading, text, figure, caption, table, formula, header, footer,
    #             page_number, footnote, aside, list, seal…
    score: float
    box: tuple[float, float, float, float]  # pixels x0, y0, x1, y1


@dataclass
class OcrLine:
    text: str
    score: float
    box: tuple[float, float, float, float]  # pixels
    script: str = "latin"


class LayoutEngine(Protocol):
    def detect(self, images: list[np.ndarray]) -> list[list[Region]]: ...


class OcrEngine(Protocol):
    def detect_lines(self, image: np.ndarray) -> list[tuple[float, float, float, float]]: ...

    def recognize(self, crops: list[np.ndarray], script: str) -> list[tuple[str, float]]: ...


# PP-DocLayout labels -> our vocabulary.
_LAYOUT_LABELS = {
    "doc_title": "title",
    "paragraph_title": "heading",
    "text": "text",
    "abstract": "text",
    "content": "text",
    "reference": "text",
    "reference_content": "text",
    "algorithm": "text",
    "image": "figure",
    "chart": "figure",
    "figure_title": "caption",
    "table_title": "caption",
    "chart_title": "caption",
    "table": "table",
    "formula": "formula",
    "display_formula": "formula",
    "inline_formula": "formula",
    "formula_number": "text",
    "header": "header",
    "header_image": "header",
    "footer": "footer",
    "footer_image": "footer",
    "number": "page_number",
    "footnote": "footnote",
    "vision_footnote": "footnote",
    "aside_text": "aside",
    "seal": "figure",
}

# Recognition model per script (PP-OCRv5). Detection is script-agnostic.
SCRIPT_REC_MODELS = {
    "latin": "latin_PP-OCRv5_mobile_rec",
    "en": "en_PP-OCRv5_mobile_rec",
    "arabic": "arabic_PP-OCRv5_mobile_rec",
    "cyrillic": "cyrillic_PP-OCRv5_mobile_rec",
    "devanagari": "devanagari_PP-OCRv5_mobile_rec",
    "cjk": "PP-OCRv5_server_rec",
    "korean": "korean_PP-OCRv5_mobile_rec",
    "greek": "el_PP-OCRv5_mobile_rec",
    "thai": "th_PP-OCRv5_mobile_rec",
    "tamil": "ta_PP-OCRv5_mobile_rec",
    "telugu": "te_PP-OCRv5_mobile_rec",
}


def _device(use_gpu: bool) -> str:
    """Paddle device string: the accelerator this Paddle build was compiled for, if one is present."""
    if not use_gpu:
        return "cpu"
    try:
        import paddle

        dev = paddle.device
        if (dev.is_compiled_with_cuda() or dev.is_compiled_with_rocm()) and dev.cuda.device_count():
            return "gpu:0"
        if dev.is_compiled_with_xpu() and dev.xpu.device_count():
            return "xpu:0"
        for kind in dev.get_all_custom_device_type() or []:  # Ascend NPU, MLU, … plugin builds
            return f"{kind}:0"
    except Exception:
        pass
    return "cpu"


def _small_gpu() -> bool:
    """Under 4 GB of video memory: keep models and batches small."""
    from pdf2epub.hardware import probe

    mem = [g.mem_mb for g in probe().nvidia if g.mem_mb]
    return bool(mem) and mem[0] < 4096


class PaddleLayout:
    def __init__(self, use_gpu: bool = True, model: str = "PP-DocLayout_plus-L"):
        self.use_gpu = use_gpu
        self.model_name = model

    @property
    def _model(self):
        return _paddle_layout(self.model_name, _device(self.use_gpu))

    def detect(self, images: list[np.ndarray]) -> list[list[Region]]:
        out: list[list[Region]] = []
        for res in self._model.predict(
            [_bgr(i) for i in images], batch_size=min(1 if _small_gpu() else 4, len(images)), layout_nms=True
        ):
            boxes = res.json["res"]["boxes"]
            out.append(
                [
                    Region(
                        label=_LAYOUT_LABELS.get(b["label"], "text"),
                        score=float(b["score"]),
                        box=tuple(float(v) for v in b["coordinate"]),
                    )
                    for b in boxes
                ]
            )
        return out


def det_model_for(device: str, choice: str = "auto") -> str:
    """Line detector: server model on an accelerator; on CPU (or a small GPU) the mobile model finds the
    same lines ~16x faster."""
    if choice != "auto":
        return choice
    return "PP-OCRv5_mobile_det" if device == "cpu" or _small_gpu() else "PP-OCRv5_server_det"


class PaddleOcr:
    def __init__(self, use_gpu: bool = True, det_model: str = "auto"):
        self.use_gpu = use_gpu
        self.det_model = det_model

    def detect_lines(self, image: np.ndarray) -> list[tuple[float, float, float, float]]:
        device = _device(self.use_gpu)
        model = _paddle_det(det_model_for(device, self.det_model), device)
        res = next(iter(model.predict(_bgr(image), batch_size=1)))
        boxes = []
        for poly in res.json["res"]["dt_polys"]:
            xs = [p[0] for p in poly]
            ys = [p[1] for p in poly]
            boxes.append((min(xs), min(ys), max(xs), max(ys)))
        return boxes

    def recognize(self, crops: list[np.ndarray], script: str) -> list[tuple[str, float]]:
        if not crops:
            return []
        model = _paddle_rec(SCRIPT_REC_MODELS.get(script, SCRIPT_REC_MODELS["latin"]), _device(self.use_gpu))
        out = []
        for res in model.predict([_bgr(c) for c in crops], batch_size=32):
            r = res.json["res"]
            out.append((r.get("rec_text", ""), float(r.get("rec_score", 0.0))))
        return out


# ---- Apple Vision (macOS) -----------------------------------------------------------------------
# VNRecognizeTextRequest ships with macOS and runs on the Neural Engine / GPU (Apple silicon) or
# CPU/GPU (Intel); Paddle has no Metal backend, so on a Mac this is both faster and download-free.
# Scripts Vision can't read (Devanagari, Tamil, Greek, Hebrew…) are delegated to Paddle.

# Our script -> Vision language codes, preferred first (filtered by what this macOS supports).
SCRIPT_VISION_LANGS = {
    "latin": ["en-US", "fr-FR", "de-DE", "es-ES", "it-IT", "pt-BR", "nl-NL", "pl-PL", "tr-TR", "id-ID",
              "ms-MY", "cs-CZ", "da-DK", "sv-SE", "nb-NO", "ro-RO", "vi-VT"],
    "en": ["en-US"],
    "cjk": ["zh-Hans", "zh-Hant", "ja-JP"],
    "korean": ["ko-KR"],
    "cyrillic": ["ru-RU", "uk-UA"],
    "arabic": ["ar-SA"],
    "thai": ["th-TH"],
}  # fmt: skip
OCR_ENGINES = ("auto", "apple", "paddle")


@cache
def _vision_languages() -> frozenset[str]:
    """Languages the accurate recognizer of this macOS supports (empty: Vision unavailable)."""
    if sys.platform != "darwin":
        return frozenset()
    try:
        import Vision

        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelAccurate)
        langs, _ = req.supportedRecognitionLanguagesAndReturnError_(None)
        return frozenset(str(x) for x in langs or ())
    except Exception:
        return frozenset()


def apple_vision_available() -> bool:
    return bool(_vision_languages())


def resolve_ocr_engine(choice: str = "auto") -> str:
    """Engine for a setting: "auto" -> Apple Vision on a Mac that has it, PaddleOCR everywhere else."""
    return "apple" if choice in ("auto", "apple") and apple_vision_available() else "paddle"


def vision_langs(script: str, prefer: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Vision languages for `script` this macOS supports; languages in `prefer` (e.g. "fr") go first."""
    supported = _vision_languages()
    langs = [lang for lang in SCRIPT_VISION_LANGS.get(script, []) if lang in supported]
    first = [lang for lang in langs if lang.split("-")[0] in prefer]
    return first + [lang for lang in langs if lang not in first]


def vision_box(bbox, width: int, height: int) -> tuple[float, float, float, float]:
    """Vision's normalized, bottom-left-origin rect -> pixel (x0, y0, x1, y1) with a top-left origin."""
    (x, y), (w, h) = bbox
    return (x * width, (1 - y - h) * height, (x + w) * width, (1 - y) * height)


def _cg_image(rgb: np.ndarray):
    import Quartz

    if rgb.ndim == 2:
        rgb = np.stack([rgb] * 3, axis=-1)
    rgb = np.ascontiguousarray(rgb[:, :, :3], dtype=np.uint8)
    h, w = rgb.shape[:2]
    provider = Quartz.CGDataProviderCreateWithCFData(rgb.tobytes())
    return Quartz.CGImageCreate(
        w,
        h,
        8,
        24,
        w * 3,
        Quartz.CGColorSpaceCreateDeviceRGB(),
        Quartz.kCGImageAlphaNone,
        provider,
        None,
        False,
        Quartz.kCGRenderingIntentDefault,
    )


Box = tuple[float, float, float, float]


def _vision_read(rgb: np.ndarray, langs: list[str], fast: bool = False) -> list[tuple[Box, str, float]]:
    """All text lines Vision finds in the image: (pixel box, text, confidence)."""
    import objc
    import Vision

    h, w = rgb.shape[:2]
    with objc.autorelease_pool():
        req = Vision.VNRecognizeTextRequest.alloc().init()
        req.setRecognitionLevel_(
            Vision.VNRequestTextRecognitionLevelFast if fast else Vision.VNRequestTextRecognitionLevelAccurate
        )
        req.setUsesLanguageCorrection_(False)  # keep what is printed; dictionary repair happens later
        if langs:
            req.setRecognitionLanguages_(langs)
        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(_cg_image(rgb), {})
        ok, err = handler.performRequests_error_([req], None)
        if not ok:
            raise RuntimeError(f"Apple Vision text recognition failed: {err}")
        out = []
        for obs in req.results() or []:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            best = cands[0]
            out.append((vision_box(obs.boundingBox(), w, h), str(best.string()), float(best.confidence())))
        return out


def _uncovered(found: list[Box], covered: list[Box]) -> list[Box]:
    """Parts of `found` lines (fast pass) that no `covered` line (accurate pass) overlaps on the same row."""
    gaps = []
    for x0, y0, x1, y1 in found:
        h = y1 - y0
        spans = sorted(
            (c[0], c[2])
            for c in covered
            if min(y1, c[3]) - max(y0, c[1]) > 0.5 * min(h, c[3] - c[1]) and c[0] < x1 and c[2] > x0
        )
        start = x0
        for a, b in [*spans, (x1, x1)]:
            if a - start >= h:  # at least ~one character's worth of width
                gaps.append((start, y0, min(a, x1), y1))
            start = max(start, b)
    return gaps


def _read_gaps(rgb: np.ndarray, gaps: list[Box], langs: list[str]) -> list[tuple[Box, str, float]]:
    """Read many small regions with one accurate request: stack them into a single strip image."""
    crops, offsets, y = [], [], 0
    for x0, y0, x1, y1 in gaps:
        crop = _pad(rgb[int(y0) : int(y1) + 1, int(x0) : int(x1) + 1])
        p = (crop.shape[0] - (int(y1) + 1 - int(y0))) // 2
        crops.append(crop)
        offsets.append((y, crop.shape[0], int(x0) - p, int(y0) - p))
        y += crop.shape[0] + max(8, crop.shape[0] // 2)  # blank gap keeps the rows apart
    strip = np.full((y, max(c.shape[1] for c in crops), 3), 255, np.uint8)
    for crop, (top, h, _, _) in zip(crops, offsets, strict=True):
        strip[top : top + h, : crop.shape[1]] = crop if crop.ndim == 3 else crop[:, :, None]
    out = []
    for (bx0, by0, bx1, by1), text, score in _vision_read(strip, langs):
        mid = (by0 + by1) / 2
        hit = next((o for o in offsets if o[0] <= mid < o[0] + o[1]), None)
        text = text.strip(" .·…-_")  # dot leaders around table-of-contents numbers
        if hit is None or not any(ch.isalnum() for ch in text):
            continue
        top, _, dx, dy = hit
        out.append(((bx0 + dx, by0 - top + dy, bx1 + dx, by1 - top + dy), text, score))
    return out


def _vision_page(rgb: np.ndarray, langs: list[str]) -> list[tuple[Box, str, float]]:
    """Accurate full-page read, plus whatever the accurate model skipped but the fast one saw
    (it drops e.g. page numbers after dot leaders in a table of contents)."""
    lines = _vision_read(rgb, langs)
    gaps = _uncovered([b for b, _, _ in _vision_read(rgb, langs, fast=True)], [b for b, _, _ in lines])
    return lines + (_read_gaps(rgb, gaps, langs) if gaps else [])


class AppleVisionOcr:
    """Apple Vision for line detection + recognition; Paddle for scripts Vision can't read."""

    def __init__(self, langs: list[str] | tuple[str, ...] = (), fallback=None):
        self.langs = tuple(langs)
        self._fallback = fallback  # factory -> OcrEngine (lazily built: Paddle loads only when needed)
        self._paddle = None

    def supports(self, script: str) -> bool:
        return bool(vision_langs(script))

    def _other(self):
        if self._paddle is None:
            self._paddle = self._fallback() if self._fallback else PaddleOcr()
        return self._paddle

    def read_lines(self, image: np.ndarray, script: str) -> list[tuple[Box, str, float]]:
        """Detection and recognition in one pass over the page (much faster than line by line)."""
        if not self.supports(script):
            boxes = self.detect_lines(image)
            crops = [image[int(y0) : int(y1) + 1, int(x0) : int(x1) + 1] for x0, y0, x1, y1 in boxes]
            return [(b, t, s) for b, (t, s) in zip(boxes, self.recognize(crops, script), strict=False)]
        return _vision_page(image, vision_langs(script, self.langs))

    def detect_lines(self, image: np.ndarray) -> list[tuple[float, float, float, float]]:
        return [box for box, _, _ in _vision_page(image, vision_langs("latin", self.langs))]

    def recognize(self, crops: list[np.ndarray], script: str) -> list[tuple[str, float]]:
        if not crops:
            return []
        if not self.supports(script):
            return self._other().recognize(crops, script)
        langs = vision_langs(script, self.langs)
        out = []
        for crop in crops:
            pad = _pad(crop)  # Vision misses text that touches the image edge
            lines = sorted(_vision_read(pad, langs), key=lambda r: r[0][0])
            if not lines:
                out.append(("", 0.0))
                continue
            out.append((" ".join(t for _, t, _ in lines), min(s for _, _, s in lines)))
        return out


def _pad(img: np.ndarray, frac: float = 0.25) -> np.ndarray:
    h = img.shape[0]
    p = max(4, int(h * frac))
    widths = ((p, p), (p, p)) + (((0, 0),) if img.ndim == 3 else ())
    return np.pad(img, widths, mode="edge" if min(img.shape[:2]) else "constant")


def make_ocr(settings) -> OcrEngine:
    """The OCR engine the settings ask for (the one place engines are constructed)."""
    paddle = lambda: PaddleOcr(settings.use_gpu, settings.ocr_det_model)  # noqa: E731
    if resolve_ocr_engine(settings.ocr_engine) == "apple":
        return AppleVisionOcr(settings.langs, fallback=paddle)
    return paddle()


def ocr_engine_label(choice: str = "auto") -> str:
    if resolve_ocr_engine(choice) != "apple":
        return "PaddleOCR"
    from pdf2epub.hardware import probe

    return "Apple Vision (Neural Engine)" if probe().arch == "arm64" else "Apple Vision"


def preload(use_gpu: bool, script: str = "latin", det_model: str = "auto", ocr_engine: str = "auto") -> None:
    """Load layout, line detection and the primary recognition model once (cached per process)."""
    device = _device(use_gpu)
    blank = np.full((320, 240, 3), 255, np.uint8)
    blank[100:110, 40:200] = 0  # one fake text line
    # One tiny warm-up inference each: the first call compiles kernels (seconds on GPU).
    list(_paddle_layout("PP-DocLayout_plus-L", device).predict([blank], batch_size=1))
    if resolve_ocr_engine(ocr_engine) == "apple" and vision_langs(script):
        _vision_read(blank, vision_langs(script))
        return
    list(_paddle_det(det_model_for(device, det_model), device).predict(blank, batch_size=1))
    list(
        _paddle_rec(SCRIPT_REC_MODELS.get(script, SCRIPT_REC_MODELS["latin"]), device).predict(
            [blank[90:120]], batch_size=1
        )
    )


def _bgr(rgb: np.ndarray) -> np.ndarray:
    return np.ascontiguousarray(rgb[:, :, ::-1]) if rgb.ndim == 3 else rgb


def _require_paddle() -> None:
    try:
        import paddle  # noqa: F401
    except ImportError as e:
        raise RuntimeError(
            "PaddlePaddle is not installed. Run `pdf2epub install-paddle`: it picks the best build "
            "for this computer (NVIDIA GPU or CPU)."
        ) from e


# Paddle 3.x's oneDNN (MKL-DNN) CPU path crashes on the detection/layout models (PIR attribute
# error) but works for recognition, where it is ~5x faster. So: on for recognition only.
def _model_kwargs(device: str, kind: str) -> dict:
    if device == "cpu":
        return {"device": device, "enable_mkldnn": kind == "rec"}
    return {"device": device}


def _quiet_paddle() -> None:
    _require_paddle()
    os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
    os.environ.setdefault("GLOG_minloglevel", "2")


def _after_import() -> None:
    """PaddleX resets its logger to INFO on import; keep it to warnings unless -v."""
    level = logging.INFO if os.environ.get("PDF2EPUB_VERBOSE") else logging.WARNING
    for name in ("paddlex", "paddleocr", "httpx", "huggingface_hub", "modelscope"):
        logging.getLogger(name).setLevel(level)


@cache
def _paddle_layout(name: str, device: str):
    _quiet_paddle()
    from paddleocr import LayoutDetection

    _after_import()

    log.info("loading %s on %s", name, device)
    return LayoutDetection(model_name=name, **_model_kwargs(device, "layout"))


@cache
def _paddle_det(name: str, device: str):
    _quiet_paddle()
    from paddleocr import TextDetection

    _after_import()

    log.info("loading %s on %s", name, device)
    return TextDetection(model_name=name, **_model_kwargs(device, "det"))


@cache
def _paddle_rec(name: str, device: str):
    _quiet_paddle()
    from paddleocr import TextRecognition

    _after_import()

    log.info("loading %s on %s", name, device)
    return TextRecognition(model_name=name, **_model_kwargs(device, "rec"))
