"""Heuristic visual checks: brand colours, logo comparison, image alt text.

OpenCV + Pillow only (no ML model). Image comparison is inherently approximate, so any visual
finding that is not certain is marked "Needs Human Verification" rather than asserted as a violation.
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image

from checker import Finding

cv2.setRNGSeed(0)


def _load_rgb(path: str, max_side: int = 400) -> np.ndarray:
    with Image.open(path) as im:
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
        im.thumbnail((max_side, max_side))
        return np.array(im)


def _hex_to_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _to_lab(rgb) -> np.ndarray:
    arr = np.array([[rgb]], dtype=np.float32) / 255.0
    return cv2.cvtColor(arr, cv2.COLOR_RGB2LAB)[0, 0]


def dominant_colours(rgb_img: np.ndarray, k: int = 5):
    """Return [(rgb_tuple, share)] sorted by share, using k-means in Lab space."""
    lab = cv2.cvtColor(rgb_img.astype(np.float32) / 255.0, cv2.COLOR_RGB2LAB).reshape(-1, 3)
    k = min(k, max(1, len(np.unique(lab, axis=0))))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 15, 0.5)
    _, labels, centers = cv2.kmeans(lab.astype(np.float32), k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    counts = np.bincount(labels.flatten(), minlength=k) / len(labels)
    rgb_centers = cv2.cvtColor(centers.reshape(1, -1, 3).astype(np.float32), cv2.COLOR_LAB2RGB).reshape(-1, 3)
    out = []
    for c, share in zip(rgb_centers, counts):
        rgb = tuple(int(round(np.clip(v, 0, 1) * 255)) for v in c)
        out.append((rgb, float(share)))
    return sorted(out, key=lambda t: -t[1])


def colour_findings(path: str, palette: list) -> list:
    if not palette:
        return []
    rgb_img = _load_rgb(path, 160)
    brand_labs = [_to_lab(_hex_to_rgb(h)) for h in palette]
    findings = []
    for rgb, share in dominant_colours(rgb_img):
        if share < 0.08 or (max(rgb) - min(rgb)) < 20:  # ignore tiny areas and neutrals (white/grey/black)
            continue
        dist = min(float(np.linalg.norm(_to_lab(rgb) - b)) for b in brand_labs)
        if dist > 25:
            hexv = "#%02X%02X%02X" % rgb
            findings.append(Finding(
                title="Colour outside the brand palette", category="Visual Consistency",
                severity="Medium" if share >= 0.25 else "Low",
                original_text=f"Dominant colour {hexv} (about {share * 100:.0f}% of the image)",
                explanation=f"{hexv} is not close to any approved brand colour (closest colour difference ΔE≈{dist:.0f}). "
                            "Automatic colour detection can be affected by photos, gradients and compression, so "
                            "this is a possible – not certain – guideline violation.",
                guideline=f"Approved brand colours: {', '.join(palette)}",
                suggestion="Use approved palette colours for large areas, or confirm that this colour is intentional.",
                suggested_text=None, related_source="Brand guidelines – brand colours",
                verification="needs_human_verification", rule_id="visual-colour"))
    return findings[:2]


def _dhash(gray: np.ndarray) -> np.ndarray:
    small = cv2.resize(gray, (9, 8), interpolation=cv2.INTER_AREA)
    return (small[:, 1:] > small[:, :-1]).flatten()


def _gray(path: str, max_side: int = 600) -> np.ndarray:
    return cv2.cvtColor(_load_rgb(path, max_side), cv2.COLOR_RGB2GRAY)


def compare_logo(image_path: str, logo_path: str) -> dict:
    """Return similarity metrics between an uploaded image and the approved logo."""
    img, logo = _gray(image_path), _gray(logo_path)
    # 1) whole-image perceptual hash similarity (image *is* the logo)
    hash_sim = 1.0 - float(np.count_nonzero(_dhash(img) != _dhash(logo))) / 64.0
    # 2) multi-scale template matching (logo placed inside a larger image)
    tm = 0.0
    ih, iw = img.shape
    for scale in np.linspace(0.1, 1.0, 46):
        th, tw = int(logo.shape[0] * scale), int(logo.shape[1] * scale)
        if th < 12 or tw < 12 or th > ih or tw > iw:
            continue
        tpl = cv2.resize(logo, (tw, th), interpolation=cv2.INTER_AREA)
        if np.std(tpl) < 1e-3:
            continue
        res = cv2.matchTemplate(img, tpl, cv2.TM_CCOEFF_NORMED)
        tm = max(tm, float(res.max()))
    # 3) ORB feature matches (robust to rotation/scale)
    orb = cv2.ORB_create(500)
    k1, d1 = orb.detectAndCompute(img, None)
    k2, d2 = orb.detectAndCompute(logo, None)
    good = 0
    if d1 is not None and d2 is not None and len(d1) > 1 and len(d2) > 1:
        bf = cv2.BFMatcher(cv2.NORM_HAMMING)
        for pair in bf.knnMatch(d2, d1, k=2):
            if len(pair) == 2 and pair[0].distance < 0.75 * pair[1].distance:
                good += 1
    matched = hash_sim >= 0.85 or tm >= 0.75 or good >= 40
    return {"hash": hash_sim, "template": tm, "orb": good, "matched": matched}


def analyse_image(path: str, palette: list, logo_paths: list, alt_text: str, filename: str = "image") -> tuple:
    """Return (findings, info_dict)."""
    findings = []
    info = {"logo_checked": False, "logo_matched": None, "palette_checked": bool(palette)}
    if not (alt_text or "").strip():
        findings.append(Finding(
            title="Missing alternative text for uploaded image", category="Accessibility", severity="Medium",
            original_text=f"[Uploaded image: {filename}]",
            explanation="The uploaded image has no alternative text, so screen-reader users cannot understand it.",
            guideline="Accessibility: every informative image needs alt text.",
            suggestion="Edit the content and add a short alt text describing the image.", suggested_text=None,
            related_source="WCAG 2.1 – 1.1.1 Non-text Content", rule_id="a11y-image-alt"))
    try:
        findings += colour_findings(path, palette)
        if logo_paths:
            info["logo_checked"] = True
            results = [compare_logo(path, lp) for lp in logo_paths]
            best = max(results, key=lambda r: (r["matched"], r["template"], r["hash"]))
            info["logo_matched"] = best["matched"]
            info["metrics"] = best
            if not best["matched"]:
                findings.append(Finding(
                    title="Approved logo not detected in image", category="Visual Consistency", severity="Medium",
                    original_text=f"[Uploaded image: {filename}]",
                    explanation=("The automatic comparison did not find the approved logo in this image "
                                 f"(hash similarity {best['hash']:.2f}, template match {best['template']:.2f}, "
                                 f"feature matches {best['orb']}). The image may simply not contain a logo, or the logo "
                                 "may be altered, recoloured or distorted."),
                    guideline="Visual identity: use the approved logo without modification.",
                    suggestion="Place the approved logo unaltered, or confirm that no logo is required for this asset.",
                    suggested_text=None, related_source="Brand guidelines – approved logo",
                    verification="needs_human_verification", rule_id="visual-logo"))
        else:
            findings.append(Finding(
                title="No approved logo on file for comparison", category="Visual Consistency", severity="Low",
                original_text=f"[Uploaded image: {filename}]",
                explanation="Visual comparison was skipped because no approved logo has been uploaded to the brand guidelines.",
                guideline="Visual identity: upload the approved logo in Brand Guidelines.",
                suggestion="Ask an administrator to upload the approved logo.", suggested_text=None,
                related_source="", verification="needs_human_verification", rule_id="visual-no-logo"))
    except Exception as exc:  # image analysis must never crash the workflow
        findings.append(Finding(
            title="Visual analysis could not be completed", category="Visual Consistency", severity="Low",
            original_text=f"[Uploaded image: {filename}]",
            explanation=f"The image could not be analysed automatically ({type(exc).__name__}).",
            guideline="Visual identity", suggestion="Have a reviewer inspect the image manually.",
            suggested_text=None, verification="needs_human_verification", rule_id="visual-error"))
    return findings, info
