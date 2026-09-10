import os
from fastapi import FastAPI, Depends, UploadFile, File, Form, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from slugify import slugify
from PIL import Image

from db import SessionLocal
from models import Base, engine, Post, Album, Photo
from auth import admin_required

origins = os.getenv("CORS_ORIGINS", "").split(",") if os.getenv("CORS_ORIGINS") else ["*"]

app = FastAPI(title="Personal API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in origins if o.strip()],
    # kimaki tunnel dev URLs get a random subdomain per session
    allow_origin_regex=r"https://[a-z0-9-]+\.kimaki\.dev",
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ----------------- Blog -----------------
@app.get("/api/posts")
def list_posts(db: Session = Depends(get_db)):
    posts = db.query(Post).order_by(Post.created_at.desc()).all()
    return [{"id": p.id, "title": p.title, "slug": p.slug, "created_at": p.created_at} for p in posts]

@app.get("/api/posts/{slug}")
def get_post(slug: str, db: Session = Depends(get_db)):
    p = db.query(Post).filter_by(slug=slug).first()
    if not p:
        raise HTTPException(404, "Not found")
    return {"id": p.id, "title": p.title, "slug": p.slug, "body_md": p.body_md, "updated_at": p.updated_at}

@app.post("/api/posts", dependencies=[Depends(admin_required)])
def create_post(title: str = Form(...), body_md: str = Form(...), db: Session = Depends(get_db)):
    s = slugify(title)
    p = Post(title=title, slug=s, body_md=body_md)
    db.add(p); db.commit(); db.refresh(p)
    return {"ok": True, "slug": p.slug}

@app.put("/api/posts/{slug}", dependencies=[Depends(admin_required)])
def update_post(slug: str, title: str = Form(None), body_md: str = Form(None), db: Session = Depends(get_db)):
    p = db.query(Post).filter_by(slug=slug).first()
    if not p: raise HTTPException(404, "Not found")
    if title: p.title = title
    if body_md: p.body_md = body_md
    db.commit()
    return {"ok": True}

@app.delete("/api/posts/{slug}", dependencies=[Depends(admin_required)])
def delete_post(slug: str, db: Session = Depends(get_db)):
    p = db.query(Post).filter_by(slug=slug).first()
    if not p: raise HTTPException(404, "Not found")
    db.delete(p); db.commit()
    return {"ok": True}

# ----------------- Gallery -----------------
UPLOAD_DIR = "/app/uploads/photos"
THUMB_DIR = "/app/uploads/thumbs"

def save_thumbnail(path_in: str, path_out: str, size=(640, 640)):
    img = Image.open(path_in)
    img.thumbnail(size)
    img.save(path_out)

@app.post("/api/albums", dependencies=[Depends(admin_required)])
def create_album(title: str = Form(...), description: str = Form(""), db: Session = Depends(get_db)):
    a = Album(title=title, description=description)
    db.add(a); db.commit(); db.refresh(a)
    return {"id": a.id, "title": a.title}

@app.get("/api/albums")
def list_albums(db: Session = Depends(get_db)):
    return db.query(Album).all()

@app.post("/api/albums/{album_id}/photos", dependencies=[Depends(admin_required)])
def upload_photo(album_id: int, file: UploadFile = File(...), caption: str = Form(""), db: Session = Depends(get_db)):
    os.makedirs(UPLOAD_DIR, exist_ok=True); os.makedirs(THUMB_DIR, exist_ok=True)
    fname = file.filename
    path = os.path.join(UPLOAD_DIR, fname)
    with open(path, "wb") as f:
        f.write(file.file.read())
    save_thumbnail(path, os.path.join(THUMB_DIR, fname))

    ph = Photo(album_id=album_id, filename=fname, caption=caption)
    db.add(ph); db.commit(); db.refresh(ph)
    return {"id": ph.id, "filename": fname}

@app.get("/api/albums/{album_id}/photos")
def list_photos(album_id: int, db: Session = Depends(get_db)):
    photos = db.query(Photo).filter_by(album_id=album_id).all()
    base = os.getenv("PUBLIC_BASE", "https://api.yourdomain.com")
    # Note: Caddy serves /media/*, so use that path
    return [
        {"id": p.id, "caption": p.caption,
         "url": f"{base}/media/photos/{p.filename}",
         "thumb_url": f"{base}/media/thumbs/{p.filename}"}
        for p in photos
    ]

# ----------------- Script Runner -----------------
from rq import Queue
import redis
from worker import run_script as run_script_func

redis_url = os.getenv("REDIS_URL", "redis://redis:6379/0")
q = Queue(connection=redis.from_url(redis_url))

@app.post("/api/run", dependencies=[Depends(admin_required)])
def run_script(payload: dict = Body(...)):
    """
    payload: { "script": "my_script.py", "args": ["foo", "bar"], "mode": "sync"|"queue" }
    """
    script = payload.get("script"); args = payload.get("args", []); mode = payload.get("mode", "sync")
    if not script:
        raise HTTPException(400, "Missing 'script'")
    if mode == "queue":
        job = q.enqueue(run_script_func, script, *args)
        return {"queued": True, "job_id": job.id}
    else:
        return run_script_func(script, *args)
