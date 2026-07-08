"""Study browser for the dashboard's transaction history.

Walks a storage folder (received or the sent/archive folder), reads one header
per series to group instances into studies, and exposes safe delete helpers.
Everything is path-based and gated to the given root via ``safe_within``.
"""

from __future__ import annotations

import os
import shutil

from .dicomfs import is_dicom, prune_empty_dirs, safe_within


def _read_header(path: str):
    try:
        from pydicom import dcmread
        return dcmread(path, stop_before_pixels=True, force=True)
    except Exception:
        return None


def _fmt_date(raw) -> str:
    s = str(raw or "").strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _fmt_name(raw) -> str:
    """DICOM PersonName 'Family^Given^Middle^Prefix^Suffix' → 'Given Family'."""
    s = str(raw or "").strip()
    if not s:
        return ""
    parts = s.split("^")
    fam = parts[0].strip() if parts else ""
    giv = parts[1].strip() if len(parts) > 1 else ""
    if fam or giv:
        return (f"{giv} {fam}").strip()
    return s.replace("^", " ").strip()


def scan_studies(root: str, max_studies: int = 800) -> list[dict]:
    """Group every stored instance under *root* into studies (newest first)."""
    if not root or not os.path.isdir(root):
        return []

    studies: dict[str, dict] = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        files = [f for f in filenames if not f.startswith(".")]
        if not files:
            continue
        hdr = None
        for f in sorted(files):
            p = os.path.join(dirpath, f)
            if is_dicom(p):
                hdr = _read_header(p)
                if hdr is not None:
                    break
        if hdr is None:
            continue
        count = sum(1 for f in files if is_dicom(os.path.join(dirpath, f)))
        if count == 0:
            continue

        suid = str(getattr(hdr, "StudyInstanceUID", "") or "")
        key = suid or os.path.dirname(dirpath) or dirpath
        st = studies.get(key)
        if st is None:
            st = {
                "patient": _fmt_name(getattr(hdr, "PatientName", "")),
                "patient_id": str(getattr(hdr, "PatientID", "") or ""),
                "study_date": _fmt_date(getattr(hdr, "StudyDate", "")),
                "study_desc": str(getattr(hdr, "StudyDescription", "") or ""),
                "study_uid": suid,
                "series": [],
                "instances": 0,
                "_dirs": [],
                "_mods": set(),
                "mtime": 0.0,
            }
            studies[key] = st

        modality = str(getattr(hdr, "Modality", "") or "")
        if modality:
            st["_mods"].add(modality)
        st["series"].append({
            "desc": str(getattr(hdr, "SeriesDescription", "") or ""),
            "modality": modality,
            "number": str(getattr(hdr, "SeriesNumber", "") or ""),
            "count": count,
        })
        st["instances"] += count
        st["_dirs"].append(dirpath)
        try:
            st["mtime"] = max(st["mtime"], os.path.getmtime(dirpath))
        except OSError:
            pass

    out = []
    for st in studies.values():
        dirs = st.pop("_dirs")
        try:
            st["path"] = os.path.commonpath(dirs) if len(dirs) > 1 else dirs[0]
        except ValueError:
            st["path"] = dirs[0]
        st["modality"] = ",".join(sorted(st.pop("_mods"))) or "?"
        st["series"].sort(key=lambda s: (s.get("number") or "", s.get("desc") or ""))
        out.append(st)
    out.sort(key=lambda s: s.get("mtime", 0), reverse=True)
    return out[:max_studies]


def study_files(root: str, path: str) -> list[str]:
    """All DICOM files under *path* (a study dir or single file), gated to root."""
    if not safe_within(root, path):
        raise ValueError("path is outside the storage folder")
    files: list[str] = []
    if os.path.isdir(path):
        for dp, _dn, fns in os.walk(path):
            for f in fns:
                if f.startswith("."):
                    continue
                fp = os.path.join(dp, f)
                if is_dicom(fp):
                    files.append(fp)
    elif os.path.isfile(path) and is_dicom(path):
        files.append(path)
    return files


def delete_study(root: str, path: str) -> None:
    """Delete one study's folder (or file) and prune the empty parents it leaves."""
    real_root = os.path.realpath(root)
    real_path = os.path.realpath(path)
    if real_path == real_root or not safe_within(root, path):
        raise ValueError("refusing to delete outside the storage folder")
    if os.path.isdir(real_path):
        shutil.rmtree(real_path)
    elif os.path.isfile(real_path):
        os.remove(real_path)
    else:
        raise ValueError("study no longer exists")
    prune_empty_dirs(os.path.dirname(real_path), real_root)


def delete_all(root: str) -> int:
    """Remove every study under *root* (keeps the root and any hidden sidecars)."""
    if not root or not os.path.isdir(root):
        return 0
    removed = 0
    for name in os.listdir(root):
        if name.startswith("."):        # keep .carinopacs_state.json etc.
            continue
        p = os.path.join(root, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            removed += 1
        except OSError:
            pass
    return removed
