# لقطة (Laqta) — السيرفر 📸

سيرفر الشبكة الاجتماعية المصغرة لتطبيق **لقطة** (شبيه إنستغرام).
FastAPI + SQLite — ملف واحد `server.py`. **النسخة الحالية: v3.0**

## ⚠️ الترقية إلى v3.0 (مهم للمثبتين سابقاً)

نسخة السيرفر **v3.0** تضيف:
- **الستوري** (`stories`, `story_views`, `story_reactions`): نشر، مشاهدات، تفاعلات، حذف تلقائي بعد 24 ساعة.
- **الرسائل الخاصة** (`conversations`, `conversation_participants`, `messages`): محادثات 1:1، نص/صور/فيديو، علامات قراءة، الرد على الستوري.

**الترقية تلقائية:** عند تشغيل السيرفر الجديد على قاعدة بيانات قديمة (v1.x أو v2.0)،
تُنشأ الجداول الجديدة تلقائياً (`CREATE TABLE IF NOT EXISTS`) — **لا تُفقد أي بيانات**
(تم اختبار الترحيل على قاعدة v1 قديمة بنجاح).

خطوات الترقية على Render:
1. استبدل ملفات المستودع بنسخة `laqta-server-v3.zip` (أو اسحب آخر نسخة).
2. Render سيعيد النشر تلقائياً — انتظر حتى يصبح **Live**.
3. تحقق: `https://<رابطك>/api/health` يجب أن يعيد `"version": "3.0"`.

> ملاحظة: تطبيق لقطة **v3.0** يتطلب سيرفر v3.0 (للستوري والرسائل).
> تطبيق v2.0/v1.x يعمل مع سيرفر v3.0 بدون مشاكل (النقاط الجديدة اختيارية).

## التشغيل محلياً

```bash
pip install -r requirements.txt
./run.sh
# أو: uvicorn server:app --host 0.0.0.0 --port 8000
```

السيرفر يعمل على `http://localhost:8000` — تحقق: `http://localhost:8000/api/health`

## متغيرات البيئة

| المتغير | الافتراضي | الوصف |
|---|---|---|
| `DATA_DIR` | `./data` | مجلد قاعدة البيانات (`laqta.db`) والملفات المرفوعة |
| `MAX_UPLOAD_MB` | `50` | الحد الأقصى لحجم الملف المرفوع |

## النشر على Render (مجاني)

1. ارفع محتويات هذا المجلد إلى مستودع GitHub جديد (كل الملفات في الجذر).
2. في [render.com](https://render.com): **New + → Web Service** ← اختر المستودع.
3. سيكتشف Render ملف `render.yaml` تلقائياً (Docker).
4. بعد النشر ستحصل على رابط مثل `https://laqta-server.onrender.com`.
5. في تطبيق لقطة: **البروفايل ← الإعدادات ← رابط السيرفر** والصق الرابط.

> ⚠️ الخطة المجانية في Render تُطفئ السيرفر بعد 15 دقيقة من عدم الاستخدام
> (يستيقظ عند أول طلب — قد يتأخر أول تحميل ~30 ثانية)،
> وقد تُفقد البيانات عند إعادة النشر. للاستخدام الجاد يلزم خطة مدفوعة (~$7/شهر).

## توثيق الـ API

المصادقة: ترويسة `Authorization: Bearer <token>` (يُرجعها `/api/register` و `/api/login`).

### الحسابات
| الطريقة | المسار | الوصف |
|---|---|---|
| POST | `/api/register` | تسجيل: `{username, password, name}` ← `{token, user}` |
| POST | `/api/login` | دخول: `{username, password}` ← `{token, user}` |
| POST | `/api/logout` | خروج (إبطال التوكن) |
| GET | `/api/me` | بروفايلي مع العدّادات |
| PATCH | `/api/me` | تعديل `{name, bio}` |
| POST | `/api/me/avatar` | رفع صورة البروفايل (multipart `file`) |

### المستخدمون
| الطريقة | المسار | الوصف |
|---|---|---|
| GET | `/api/users/search?q=` | بحث بالاسم |
| GET | `/api/users/{username}` | بروفايل مستخدم (+`is_following`) |
| GET | `/api/users/{username}/posts` | منشورات مستخدم |
| POST | `/api/users/{username}/follow` | متابعة/إلغاء (تبديل) ← `{following}` |
| GET | `/api/users/{username}/followers` | قائمة المتابِعين |
| GET | `/api/users/{username}/following` | قائمة المتابَعين |

### المنشورات
| الطريقة | المسار | الوصف |
|---|---|---|
| POST | `/api/posts` | نشر (multipart: `file` + `caption`) ← المنشور |
| GET | `/api/feed?limit=&offset=` | خلاصة المتابَعين (زمني تنازلي) |
| GET | `/api/posts/{id}` | تفاصيل المنشور + التعليقات |
| DELETE | `/api/posts/{id}` | حذف (صاحب المنشور فقط) |
| POST | `/api/posts/{id}/like` | لايك/إلغاء (تبديل) ← `{liked, like_count}` |
| POST | `/api/posts/{id}/view` | تسجيل مشاهدة (مرة واحدة لكل مستخدم) ← `{ok, view_count}` ✨ جديد في v2.0 |
| GET | `/api/posts/{id}/comments` | قائمة التعليقات |
| POST | `/api/posts/{id}/comments` | إضافة تعليق `{text}` |
| DELETE | `/api/comments/{id}` | حذف تعليق (صاحبه أو صاحب المنشور) |

### النشاط والملفات
| الطريقة | المسار | الوصف |
|---|---|---|
| GET | `/api/activity` | لايكات/تعليقات/متابَعات جديدة على حسابي |
| GET | `/media/{file}` | الملفات المرفوعة (صور/فيديو/أفاتار) |
| GET | `/api/health` | فحص السلامة |

كائن المستخدم: `{id, username, name, bio, avatar_url, followers_count, following_count, posts_count, is_following, is_self}`

كائن المنشور: `{id, author{id,username,name,avatar_url}, media_url, media_type, caption, like_count, comment_count, view_count, liked_by_me, is_mine, created_at}`

> `view_count` (جديد في v2.0): عدد المشاهدات الفريدة (مرة واحدة لكل مستخدم).

## ملاحظات
- كلمات المرور محفوظة بتجزئة PBKDF2-SHA256 مع salt — لا تُحفظ نصاً صريحاً.
- المستخدم الجديد يتابع نفسه تلقائياً ليرى منشوراته في خلاصته (لا تُحتسب في العدّادات).
- أنواع الملفات: صور `jpg/png/webp/gif` — فيديو `mp4/mov/webm/3gp/mkv`.
- هذا وضع تجريبي: التسجيل بـ username/password بدون تحقق SMS.
