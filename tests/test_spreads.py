import cv2
import numpy as np
import pytest

from pdf2epub.stages.spreads import register, stitch


def synthetic_picture(h=900, w=1200, seed=0):
    """A 'printed' picture: smooth gradients + lines crossing the middle (like map borders/coasts)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    img = np.stack([(xx / w) * 200 + 30, (yy / h) * 180 + 40, ((xx + yy) / (w + h)) * 150 + 60], axis=-1)
    for _ in range(25):
        y_a, y_b = rng.integers(0, h, 2)
        color = tuple(int(c) for c in rng.integers(0, 255, 3))
        cv2.line(img, (0, int(y_a)), (w - 1, int(y_b)), color, int(rng.integers(2, 6)))
    for _ in range(15):
        c = (int(rng.integers(0, w)), int(rng.integers(0, h)))
        cv2.circle(img, c, int(rng.integers(20, 120)), tuple(int(v) for v in rng.integers(0, 255, 3)), 3)
    return np.clip(img, 0, 255).astype(np.uint8)


def framed_picture(h=900, w=1200, seed=11):
    """Map-like page content: frames, horizontal rules, text-like baselines, filled regions."""
    rng = np.random.default_rng(seed)
    img = np.full((h, w, 3), 235, np.float32)
    img[:, :, 2] = 245
    for _ in range(12):
        x0, y0 = int(rng.integers(0, w - 200)), int(rng.integers(0, h - 150))
        cv2.rectangle(
            img,
            (x0, y0),
            (x0 + int(rng.integers(150, 600)), y0 + int(rng.integers(40, 300))),
            tuple(float(v) for v in rng.integers(40, 230, 3)),
            -1,
        )
    for _ in range(30):
        y = int(rng.integers(0, h))
        cv2.line(
            img, (0, y), (w - 1, y + int(rng.integers(-3, 4))), tuple(float(v) for v in rng.integers(0, 120, 3)), 2
        )
    cv2.rectangle(img, (5, 40), (w - 5, h - 40), (20, 20, 20), 3)
    return np.clip(img, 0, 255).astype(np.uint8)


def split(img, shift_y=0, scale=1.0, angle=0.0, overlap=0, gain=1.0):
    h, w = img.shape[:2]
    mid = w // 2
    left = img[:, :mid].copy()
    right = img[:, mid - overlap :].copy()
    # Apply the inverse of what register() must recover to the right half.
    M = cv2.getRotationMatrix2D((0.0, h / 2), -angle, 1 / scale)
    M[1, 2] -= shift_y
    right = cv2.warpAffine(right, M, (right.shape[1], h), borderMode=cv2.BORDER_REPLICATE)
    right = np.clip(right.astype(np.float32) * gain, 0, 255).astype(np.uint8)
    return left, right


@pytest.mark.parametrize(
    "shift_y,overlap,gain",
    [(0, 0, 1.0), (7, 0, 1.0), (-11, 0, 1.08), (5, 10, 0.93)],
)
def test_register_recovers_shift_and_overlap(shift_y, overlap, gain):
    img = synthetic_picture()
    left, right = split(img, shift_y=shift_y, overlap=overlap, gain=gain)
    h = img.shape[0]
    reg = register(left, right, 50, h - 50)
    assert reg.ncc > 0.6
    assert abs(reg.dy - shift_y) <= 1
    assert abs(reg.overlap - overlap) <= 1
    assert reg.drift <= 2.0
    assert reg.continuity <= 2.0


def test_register_corrects_small_rotation():
    img = synthetic_picture(seed=3)
    left, right = split(img, shift_y=4, angle=0.5)
    reg = register(left, right, 50, img.shape[0] - 50)
    assert reg.ncc > 0.5
    assert reg.drift <= 3.0


def test_unrelated_halves_score_low():
    a = synthetic_picture(seed=1)
    b = synthetic_picture(seed=2)
    left = a[:, :600]
    right = np.ascontiguousarray(np.flipud(b[:, 600:]))
    reg = register(left, right, 50, 850)
    aligned = synthetic_picture(seed=1)
    good = register(aligned[:, :600], aligned[:, 600:], 50, 850)
    assert reg.ncc < good.ncc


def test_stitch_restores_picture():
    img = synthetic_picture(seed=5)
    left, right = split(img, shift_y=6, overlap=8)
    h = img.shape[0]
    y0, y1 = 50, h - 50
    reg = register(left, right, y0, y1)
    out = stitch(left[y0:y1], right, reg, y0, y1)
    ref = img[y0:y1, : out.shape[1]]
    assert out.shape[1] == pytest.approx(img.shape[1], abs=3)
    diff = np.abs(out[:, 590:610].astype(int) - ref[:, 590:610].astype(int)).mean()
    assert diff < 12


@pytest.mark.parametrize("shift_y", [0, 9, -14])
def test_lost_gutter_still_aligns_vertically(shift_y):
    """Scanner cropped both pages at the binding: 30 columns of the picture are gone."""
    img = framed_picture()
    h, w = img.shape[:2]
    left = img[:, : w // 2 - 15].copy()
    right_src = img[:, w // 2 + 15 :].copy()
    M = np.float32([[1, 0, 0], [0, 1, -shift_y]])
    right = cv2.warpAffine(right_src, M, (right_src.shape[1], h), borderMode=cv2.BORDER_REPLICATE)
    reg = register(left, right, 60, h - 60)
    assert abs(reg.dy - shift_y) <= 1
    # Accepted either as seamless (flat content continues) or as an aligned gap — never rejected.
    assert reg.ncc >= 0.55 or reg.band_ncc >= 0.35


def test_unrelated_pictures_have_low_band_agreement():
    a = synthetic_picture(seed=21)
    b = synthetic_picture(seed=22)
    reg = register(a[:, :600], np.ascontiguousarray(b[:, 600:]), 60, 840)
    same = register(a[:, :600], a[:, 600:], 60, 840)
    assert reg.band_ncc < 0.35 < same.band_ncc


def test_cut_labels_evidence():
    from pdf2epub.ir import BBox, Word
    from pdf2epub.stages.spreads import cut_labels
    from tests.helpers import block, page

    lb = block("l", bbox=(0.2, 0.1, 1.0, 0.7))
    rb = block("r", bbox=(0.0, 0.1, 0.3, 0.6))
    left = page(
        0,
        [lb],
        edge_lines=[
            Word(text="Gulf of Ade", bbox=BBox(x0=0.85, y0=0.6, x1=0.99, y1=0.62)),
            Word(text="Al-Yamamah", bbox=BBox(x0=0.85, y0=0.4, x1=0.99, y1=0.42)),
        ],
    )
    right = page(
        1,
        [rb],
        edge_lines=[
            Word(text="ian Peninsula", bbox=BBox(x0=0.01, y0=0.5, x1=0.2, y1=0.52)),
            Word(text="Physical", bbox=BBox(x0=0.01, y0=0.53, x1=0.2, y1=0.55)),
        ],
    )
    assert cut_labels(left, right, lb, rb, "en") == 2  # 'Ade' fragment + lowercase 'ian Peninsula'
