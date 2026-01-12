import os
import uuid
import base64
import hmac
import hashlib
from io import BytesIO
from pathlib import Path

import fitz  # PyMuPDF
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

API_DIR = Path(__file__).resolve().parent
ROOT_DIR = API_DIR.parent

TMP_ROOT = Path(os.environ.get("TMPDIR", "/tmp")) / "pdf_sign_app"
DOCS_DIR = TMP_ROOT / "docs"
RENDERS_DIR = TMP_ROOT / "renders"
SIGNED_DIR = TMP_ROOT / "signed"

for d in (DOCS_DIR, RENDERS_DIR, SIGNED_DIR):
    d.mkdir(parents=True, exist_ok=True)

SECRET_KEY = os.environ.get("PDF_HMAC_SECRET", "CHANGE_ME_LONG_RANDOM_SECRET")
ALLOWED_EXT = {"pdf"}


def allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXT


def doc_path(doc_id: str) -> Path:
    return DOCS_DIR / f"{doc_id}.pdf"


def safe_doc_id(doc_id: str) -> str:
    if not doc_id or any(c for c in doc_id if c not in "0123456789abcdef"):
        abort(400, "Invalid doc_id")
    return doc_id


def compute_hmac(data: bytes) -> str:
    return hmac.new(SECRET_KEY.encode("utf-8"), data, hashlib.sha256).hexdigest()


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
            png_path = RENDERS_DIR / png_name
            pix.save(png_path)
            out.append({"idx": i, "name": png_name})
    return out


app = Flask(
    __name__,
    template_folder=str(ROOT_DIR / "templates"),
)

app.config["MAX_CONTENT_LENGTH"] = 64 * 1024 * 1024  # 64MB


@app.get("/")
def index():
    return render_template("upload.html")


@app.post("/upload")
def upload():
    if "file" not in request.files:
        abort(400, "No file field")
    f = request.files["file"]
    if not f or not f.filename:
        abort(400, "No file selected")
    if not allowed_file(f.filename):
        abort(400, "Only PDF allowed")

    doc_id = uuid.uuid4().hex[:12]
    pdf_path = doc_path(doc_id)
    f.save(pdf_path)

    sign_url = url_for("sign", doc_id=doc_id, _external=True)
    qr_url = url_for("qr", doc_id=doc_id, _external=True)

    return render_template("share.html", doc_id=doc_id, sign_url=sign_url, qr_url=qr_url)


@app.get("/qr/<string:doc_id>")
def qr(doc_id):
    doc_id = safe_doc_id(doc_id)
    sign_url = url_for("sign", doc_id=doc_id, _external=True)

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


def _collect_pages_from_request():
    """
    Obsługuje:
    1) multipart/form-data: page_<index> = PNG blob
    2) JSON: { pages: [{index, dataURL}] }
    Zwraca listę (idx:int, png_bytes:bytes)
    """
    ctype = (request.content_type or "").lower()

    # 1) Multipart (preferowane na mobile)
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

    # 2) JSON fallback
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


@app.post("/api/sign/<string:doc_id>")
def api_sign(doc_id):
    doc_id = safe_doc_id(doc_id)
    pdf_path = doc_path(doc_id)
    if not pdf_path.exists():
        return jsonify({"ok": False, "error": "PDF not found"}), 404

    pages = _collect_pages_from_request()
    if not pages:
        return jsonify({"ok": False, "error": "No signature data received"}), 400

    out_bytes = BytesIO()
    with fitz.open(pdf_path) as doc:
        for idx, png_bytes in pages:
            if idx < 0 or idx >= len(doc):
                continue
            page = doc.load_page(idx)
            rect = page.rect
            page.insert_image(rect, stream=png_bytes, keep_proportion=False, overlay=True)
        doc.save(out_bytes)

    signed_pdf = out_bytes.getvalue()
    digest = compute_hmac(signed_pdf)[:16]
    filename = f"{doc_id}__{digest}_signed.pdf"
    signed_path = SIGNED_DIR / filename
    signed_path.write_bytes(signed_pdf)

    return jsonify(
        {
            "ok": True,
            "filename": filename,
            "success_url": url_for("success", filename=filename),
            "download_url": url_for("download", filename=filename),
        }
    )


@app.get("/success/<path:filename>")
def success(filename):
    return render_template(
        "success.html",
        filename=filename,
        download_url=url_for("download", filename=filename),
    )


@app.get("/download/<path:filename>")
def download(filename):
    fpath = SIGNED_DIR / filename
    if not fpath.exists():
        abort(404, "Signed PDF not found")
    return send_from_directory(str(SIGNED_DIR), filename, as_attachment=True)
