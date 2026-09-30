#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
لقطة (Laqta) — سيرفر الشبكة الاجتماعية
========================================
سيرفر FastAPI + SQLite لتطبيق "لقطة" (شبيه إنستغرام):
- حسابات: تسجيل / دخول (username + password — وضع تجريبي، بدون SMS)
- بروفايل: صورة، بايو، عدّادات متابِعين
- منشورات: صورة أو فيديو + وصف، رفع ملفات
- خلاصة زمنية من المتابَعين، لايك، تعليقات، متابعة، بحث

التشغيل:
    ./run.sh
    أو: uvicorn server:app --host 0.0.0.0 --port 8000

متغيرات البيئة:
    DATA_DIR=./data     مجلد قاعدة البيانات والملفات المرفوعة
    MAX_UPLOAD_MB=50    حد حجم الملف المرفوع

النشر على Render: راجع render.yaml و Dockerfile و README.md
"""

import hashlib
import os
import re
import secrets
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel

# ---------------------------------------------------------------- الإعدادات
SERVER_VERSION = "3.0"
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DB_PATH = DATA_DIR / "laqta.db"
UPLOAD_DIR = DATA_DIR / "uploads"
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "50"))

DATA_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

USERNAME_RE = re.compile(r"^[A-Za-z0-9_.]{3,20}$")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".webm", ".3gp", ".mkv"}

# ---------------------------------------------------------------- قاعدة البيانات
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            bio TEXT NOT NULL DEFAULT '',
            avatar TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tokens (
            token TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            media TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'image',
            caption TEXT NOT NULL DEFAULT '',
            views INTEGER NOT NULL DEFAULT 0,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS likes (
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (user_id, post_id)
        );
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS follows (
            follower_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            followed_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (follower_id, followed_id)
        );
        CREATE TABLE IF NOT EXISTS post_views (
            post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (post_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_posts_user ON posts(user_id);
        CREATE INDEX IF NOT EXISTS idx_posts_created ON posts(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_comments_post ON comments(post_id);
        CREATE INDEX IF NOT EXISTS idx_likes_post ON likes(post_id);
        CREATE TABLE IF NOT EXISTS stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            media TEXT NOT NULL,
            media_type TEXT NOT NULL DEFAULT 'image',
            caption TEXT NOT NULL DEFAULT '',
            created_at INTEGER NOT NULL,
            expires_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS story_views (
            story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (story_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS story_reactions (
            story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            emoji TEXT NOT NULL,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (story_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS conversations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS conversation_participants (
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at INTEGER NOT NULL,
            PRIMARY KEY (conversation_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
            sender_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            text TEXT NOT NULL DEFAULT '',
            media TEXT NOT NULL DEFAULT '',
            media_type TEXT NOT NULL DEFAULT '',
            reply_story_id INTEGER NULL REFERENCES stories(id) ON DELETE SET NULL,
            created_at INTEGER NOT NULL,
            read_at INTEGER NULL
        );
        CREATE INDEX IF NOT EXISTS idx_stories_user ON stories(user_id);
        CREATE INDEX IF NOT EXISTS idx_stories_expires ON stories(expires_at);
        CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(conversation_id, id);
        CREATE INDEX IF NOT EXISTS idx_participants_user ON conversation_participants(user_id);
        """)

def migrate_db():
    """ترحيل قواعد البيانات القديمة: إضافة عمود views إلى posts إن كان ناقصاً."""
    with db() as c:
        cols = [r["name"] for r in c.execute("PRAGMA table_info(posts)")]
        if "views" not in cols:
            c.execute("ALTER TABLE posts ADD COLUMN views INTEGER NOT NULL DEFAULT 0")

init_db()
migrate_db()

def cleanup_expired_stories():
    """حذف الستوري المنتهي (24 ساعة) مع ملفات الميديا — تُستدعى عند الإقلاع وعند طلب الخلاصة."""
    now = int(time.time())
    with db() as c:
        rows = c.execute("SELECT id, media FROM stories WHERE expires_at <= ?", (now,)).fetchall()
        for r in rows:
            c.execute("DELETE FROM stories WHERE id = ?", (r["id"],))
            try: (UPLOAD_DIR / r["media"]).unlink()
            except OSError: pass

cleanup_expired_stories()

# ---------------------------------------------------------------- كلمات المرور
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return f"{salt}${h}"

def verify_password(password: str, stored: str) -> bool:
    try:
        salt, h = stored.split("$", 1)
    except ValueError:
        return False
    calc = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000).hex()
    return secrets.compare_digest(calc, h)

# ---------------------------------------------------------------- المصادقة
def current_user(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "يلزم تسجيل الدخول")
    token = authorization[7:]
    with db() as c:
        row = c.execute(
            "SELECT u.* FROM users u JOIN tokens t ON t.user_id = u.id WHERE t.token = ?",
            (token,)).fetchone()
    if not row:
        raise HTTPException(401, "جلسة غير صالحة")
    return row

# ---------------------------------------------------------------- التسلسل
def user_public(row, me_id: int = 0) -> dict:
    with db() as c:
        # تُستبعد المتابعة الذاتية (يضيفها النظام ليرى المستخدم منشوراته في خلاصته)
        followers = c.execute("SELECT COUNT(*) n FROM follows WHERE followed_id = ? AND follower_id != followed_id",
                              (row["id"],)).fetchone()["n"]
        following = c.execute("SELECT COUNT(*) n FROM follows WHERE follower_id = ? AND follower_id != followed_id",
                              (row["id"],)).fetchone()["n"]
        posts = c.execute("SELECT COUNT(*) n FROM posts WHERE user_id = ?",
                          (row["id"],)).fetchone()["n"]
        is_following = False
        if me_id and me_id != row["id"]:
            is_following = c.execute(
                "SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?",
                (me_id, row["id"])).fetchone() is not None
    return {
        "id": row["id"],
        "username": row["username"],
        "name": row["name"],
        "bio": row["bio"],
        "avatar_url": f"/media/{row['avatar']}" if row["avatar"] else None,
        "followers_count": followers,
        "following_count": following,
        "posts_count": posts,
        "is_following": is_following,
        "is_self": me_id == row["id"],
        "created_at": row["created_at"],
    }

def post_public(row, me_id: int = 0) -> dict:
    with db() as c:
        author = c.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        like_count = c.execute("SELECT COUNT(*) n FROM likes WHERE post_id = ?",
                               (row["id"],)).fetchone()["n"]
        comment_count = c.execute("SELECT COUNT(*) n FROM comments WHERE post_id = ?",
                                  (row["id"],)).fetchone()["n"]
        liked = (c.execute("SELECT 1 FROM likes WHERE post_id = ? AND user_id = ?",
                           (row["id"], me_id)).fetchone() is not None) if me_id else False
    return {
        "id": row["id"],
        "author": {
            "id": author["id"],
            "username": author["username"],
            "name": author["name"],
            "avatar_url": f"/media/{author['avatar']}" if author["avatar"] else None,
        },
        "media_url": f"/media/{row['media']}",
        "media_type": row["media_type"],
        "caption": row["caption"],
        "like_count": like_count,
        "comment_count": comment_count,
        "view_count": row["views"] if "views" in row.keys() else 0,
        "liked_by_me": liked,
        "is_mine": me_id == row["user_id"],
        "created_at": row["created_at"],
    }

def save_upload(upload: UploadFile, allowed: set) -> str:
    ext = Path(upload.filename or "").suffix.lower()
    if ext not in allowed:
        raise HTTPException(400, f"نوع الملف غير مدعوم: {ext or '؟'}")
    data = upload.file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, f"حجم الملف يتجاوز {MAX_UPLOAD_MB}MB")
    if len(data) == 0:
        raise HTTPException(400, "الملف فارغ")
    name = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / name).write_bytes(data)
    return name

# ---------------------------------------------------------------- التطبيق
app = FastAPI(title="Laqta API", version=SERVER_VERSION)

class RegisterIn(BaseModel):
    username: str
    password: str
    name: str = ""

class LoginIn(BaseModel):
    username: str
    password: str

class ProfilePatch(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None

class CommentIn(BaseModel):
    text: str

# ---------------------------------------------------------- الحسابات
@app.post("/api/register", status_code=201)
def register(body: RegisterIn):
    username = body.username.strip().lower()
    if not USERNAME_RE.match(username):
        raise HTTPException(400, "اسم المستخدم: 3-20 حرف (أحرف/أرقام/._)")
    if len(body.password) < 4:
        raise HTTPException(400, "كلمة المرور قصيرة (4 أحرف على الأقل)")
    now = int(time.time())
    with db() as c:
        if c.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone():
            raise HTTPException(400, "اسم المستخدم مسجّل مسبقاً")
        cur = c.execute(
            "INSERT INTO users (username, password_hash, name, created_at) VALUES (?,?,?,?)",
            (username, hash_password(body.password), body.name.strip()[:50], now))
        uid = cur.lastrowid
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO tokens (token, user_id, created_at) VALUES (?,?,?)",
                  (token, uid, now))
        # تابع نفسك تلقائياً لترى منشوراتك في الخلاصة
        c.execute("INSERT OR IGNORE INTO follows (follower_id, followed_id, created_at) VALUES (?,?,?)",
                  (uid, uid, now))
        row = c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()
    return {"token": token, "user": user_public(row, uid)}

@app.post("/api/login")
def login(body: LoginIn):
    username = body.username.strip().lower()
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if not row or not verify_password(body.password, row["password_hash"]):
            raise HTTPException(401, "بيانات الدخول غير صحيحة")
        token = secrets.token_urlsafe(32)
        c.execute("INSERT INTO tokens (token, user_id, created_at) VALUES (?,?,?)",
                  (token, row["id"], int(time.time())))
    return {"token": token, "user": user_public(row, row["id"])}

@app.post("/api/logout")
def logout(me=Depends(current_user), authorization: Optional[str] = Header(None)):
    with db() as c:
        c.execute("DELETE FROM tokens WHERE token = ?", (authorization[7:],))
    return {"ok": True}

@app.get("/api/me")
def me(me=Depends(current_user)):
    return user_public(me, me["id"])

@app.patch("/api/me")
def patch_me(body: ProfilePatch, me=Depends(current_user)):
    with db() as c:
        if body.name is not None:
            c.execute("UPDATE users SET name = ? WHERE id = ?", (body.name.strip()[:50], me["id"]))
        if body.bio is not None:
            c.execute("UPDATE users SET bio = ? WHERE id = ?", (body.bio.strip()[:150], me["id"]))
        row = c.execute("SELECT * FROM users WHERE id = ?", (me["id"],)).fetchone()
    return user_public(row, me["id"])

@app.post("/api/me/avatar")
def upload_avatar(file: UploadFile = File(...), me=Depends(current_user)):
    name = save_upload(file, IMAGE_EXTS)
    with db() as c:
        old = c.execute("SELECT avatar FROM users WHERE id = ?", (me["id"],)).fetchone()["avatar"]
        c.execute("UPDATE users SET avatar = ? WHERE id = ?", (name, me["id"]))
        row = c.execute("SELECT * FROM users WHERE id = ?", (me["id"],)).fetchone()
    if old:
        try: (UPLOAD_DIR / old).unlink()
        except OSError: pass
    return user_public(row, me["id"])

# ---------------------------------------------------------- المستخدمون
@app.get("/api/users/search")
def search_users(q: str = Query("", min_length=1), limit: int = Query(20, le=50),
                 me=Depends(current_user)):
    like = f"%{q.strip().lower()}%"
    with db() as c:
        rows = c.execute(
            "SELECT * FROM users WHERE LOWER(username) LIKE ? OR LOWER(name) LIKE ? "
            "ORDER BY username LIMIT ?", (like, like, limit)).fetchall()
    return [user_public(r, me["id"]) for r in rows]

@app.get("/api/users/{username}")
def get_user(username: str, me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?",
                        (username.strip().lower(),)).fetchone()
    if not row:
        raise HTTPException(404, "المستخدم غير موجود")
    return user_public(row, me["id"])

@app.get("/api/users/{username}/posts")
def user_posts(username: str, limit: int = Query(12, le=50), offset: int = 0,
               me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?",
                        (username.strip().lower(),)).fetchone()
        if not row:
            raise HTTPException(404, "المستخدم غير موجود")
        posts = c.execute(
            "SELECT * FROM posts WHERE user_id = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (row["id"], limit, offset)).fetchall()
    return [post_public(p, me["id"]) for p in posts]

@app.post("/api/users/{username}/follow")
def toggle_follow(username: str, me=Depends(current_user)):
    target_name = username.strip().lower()
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (target_name,)).fetchone()
        if not row:
            raise HTTPException(404, "المستخدم غير موجود")
        if row["id"] == me["id"]:
            raise HTTPException(400, "لا يمكنك متابعة نفسك")
        exists = c.execute("SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?",
                           (me["id"], row["id"])).fetchone()
        if exists:
            c.execute("DELETE FROM follows WHERE follower_id = ? AND followed_id = ?",
                      (me["id"], row["id"]))
            following = False
        else:
            c.execute("INSERT INTO follows (follower_id, followed_id, created_at) VALUES (?,?,?)",
                      (me["id"], row["id"], int(time.time())))
            following = True
    return {"following": following}

@app.get("/api/users/{username}/followers")
def get_followers(username: str, limit: int = Query(50, le=100), me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT id FROM users WHERE username = ?",
                        (username.strip().lower(),)).fetchone()
        if not row:
            raise HTTPException(404, "المستخدم غير موجود")
        rows = c.execute(
            "SELECT u.* FROM users u JOIN follows f ON f.follower_id = u.id "
            "WHERE f.followed_id = ? AND u.id != ? ORDER BY f.created_at DESC LIMIT ?",
            (row["id"], row["id"], limit)).fetchall()
    return [user_public(r, me["id"]) for r in rows]

@app.get("/api/users/{username}/following")
def get_following(username: str, limit: int = Query(50, le=100), me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT id FROM users WHERE username = ?",
                        (username.strip().lower(),)).fetchone()
        if not row:
            raise HTTPException(404, "المستخدم غير موجود")
        rows = c.execute(
            "SELECT u.* FROM users u JOIN follows f ON f.followed_id = u.id "
            "WHERE f.follower_id = ? AND u.id != ? ORDER BY f.created_at DESC LIMIT ?",
            (row["id"], row["id"], limit)).fetchall()
    return [user_public(r, me["id"]) for r in rows]

# ---------------------------------------------------------- المنشورات
@app.post("/api/posts", status_code=201)
def create_post(caption: str = Form(""), file: UploadFile = File(...), me=Depends(current_user)):
    name = save_upload(file, IMAGE_EXTS | VIDEO_EXTS)
    ext = Path(name).suffix.lower()
    media_type = "video" if ext in VIDEO_EXTS else "image"
    now = int(time.time())
    with db() as c:
        cur = c.execute(
            "INSERT INTO posts (user_id, media, media_type, caption, created_at) VALUES (?,?,?,?,?)",
            (me["id"], name, media_type, caption.strip()[:2200], now))
        row = c.execute("SELECT * FROM posts WHERE id = ?", (cur.lastrowid,)).fetchone()
    return post_public(row, me["id"])

@app.get("/api/feed")
def feed(limit: int = Query(20, le=50), offset: int = 0, me=Depends(current_user)):
    with db() as c:
        rows = c.execute(
            """SELECT p.* FROM posts p
               JOIN follows f ON f.followed_id = p.user_id
               WHERE f.follower_id = ?
               ORDER BY p.created_at DESC, p.id DESC LIMIT ? OFFSET ?""",
            (me["id"], limit, offset)).fetchall()
    return [post_public(p, me["id"]) for p in rows]

@app.get("/api/posts/{post_id}")
def get_post(post_id: int, me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            raise HTTPException(404, "المنشور غير موجود")
        comments = c.execute(
            """SELECT cm.*, u.username, u.name,
                      CASE WHEN u.avatar='' THEN NULL ELSE '/media/'||u.avatar END AS avatar_url
               FROM comments cm JOIN users u ON u.id = cm.user_id
               WHERE cm.post_id = ? ORDER BY cm.created_at ASC LIMIT 100""",
            (post_id,)).fetchall()
    data = post_public(row, me["id"])
    data["comments"] = [dict(id=cm["id"], text=cm["text"], created_at=cm["created_at"],
                             user={"id": cm["user_id"], "username": cm["username"],
                                   "name": cm["name"], "avatar_url": cm["avatar_url"]})
                        for cm in comments]
    return data

@app.delete("/api/posts/{post_id}")
def delete_post(post_id: int, me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM posts WHERE id = ?", (post_id,)).fetchone()
        if not row:
            raise HTTPException(404, "المنشور غير موجود")
        if row["user_id"] != me["id"]:
            raise HTTPException(403, "ليس منشورك")
        c.execute("DELETE FROM posts WHERE id = ?", (post_id,))
    try: (UPLOAD_DIR / row["media"]).unlink()
    except OSError: pass
    return {"ok": True}

@app.post("/api/posts/{post_id}/view")
def record_view(post_id: int, me=Depends(current_user)):
    """تسجيل مشاهدة: تُحتسب مرة واحدة لكل مستخدم لكل منشور."""
    now = int(time.time())
    with db() as c:
        if not c.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "المنشور غير موجود")
        cur = c.execute(
            "INSERT OR IGNORE INTO post_views (post_id, user_id, created_at) VALUES (?,?,?)",
            (post_id, me["id"], now))
        if cur.rowcount == 1:
            c.execute("UPDATE posts SET views = views + 1 WHERE id = ?", (post_id,))
        n = c.execute("SELECT views FROM posts WHERE id = ?", (post_id,)).fetchone()["views"]
    return {"ok": True, "view_count": n}

@app.post("/api/posts/{post_id}/like")
def toggle_like(post_id: int, me=Depends(current_user)):
    with db() as c:
        if not c.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "المنشور غير موجود")
        exists = c.execute("SELECT 1 FROM likes WHERE post_id = ? AND user_id = ?",
                           (post_id, me["id"])).fetchone()
        if exists:
            c.execute("DELETE FROM likes WHERE post_id = ? AND user_id = ?", (post_id, me["id"]))
            liked = False
        else:
            c.execute("INSERT INTO likes (user_id, post_id, created_at) VALUES (?,?,?)",
                      (me["id"], post_id, int(time.time())))
            liked = True
        count = c.execute("SELECT COUNT(*) n FROM likes WHERE post_id = ?",
                          (post_id,)).fetchone()["n"]
    return {"liked": liked, "like_count": count}

@app.get("/api/posts/{post_id}/comments")
def list_comments(post_id: int, limit: int = Query(50, le=100), offset: int = 0,
                  me=Depends(current_user)):
    with db() as c:
        if not c.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "المنشور غير موجود")
        rows = c.execute(
            """SELECT cm.*, u.username, u.name,
                      CASE WHEN u.avatar='' THEN NULL ELSE '/media/'||u.avatar END AS avatar_url
               FROM comments cm JOIN users u ON u.id = cm.user_id
               WHERE cm.post_id = ? ORDER BY cm.created_at ASC LIMIT ? OFFSET ?""",
            (post_id, limit, offset)).fetchall()
    return [dict(id=r["id"], text=r["text"], created_at=r["created_at"],
                 user={"id": r["user_id"], "username": r["username"],
                       "name": r["name"], "avatar_url": r["avatar_url"]})
            for r in rows]

@app.post("/api/posts/{post_id}/comments", status_code=201)
def add_comment(post_id: int, body: CommentIn, me=Depends(current_user)):
    text = body.text.strip()
    if not text:
        raise HTTPException(400, "التعليق فارغ")
    with db() as c:
        if not c.execute("SELECT 1 FROM posts WHERE id = ?", (post_id,)).fetchone():
            raise HTTPException(404, "المنشور غير موجود")
        cur = c.execute(
            "INSERT INTO comments (post_id, user_id, text, created_at) VALUES (?,?,?,?)",
            (post_id, me["id"], text[:500], int(time.time())))
        row = c.execute(
            """SELECT cm.*, u.username, u.name,
                      CASE WHEN u.avatar='' THEN NULL ELSE '/media/'||u.avatar END AS avatar_url
               FROM comments cm JOIN users u ON u.id = cm.user_id WHERE cm.id = ?""",
            (cur.lastrowid,)).fetchone()
    return dict(id=row["id"], text=row["text"], created_at=row["created_at"],
                user={"id": row["user_id"], "username": row["username"],
                      "name": row["name"], "avatar_url": row["avatar_url"]})

@app.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, me=Depends(current_user)):
    with db() as c:
        row = c.execute(
            "SELECT cm.*, p.user_id AS owner_id FROM comments cm "
            "JOIN posts p ON p.id = cm.post_id WHERE cm.id = ?", (comment_id,)).fetchone()
        if not row:
            raise HTTPException(404, "التعليق غير موجود")
        if row["user_id"] != me["id"] and row["owner_id"] != me["id"]:
            raise HTTPException(403, "لا يمكنك حذف هذا التعليق")
        c.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
    return {"ok": True}

# ---------------------------------------------------------- النشاط
@app.get("/api/activity")
def activity(limit: int = Query(30, le=50), me=Depends(current_user)):
    items = []
    with db() as c:
        for r in c.execute(
            """SELECT l.created_at, u.id AS uid, u.username, u.name,
                      CASE WHEN u.avatar='' THEN NULL ELSE '/media/'||u.avatar END AS avatar_url,
                      l.post_id
               FROM likes l JOIN users u ON u.id = l.user_id
               JOIN posts p ON p.id = l.post_id
               WHERE p.user_id = ? AND l.user_id != ?
               ORDER BY l.created_at DESC LIMIT ?""",
            (me["id"], me["id"], limit)).fetchall():
            items.append({"type": "like", "created_at": r["created_at"], "post_id": r["post_id"],
                          "actor": {"id": r["uid"], "username": r["username"],
                                    "name": r["name"], "avatar_url": r["avatar_url"]}})
        for r in c.execute(
            """SELECT cm.created_at, cm.text, u.id AS uid, u.username, u.name,
                      CASE WHEN u.avatar='' THEN NULL ELSE '/media/'||u.avatar END AS avatar_url,
                      cm.post_id
               FROM comments cm JOIN users u ON u.id = cm.user_id
               JOIN posts p ON p.id = cm.post_id
               WHERE p.user_id = ? AND cm.user_id != ?
               ORDER BY cm.created_at DESC LIMIT ?""",
            (me["id"], me["id"], limit)).fetchall():
            items.append({"type": "comment", "created_at": r["created_at"], "post_id": r["post_id"],
                          "text": r["text"][:120],
                          "actor": {"id": r["uid"], "username": r["username"],
                                    "name": r["name"], "avatar_url": r["avatar_url"]}})
        for r in c.execute(
            """SELECT f.created_at, u.id AS uid, u.username, u.name,
                      CASE WHEN u.avatar='' THEN NULL ELSE '/media/'||u.avatar END AS avatar_url
               FROM follows f JOIN users u ON u.id = f.follower_id
               WHERE f.followed_id = ? AND f.follower_id != ?
               ORDER BY f.created_at DESC LIMIT ?""",
            (me["id"], me["id"], limit)).fetchall():
            items.append({"type": "follow", "created_at": r["created_at"], "post_id": None,
                          "actor": {"id": r["uid"], "username": r["username"],
                                    "name": r["name"], "avatar_url": r["avatar_url"]}})
    items.sort(key=lambda x: x["created_at"], reverse=True)
    return items[:limit]

# ---------------------------------------------------------- الستوري
STORY_EMOJIS = {"❤️", "😂", "😮", "😢", "👏", "🔥"}
STORY_TTL = 86400  # 24 ساعة

def story_public(row, me_id: int = 0) -> dict:
    with db() as c:
        author = c.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        view_count = c.execute("SELECT COUNT(*) n FROM story_views WHERE story_id = ?",
                               (row["id"],)).fetchone()["n"]
        seen = False
        my_reaction = None
        if me_id:
            seen = c.execute("SELECT 1 FROM story_views WHERE story_id = ? AND user_id = ?",
                             (row["id"], me_id)).fetchone() is not None
            r = c.execute("SELECT emoji FROM story_reactions WHERE story_id = ? AND user_id = ?",
                          (row["id"], me_id)).fetchone()
            my_reaction = r["emoji"] if r else None
    return {
        "id": row["id"],
        "author": {
            "id": author["id"], "username": author["username"], "name": author["name"],
            "avatar_url": f"/media/{author['avatar']}" if author["avatar"] else None,
        },
        "media_url": f"/media/{row['media']}",
        "media_type": row["media_type"],
        "caption": row["caption"],
        "created_at": row["created_at"],
        "expires_at": row["expires_at"],
        "seen": seen,
        "view_count": view_count,
        "my_reaction": my_reaction,
    }

@app.post("/api/stories", status_code=201)
def create_story(caption: str = Form(""), file: UploadFile = File(...), me=Depends(current_user)):
    name = save_upload(file, IMAGE_EXTS | VIDEO_EXTS)
    ext = Path(name).suffix.lower()
    media_type = "video" if ext in VIDEO_EXTS else "image"
    now = int(time.time())
    with db() as c:
        cur = c.execute(
            "INSERT INTO stories (user_id, media, media_type, caption, created_at, expires_at) "
            "VALUES (?,?,?,?,?,?)",
            (me["id"], name, media_type, caption.strip()[:500], now, now + STORY_TTL))
        row = c.execute("SELECT * FROM stories WHERE id = ?", (cur.lastrowid,)).fetchone()
    return story_public(row, me["id"])

@app.get("/api/stories/feed")
def stories_feed(me=Depends(current_user)):
    cleanup_expired_stories()
    now = int(time.time())
    with db() as c:
        rows = c.execute(
            """SELECT s.* FROM stories s
               WHERE s.expires_at > ?
                 AND (s.user_id = ?
                      OR s.user_id IN (SELECT followed_id FROM follows
                                       WHERE follower_id = ? AND followed_id != follower_id))
               ORDER BY s.created_at ASC""",
            (now, me["id"], me["id"])).fetchall()
    groups: dict = {}
    order = []
    for r in rows:
        d = story_public(r, me["id"])
        uid = d["author"]["id"]
        if uid not in groups:
            groups[uid] = {"author": d["author"], "has_unseen": False, "items": []}
            order.append(uid)
        groups[uid]["items"].append(d)
        if not d["seen"]:
            groups[uid]["has_unseen"] = True
    result = [groups[u] for u in order]
    # قصتي أولاً، ثم المجموعات حسب الأحدث
    result.sort(key=lambda g: (g["author"]["id"] != me["id"],
                               -max(i["created_at"] for i in g["items"])))
    return result

@app.post("/api/stories/{story_id}/view")
def view_story(story_id: int, me=Depends(current_user)):
    now = int(time.time())
    with db() as c:
        if not c.execute("SELECT 1 FROM stories WHERE id = ? AND expires_at > ?",
                         (story_id, now)).fetchone():
            raise HTTPException(404, "الستوري غير موجود")
        c.execute("INSERT OR IGNORE INTO story_views (story_id, user_id, created_at) VALUES (?,?,?)",
                  (story_id, me["id"], now))
    return {"ok": True}

class ReactIn(BaseModel):
    emoji: str

@app.post("/api/stories/{story_id}/react")
def react_story(story_id: int, body: ReactIn, me=Depends(current_user)):
    if body.emoji not in STORY_EMOJIS:
        raise HTTPException(400, "تفاعل غير مدعوم")
    now = int(time.time())
    with db() as c:
        if not c.execute("SELECT 1 FROM stories WHERE id = ? AND expires_at > ?",
                         (story_id, now)).fetchone():
            raise HTTPException(404, "الستوري غير موجود")
        c.execute(
            "INSERT INTO story_reactions (story_id, user_id, emoji, created_at) VALUES (?,?,?,?) "
            "ON CONFLICT(story_id, user_id) DO UPDATE SET emoji=excluded.emoji, "
            "created_at=excluded.created_at",
            (story_id, me["id"], body.emoji, now))
    return {"ok": True, "emoji": body.emoji}

@app.get("/api/stories/{story_id}/viewers")
def story_viewers(story_id: int, me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if not row:
            raise HTTPException(404, "الستوري غير موجود")
        if row["user_id"] != me["id"]:
            raise HTTPException(403, "قائمة المشاهدين لصاحب الستوري فقط")
        rows = c.execute(
            """SELECT u.* FROM users u JOIN story_views v ON v.user_id = u.id
               WHERE v.story_id = ? ORDER BY v.created_at DESC""", (story_id,)).fetchall()
    return [user_public(r, me["id"]) for r in rows]

@app.delete("/api/stories/{story_id}")
def delete_story(story_id: int, me=Depends(current_user)):
    with db() as c:
        row = c.execute("SELECT * FROM stories WHERE id = ?", (story_id,)).fetchone()
        if not row:
            raise HTTPException(404, "الستوري غير موجود")
        if row["user_id"] != me["id"]:
            raise HTTPException(403, "ليس الستوري الخاص بك")
        c.execute("DELETE FROM stories WHERE id = ?", (story_id,))
    try: (UPLOAD_DIR / row["media"]).unlink()
    except OSError: pass
    return {"ok": True}

# ---------------------------------------------------------- الرسائل الخاصة
def _convo_or_404(convo_id: int, me_id: int):
    with db() as c:
        ok = c.execute(
            "SELECT 1 FROM conversation_participants WHERE conversation_id = ? AND user_id = ?",
            (convo_id, me_id)).fetchone()
    if not ok:
        raise HTTPException(404, "المحادثة غير موجودة")

def _other_user(convo_id: int, me_id: int):
    with db() as c:
        return c.execute(
            """SELECT u.* FROM users u
               JOIN conversation_participants p ON p.user_id = u.id
               WHERE p.conversation_id = ? AND u.id != ? LIMIT 1""",
            (convo_id, me_id)).fetchone()

def convo_public(convo_id: int, me_id: int) -> dict:
    with db() as c:
        other = _other_user(convo_id, me_id)
        last = c.execute(
            "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT 1",
            (convo_id,)).fetchone()
        unread = c.execute(
            "SELECT COUNT(*) n FROM messages WHERE conversation_id = ? "
            "AND sender_id != ? AND read_at IS NULL",
            (convo_id, me_id)).fetchone()["n"]
        created = c.execute("SELECT created_at FROM conversations WHERE id = ?",
                            (convo_id,)).fetchone()["created_at"]
    return {
        "id": convo_id,
        "other": {
            "id": other["id"], "username": other["username"], "name": other["name"],
            "avatar_url": f"/media/{other['avatar']}" if other["avatar"] else None,
        } if other else None,
        "last_text": last["text"] if last else "",
        "last_media_type": last["media_type"] if last else "",
        "last_created_at": last["created_at"] if last else created,
        "last_sender_id": last["sender_id"] if last else 0,
        "unread_count": unread,
    }

def message_public(row, me_id: int) -> dict:
    with db() as c:
        sender = c.execute("SELECT * FROM users WHERE id = ?", (row["sender_id"],)).fetchone()
        story = None
        if row["reply_story_id"]:
            s = c.execute("SELECT * FROM stories WHERE id = ?", (row["reply_story_id"],)).fetchone()
            if s:
                sa = c.execute("SELECT username FROM users WHERE id = ?",
                               (s["user_id"],)).fetchone()
                story = {"id": s["id"], "media_url": f"/media/{s['media']}",
                         "media_type": s["media_type"],
                         "author_username": sa["username"] if sa else ""}
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "sender": {"id": sender["id"], "username": sender["username"], "name": sender["name"],
                   "avatar_url": f"/media/{sender['avatar']}" if sender["avatar"] else None},
        "text": row["text"],
        "media_url": f"/media/{row['media']}" if row["media"] else None,
        "media_type": row["media_type"],
        "reply_story": story,
        "created_at": row["created_at"],
        "is_mine": me_id == row["sender_id"],
        "is_read": row["read_at"] is not None,
    }

class ConvoIn(BaseModel):
    username: str

@app.post("/api/conversations", status_code=201)
def get_or_create_convo(body: ConvoIn, me=Depends(current_user)):
    target = body.username.strip().lower()
    with db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (target,)).fetchone()
        if not row:
            raise HTTPException(404, "المستخدم غير موجود")
        if row["id"] == me["id"]:
            raise HTTPException(400, "لا يمكنك مراسلة نفسك")
        found = c.execute(
            """SELECT c.id FROM conversations c
               JOIN conversation_participants p1 ON p1.conversation_id = c.id AND p1.user_id = ?
               JOIN conversation_participants p2 ON p2.conversation_id = c.id AND p2.user_id = ?
               WHERE (SELECT COUNT(*) FROM conversation_participants p
                      WHERE p.conversation_id = c.id) = 2""",
            (me["id"], row["id"])).fetchone()
        if found:
            convo_id = found["id"]
            created_new = False
        else:
            now = int(time.time())
            cur = c.execute("INSERT INTO conversations (created_at) VALUES (?)", (now,))
            convo_id = cur.lastrowid
            for uid in (me["id"], row["id"]):
                c.execute("INSERT INTO conversation_participants "
                          "(conversation_id, user_id, created_at) VALUES (?,?,?)",
                          (convo_id, uid, now))
            created_new = True
    data = convo_public(convo_id, me["id"])
    data["created_new"] = created_new
    return data

@app.get("/api/conversations")
def list_convos(me=Depends(current_user)):
    with db() as c:
        ids = [r["conversation_id"] for r in c.execute(
            "SELECT conversation_id FROM conversation_participants WHERE user_id = ?",
            (me["id"],)).fetchall()]
    convos = [convo_public(i, me["id"]) for i in ids]
    convos.sort(key=lambda x: x["last_created_at"], reverse=True)
    return convos

@app.get("/api/conversations/{convo_id}/messages")
def list_messages(convo_id: int, limit: int = Query(30, le=50), before_id: int = 0,
                  me=Depends(current_user)):
    _convo_or_404(convo_id, me["id"])
    with db() as c:
        if before_id:
            rows = c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? AND id < ? "
                "ORDER BY id DESC LIMIT ?", (convo_id, before_id, limit)).fetchall()
        else:
            rows = c.execute(
                "SELECT * FROM messages WHERE conversation_id = ? ORDER BY id DESC LIMIT ?",
                (convo_id, limit)).fetchall()
    return [message_public(r, me["id"]) for r in reversed(rows)]

@app.post("/api/conversations/{convo_id}/messages", status_code=201)
def send_message(convo_id: int, text: str = Form(""),
                 reply_story_id: Optional[int] = Form(None),
                 file: Optional[UploadFile] = File(None), me=Depends(current_user)):
    _convo_or_404(convo_id, me["id"])
    text = (text or "").strip()[:2000]
    media = ""
    media_type = ""
    if file is not None and file.filename:
        media = save_upload(file, IMAGE_EXTS | VIDEO_EXTS)
        media_type = "video" if Path(media).suffix.lower() in VIDEO_EXTS else "image"
    if not text and not media:
        raise HTTPException(400, "الرسالة فارغة")
    if reply_story_id:
        with db() as c:
            if not c.execute("SELECT 1 FROM stories WHERE id = ?",
                             (reply_story_id,)).fetchone():
                raise HTTPException(404, "الستوري غير موجود")
    now = int(time.time())
    with db() as c:
        cur = c.execute(
            "INSERT INTO messages (conversation_id, sender_id, text, media, media_type, "
            "reply_story_id, created_at) VALUES (?,?,?,?,?,?,?)",
            (convo_id, me["id"], text, media, media_type, reply_story_id, now))
        row = c.execute("SELECT * FROM messages WHERE id = ?", (cur.lastrowid,)).fetchone()
    return message_public(row, me["id"])

@app.post("/api/conversations/{convo_id}/read")
def mark_read(convo_id: int, me=Depends(current_user)):
    _convo_or_404(convo_id, me["id"])
    now = int(time.time())
    with db() as c:
        c.execute("UPDATE messages SET read_at = ? WHERE conversation_id = ? "
                  "AND sender_id != ? AND read_at IS NULL",
                  (now, convo_id, me["id"]))
    return {"ok": True}

# ---------------------------------------------------------- الملفات
@app.get("/media/{filename}")
def serve_media(filename: str):
    if not re.match(r"^[a-f0-9]{32}\.[a-z0-9]+$", filename):
        raise HTTPException(404, "غير موجود")
    path = UPLOAD_DIR / filename
    if not path.is_file():
        raise HTTPException(404, "غير موجود")
    return FileResponse(path)

@app.get("/api/health")
def health():
    return {"ok": True, "service": "laqta", "version": SERVER_VERSION}
