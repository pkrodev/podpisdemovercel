# api/index.py
import os
import uuid
import json
import base64
from io import BytesIO
from pathlib import Path
from datetime import datetime, timezone

import pymupdf as fitz  # PyMuPDF
import qrcode
from flask import (
    Flask,
    request,
    abort,
    url_for,
    render_template,
    send_file,
    send_from_directory,
    jsonify,
)

# =============================
# Paths (Vercel: /tmp)
# =============================
API_DIR = Path(__file__).resolve().parent
ROOT_DIR = API_DIR.parent

TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "pdf_sign_demo"
DOCS_DIR = TMP_ROOT / "docs"
RENDERS_DIR = TMP_ROOT / "renders"
META_DIR = TMP_ROOT / "meta"
HISTORY_FILE = TMP_ROOT / "history.json"
CURRENT_FILE = TMP_ROOT / "current.json"

for d in (DOCS_DIR, RENDERS_DIR, META_DIR):
    d.mkdir(parents=True, exist_ok=True)

ALLOWED_EXT = {"pdf"}


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def doc_path(doc_id: str) -> Path:
    return DOCS_DIR / f"{doc_id}.pdf"


def meta_path(doc_id: str) -> Path:
    return META_DIR / f"{doc_id}.json"


def safe_doc_id(doc_id: str) -> str:
    if not doc_id or any(c for c in doc_id if c not in "0123456789abcdef"):
        abort(400, "Invalid doc_id")
    return doc_id


def cleanup_doc_files(doc_id: str):
    """Remove PDF and rendered PNGs for doc_id."""
    try:
        p = doc_path(doc_id)
        if p.exists():
            p.unlink()
    except Exception:
        pass

    try:
        for f in RENDERS_DIR.glob(f"{doc_id}_p*.png"):
            try:
                f.unlink()
            except Exception:
                pass
    except Exception:
        pass

    # meta (nazwa pliku) – usuń (żeby nie zostawiać śmieci)
    try:
        mp = meta_path(doc_id)
        if mp.exists():
            mp.unlink()
    except Exception:
        pass


def render_pdf_pages_to_pngs(doc_id: str, zoom: float = 1.6):
    pdf = doc_path(doc_id)
    if not pdf.exists():
        abort(404, "PDF not found")

    out = []
    with fitz.open(pdf) as doc:
        for i, page in enumerate(doc):
            mat = fitz.Matrix(zoom, zoom)
            pix = page.get_pixmap(matrix=mat, alpha=False)
            png_name = f"{doc_id}_p{i+1}.png"
            pix.save(RENDERS_DIR / png_name)
            out.append({"idx": i, "name": png_name})
    return out


def _collect_pages_from_request():
    """
    Prefer: multipart/form-data: page_<index> = PNG blob
    Fallback: JSON: { pages: [{index, dataURL}] }
    Returns list[(idx:int, png_bytes:bytes)]
    """
    ctype = (request.content_type or "").lower()

    if "multipart/form-data" in ctype:
        pages = []
        for key, storage in request.files.items():
            if not key.startswith("page_"):
                continue
            try:
                idx = int(key.split("_", 1)[1])
            except Exception:
                continue
            png_bytes = storage.read()
            if png_bytes:
                pages.append((idx, png_bytes))
        return pages

    payload = request.get_json(silent=True) or {}
    out = []
    for item in payload.get("pages", []):
        try:
            idx = int(item.get("index", -1))
        except Exception:
            continue
        data_url = item.get("dataURL", "")
        if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
            continue
        try:
            png_bytes = base64.b64decode(data_url.split(",", 1)[1])
        except Exception:
            continue
        out.append((idx, png_bytes))
    return out


# =============================
# Current document pointer
# =============================
def get_current_doc_id() -> str | None:
    if not CURRENT_FILE.exists():
        return None
    try:
        data = json.loads(CURRENT_FILE.read_text(encoding="utf-8"))
        doc_id = data.get("doc_id")
        if isinstance(doc_id, str) and doc_id:
            return safe_doc_id(doc_id)
    except Exception:
        return None
    return None


def set_current_doc_id(doc_id: str, original_filename: str | None = None):
    payload = {
        "doc_id": doc_id,
        "updated_at": utc_iso(),
        "filename": original_filename or None,
    }
    try:
        CURRENT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def clear_current_if(doc_id: str):
    """If signed doc was current -> clear pointer."""
    cur = get_current_doc_id()
    if cur and cur == doc_id:
        try:
            if CURRENT_FILE.exists():
                CURRENT_FILE.unlink()
        except Exception:
            pass


# =============================
# History (JSON file)
# =============================
def load_history():
    if not HISTORY_FILE.exists():
        return []
    try:
        return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_history(items):
    try:
        HISTORY_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def add_history_entry(doc_id: str):
    filename = None
    mp = meta_path(doc_id)
    if mp.exists():
        try:
            meta = json.loads(mp.read_text(encoding="utf-8"))
            filename = meta.get("original_filename")
        except Exception:
            filename = None

    items = load_history()
    items.append(
        {
            "doc_id": doc_id,
            "filename": filename or f"{doc_id}.pdf",
            "signed_at": utc_iso(),
        }
    )
    items = sorted(items, key=lambda x: x.get("signed_at", ""), reverse=True)
    save_history(items)

    # meta już niepotrzebne
    try:
        if mp.exists():
            mp.unlink()
    except Exception:
        pass


# =============================
# Flask
# =============================
app = Flask(__name__, template_folder=str(ROOT_DIR / "templates"))
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64MB


@app.get("/")
def index():
    # na upload możemy też wyświetlić stały link (bez current doc)
    stable_sign_url = url_for("sign_current", _external=True)
    stable_qr_url = url_for("qr_current", _external=True)
    return render_template("upload.html", stable_sign_url=stable_sign_url, stable_qr_url=stable_qr_url)


@app.get("/history")
def history():
    items = load_history()
    return render_template("history.html", items=items)


@app.post("/upload")
def upload():
    if "file" not in request.files:
        abort(400, "No file field")
    f = request.files["file"]
    if not f or not f.filename:
        abort(400, "No file selected")
    if not allowed_file(f.filename):
        abort(400, "Only PDF allowed")

    # Jeśli był już "current" dokument, sprzątnij go (żeby /tmp się nie zapychał)
    old = get_current_doc_id()
    if old:
        cleanup_doc_files(old)

    doc_id = uuid.uuid4().hex[:12]
    f.save(doc_path(doc_id))

    # meta: nazwa pliku
    try:
        meta_path(doc_id).write_text(
            json.dumps(
                {"original_filename": f.filename, "uploaded_at": utc_iso()},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass

    # Ustaw jako current
    set_current_doc_id(doc_id, original_filename=f.filename)

    # Stable linki (dla tabletu)
    stable_sign_url = url_for("sign_current", _external=True)
    stable_qr_url = url_for("qr_current", _external=True)

    return render_template(
        "share.html",
        doc_id=doc_id,
        sign_url=stable_sign_url,
        qr_url=stable_qr_url,
        stable_sign_url=stable_sign_url,
    )


@app.get("/qr/<string:doc_id>")
def qr(doc_id):
    doc_id = safe_doc_id(doc_id)
    sign_url = url_for("sign", doc_id=doc_id, _external=True)
    img = qrcode.make(sign_url)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return send_file(bio, mimetype="image/png")


@app.get("/qr/current")
def qr_current():
    sign_url = url_for("sign_current", _external=True)
    img = qrcode.make(sign_url)
    bio = BytesIO()
    img.save(bio, format="PNG")
    bio.seek(0)
    return send_file(bio, mimetype="image/png")


@app.get("/render/<path:filename>")
def render_file(filename):
    return send_from_directory(str(RENDERS_DIR), filename)


@app.get("/sign/<string:doc_id>")
def sign(doc_id):
    doc_id = safe_doc_id(doc_id)
    pages = render_pdf_pages_to_pngs(doc_id, zoom=1.6)
    return render_template("sign.html", doc_id=doc_id, pages=pages)


@app.get("/sign/current")
def sign_current():
    doc_id = get_current_doc_id()
    if not doc_id:
        # brak aktualnego dokumentu -> pokaż upload
        stable_sign_url = url_for("sign_current", _external=True)
        stable_qr_url = url_for("qr_current", _external=True)
        return render_template(
            "upload.html",
            stable_sign_url=stable_sign_url,
            stable_qr_url=stable_qr_url,
            info="Brak aktywnego dokumentu. Wyślij nowy plik PDF.",
        )
    return sign(doc_id)


@app.post("/api/sign/<string:doc_id>")
def api_sign(doc_id):
    """
    Demo behavior:
    - Accept per-page overlay PNGs (page_<i>)
    - Add entry to history
    - If it was current -> clear current pointer
    - Cleanup PDF + renders
    - Return confirmation message
    """
    doc_id = safe_doc_id(doc_id)
    if not doc_path(doc_id).exists():
        return jsonify({"ok": False, "error": "PDF not found"}), 404

    pages = _collect_pages_from_request()
    if not pages:
        return jsonify({"ok": False, "error": "No signature data received"}), 400

    useful = any(len(png_bytes) > 2000 for _, png_bytes in pages)
    if not useful:
        return jsonify({"ok": False, "error": "Signature looks empty"}), 400

    add_history_entry(doc_id)
    clear_current_if(doc_id)
    cleanup_doc_files(doc_id)

    return jsonify({"ok": True, "message": "Dokument został podpisany."})
