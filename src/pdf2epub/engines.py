"""Vision engines behind small interfaces so stages don't depend on a specific toolkit.

Paddle models are loaded lazily, once per process, and reused for every page.
"""

from __future__ import annotations

import logging
import os
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
    if not use_gpu:
        return "cpu"
    try:
        import paddle

        return "gpu:0" if paddle.device.is_compiled_with_cuda() and paddle.device.cuda.device_count() else "cpu"
    except Exception:
        return "cpu"


class PaddleLayout:
    def __init__(self, use_gpu: bool = True, model: str = "PP-DocLayout_plus-L"):
        self.use_gpu = use_gpu
        self.model_name = model

    @property
    def _model(self):
        return _paddle_layout(self.model_name, _device(self.use_gpu))

    def detect(self, images: list[np.ndarray]) -> list[list[Region]]:
        out: list[list[Region]] = []
        for res in self._model.predict([_bgr(i) for i in images], batch_size=min(4, len(images)), layout_nms=True):
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
    """Line detector: server model on GPU; on CPU the mobile model finds the same lines ~16x faster."""
    if choice != "auto":
        return choice
    return "PP-OCRv5_server_det" if device.startswith("gpu") else "PP-OCRv5_mobile_det"


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


def preload(use_gpu: bool, script: str = "latin", det_model: str = "auto") -> None:
    """Load layout, line detection and the primary recognition model once (cached per process)."""
    device = _device(use_gpu)
    blank = np.full((320, 240, 3), 255, np.uint8)
    blank[100:110, 40:200] = 0  # one fake text line
    # One tiny warm-up inference each: the first call compiles kernels (seconds on GPU).
    list(_paddle_layout("PP-DocLayout_plus-L", device).predict([blank], batch_size=1))
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
            "PaddlePaddle is not installed. Install the tool with an extra: "
            "`pdf2epub[cpu]` or `pdf2epub[gpu]` (see README › Install)."
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
