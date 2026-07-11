# -*- coding: utf-8 -*-
"""
🏭 کارخانه ویدیوی خودکار — نسخه ۱
همه‌چیز رایگان: Gemini (سناریو) + edge-tts (صدا) + Pexels (تصویر) + FFmpeg (مونتاژ)
+ YouTube Data API + Instagram Graph API
اجرا روی GitHub Actions — دو فاز:
    python factory.py render    → ساخت ویدیو + آپلود یوتیوب
    python factory.py publish   → انتشار در اینستاگرام (بعد از push شدن فایل)
"""

import os, re, sys, json, time, base64, asyncio, subprocess
from pathlib import Path
from datetime import datetime, timezone

import requests
import yaml
import edge_tts

# ---------- مسیرها ----------
ROOT = Path(__file__).parent
OUT = ROOT / "output"
WORK = ROOT / "work"
FONTS = ROOT / "fonts"
META = ROOT / "meta.json"

GRAPH = "https://graph.instagram.com/v21.0"


def log(msg):
    print(msg, flush=True)


def die(msg):
    log(f"❌ {msg}")
    sys.exit(1)


def run(cmd):
    """اجرای دستور شل با نمایش خطا"""
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(r.stderr[-3000:])
        die(f"دستور شکست خورد: {cmd[0]}")
    return r


def ffprobe_duration(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True)
    return float(r.stdout.strip())


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name):
    v = os.environ.get(name, "").strip()
    return v or None


# =====================================================
# ۱) تولید سناریو با Gemini (رایگان)
# =====================================================
def generate_script(cfg):
    key = env("GEMINI_API_KEY") or die("سیکرت GEMINI_API_KEY تنظیم نشده (مرحله ۲ راهنما)")
    model = cfg.get("model", "gemini-2.0-flash")

    import random
    hint = ""
    hints = cfg.get("topic_hints") or []
    if hints:
        hint = f"\nپیشنهاد موضوع امروز (می‌تونی خلاقانه تغییرش بدی): {random.choice(hints)}"

    n = int(cfg.get("scenes_count", 5))
    prompt = f"""تو سناریونویس ویدیوهای کوتاه عمودی (یوتیوب شورتس / ریلز) هستی.

نیچ کانال: {cfg['niche']}
{cfg.get('extra_style_notes', '')}{hint}

یک سناریوی جدید بنویس. خروجی فقط و فقط JSON با دقیقاً این ساختار:
{{
  "title": "عنوان جذاب فارسی، حداکثر ۸۵ کاراکتر",
  "caption": "کپشن فارسی ۱ تا ۲ جمله برای اینستاگرام",
  "scenes": [
    {{"narration": "متن گفتار فارسی این صحنه", "keywords": "2-3 english words for stock video"}}
  ]
}}

قوانین:
- دقیقاً {n} صحنه.
- جمله اول باید قلاب (hook) قوی باشد؛ بدون سلام و مقدمه.
- هر narration حداکثر ۲۲ کلمه، محاوره‌ای و ساده، بدون ایموجی و بدون علامت * یا #.
- keywords انگلیسی، مناسب جستجوی ویدیوی استوک سینمایی عمودی (مثل: ocean storm night).
- صحنه آخر با یک جمله باز یا سوال تمام شود تا مخاطب کامنت بگذارد."""

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 1.0, "topP": 0.95,
        },
    }
    last_err = ""
    for attempt in range(3):
        r = requests.post(url, json=body, timeout=90)
        if r.status_code != 200:
            last_err = r.text[:500]
            time.sleep(5)
            continue
        try:
            text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
            text = re.sub(r"^```(json)?|```$", "", text.strip(), flags=re.M)
            data = json.loads(text)
            assert data.get("title") and len(data.get("scenes", [])) >= 3
            log(f"📝 سناریو آماده شد: {data['title']}")
            return data
        except Exception as e:
            last_err = f"{e}"
            time.sleep(3)
    die(f"Gemini جواب معتبر نداد: {last_err}")


def clean_for_tts(s):
    s = re.sub(r"[\U00010000-\U0010FFFF]", "", s)          # حذف ایموجی
    s = re.sub(r"[*_#`\"«»]", "", s)
    return re.sub(r"\s+", " ", s).strip()


# =====================================================
# ۲) صداگذاری با edge-tts + زمان‌بندی کلمه‌به‌کلمه
# =====================================================
async def tts_scene(text, voice, rate, mp3_path):
    com = edge_tts.Communicate(text, voice, rate=rate)
    words = []
    with open(mp3_path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append((ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"]))
    return words


# =====================================================
# ۳) زیرنویس ASS فارسی (راست‌به‌چپ با فونت وزیرمتن)
# =====================================================
def ass_time(t):
    cs = max(0, int(round(t * 100)))
    return f"{cs // 360000}:{(cs // 6000) % 60:02d}:{(cs // 100) % 60:02d}.{cs % 100:02d}"


ASS_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Sub,Vazirmatn,74,&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,0,0,1,3.4,1.4,2,70,70,320,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def build_ass(all_words, path, max_words=3, max_chars=24):
    lines, group = [], []
    def flush():
        if not group:
            return
        st = group[0][0]
        en = group[-1][0] + max(group[-1][1], 0.15) + 0.08
        txt = " ".join(w[2] for w in group)
        lines.append(f"Dialogue: 0,{ass_time(st)},{ass_time(en)},Sub,,0,0,0,,{txt}")
        group.clear()

    for w in all_words:
        group.append(w)
        chars = sum(len(x[2]) + 1 for x in group)
        if len(group) >= max_words or chars >= max_chars:
            flush()
    flush()
    path.write_text(ASS_HEADER + "\n".join(lines), encoding="utf-8")


# =====================================================
# ۴) کلیپ استوک از Pexels (رایگان)
# =====================================================
def pexels_clip(keywords, dest, used_ids):
    key = env("PEXELS_API_KEY") or die("سیکرت PEXELS_API_KEY تنظیم نشده (مرحله ۲ راهنما)")
    for q in [keywords, "cinematic nature", "abstract dark background"]:
        r = requests.get(
            "https://api.pexels.com/videos/search",
            headers={"Authorization": key},
            params={"query": q, "orientation": "portrait", "per_page": 10},
            timeout=60)
        if r.status_code != 200:
            continue
        for v in r.json().get("videos", []):
            if v["id"] in used_ids:
                continue
            files = [f for f in v["video_files"]
                     if f.get("file_type") == "video/mp4" and (f.get("height") or 0) >= 1080]
            if not files:
                continue
            files.sort(key=lambda f: f["height"])
            url = files[0]["link"]
            with requests.get(url, stream=True, timeout=180) as dl:
                dl.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in dl.iter_content(1 << 20):
                        f.write(chunk)
            used_ids.add(v["id"])
            log(f"🎞️ کلیپ استوک: {q} (id {v['id']})")
            return
    die(f"هیچ کلیپی برای «{keywords}» پیدا نشد")


# =====================================================
# ۵) مونتاژ با FFmpeg
# =====================================================
def build_video(cfg, script):
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)
    voice = cfg.get("voice", "fa-IR-FaridNeural")
    rate = cfg.get("rate", "+8%")

    all_words, scene_files, audio_files = [], [], []
    used_ids, t_offset = set(), 0.0

    for i, scene in enumerate(script["scenes"]):
        text = clean_for_tts(scene["narration"])
        if not text:
            continue
        mp3 = WORK / f"voice_{i}.mp3"
        words = asyncio.run(tts_scene(text, voice, rate, mp3))
        dur = ffprobe_duration(mp3) + 0.25

        raw = WORK / f"stock_{i}.mp4"
        pexels_clip(scene.get("keywords", "cinematic"), raw, used_ids)

        seg = WORK / f"scene_{i}.mp4"
        run(["ffmpeg", "-y", "-stream_loop", "-1", "-i", str(raw), "-t", f"{dur:.3f}",
             "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,"
                    "crop=1080:1920,fps=30,setsar=1",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-pix_fmt", "yuv420p", str(seg)])

        scene_files.append(seg)
        audio_files.append(mp3)
        all_words += [(t_offset + w[0], w[1], w[2]) for w in words]
        t_offset += dur
        log(f"🎬 صحنه {i + 1} آماده شد ({dur:.1f}s)")

    (WORK / "v.txt").write_text("\n".join(f"file '{f.resolve()}'" for f in scene_files))
    (WORK / "a.txt").write_text("\n".join(f"file '{f.resolve()}'" for f in audio_files))

    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(WORK / "v.txt"),
         "-c", "copy", str(WORK / "silent.mp4")])
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(WORK / "a.txt"),
         "-c:a", "aac", "-b:a", "160k", str(WORK / "voice.m4a")])

    subs = WORK / "subs.ass"
    build_ass(all_words, subs,
              max_words=int(cfg.get("caption_words", 3)))

    fname = f"video_{datetime.now(timezone.utc):%Y%m%d_%H%M}.mp4"
    final = OUT / fname
    run(["ffmpeg", "-y", "-i", str(WORK / "silent.mp4"), "-i", str(WORK / "voice.m4a"),
         "-vf", f"subtitles={subs.as_posix()}:fontsdir={FONTS.as_posix()}",
         "-map", "0:v", "-map", "1:a",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-shortest",
         str(final)])

    log(f"✅ ویدیو ساخته شد: {fname} ({t_offset:.0f} ثانیه، {final.stat().st_size // 1024} KB)")
    if t_offset > 175:
        log("⚠️ ویدیو از ۳ دقیقه بلندتر است؛ شاید Short حساب نشود. scenes_count را کم کن.")
    return fname


def cleanup_old(cfg):
    keep = int(cfg.get("keep_days", 7))
    today = datetime.now(timezone.utc)
    for f in OUT.glob("video_*.mp4"):
        try:
            d = datetime.strptime(f.name[6:14], "%Y%m%d").replace(tzinfo=timezone.utc)
            if (today - d).days > keep:
                f.unlink()
                log(f"🧹 حذف فایل قدیمی {f.name}")
        except ValueError:
            pass


# =====================================================
# ۶) آپلود یوتیوب (REST خام، بدون SDK)
# =====================================================
def youtube_upload(cfg, script, fname):
    cid, csec, rtok = env("YT_CLIENT_ID"), env("YT_CLIENT_SECRET"), env("YT_REFRESH_TOKEN")
    if not (cid and csec and rtok):
        log("⏭️ سیکرت‌های یوتیوب تنظیم نشده‌اند — از یوتیوب رد شدیم (مرحله ۵ راهنما)")
        return
    if not cfg.get("enable_youtube", True):
        log("⏭️ یوتیوب در config خاموش است")
        return

    r = requests.post("https://oauth2.googleapis.com/token", data={
        "client_id": cid, "client_secret": csec,
        "refresh_token": rtok, "grant_type": "refresh_token"}, timeout=60)
    if r.status_code != 200:
        die(f"گرفتن توکن یوتیوب شکست خورد (refresh token را دوباره بگیر — مرحله ۵): {r.text[:300]}")
    token = r.json()["access_token"]

    title = script["title"].strip()[:92]
    if "#shorts" not in title.lower():
        title += " #Shorts"
    tags = [h.lstrip("#") for h in cfg.get("hashtags", [])][:15]
    body = {
        "snippet": {"title": title,
                    "description": script.get("caption", "") + "\n\n" + " ".join(cfg.get("hashtags", [])),
                    "tags": tags,
                    "categoryId": str(cfg.get("category_id", 27))},
        "status": {"privacyStatus": cfg.get("privacy_status", "public"),
                   "selfDeclaredMadeForKids": False},
    }
    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={"uploadType": "resumable", "part": "snippet,status"},
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json; charset=UTF-8"},
        json=body, timeout=60)
    if init.status_code not in (200, 201):
        die(f"شروع آپلود یوتیوب شکست خورد: {init.status_code} {init.text[:400]}")
    loc = init.headers["Location"]
    with open(OUT / fname, "rb") as f:
        up = requests.put(loc, data=f,
                          headers={"Authorization": f"Bearer {token}",
                                   "Content-Type": "video/mp4"}, timeout=600)
    if up.status_code in (200, 201):
        vid = up.json().get("id")
        log(f"▶️ آپلود یوتیوب موفق: https://youtube.com/watch?v={vid}")
        log("ℹ️ اگر ویدیو private و قفل‌شده است، پروژه‌ات هنوز Audit گوگل را نگرفته (مرحله ۵ راهنما).")
    else:
        die(f"آپلود یوتیوب شکست خورد: {up.status_code} {up.text[:400]}")


# =====================================================
# ۷) اینستاگرام: آدرس عمومی ویدیو + انتشار Reels
# =====================================================
def public_urls(fname):
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    branch = os.environ.get("GITHUB_REF_NAME", "main")
    owner, name = repo.split("/", 1)
    return [
        f"https://{owner}.github.io/{name}/output/{fname}",          # GitHub Pages (اولویت)
        f"https://raw.githubusercontent.com/{repo}/{branch}/output/{fname}",  # جایگزین
    ]


def wait_public(url, tries=26, delay=10):
    for _ in range(tries):
        try:
            h = requests.head(url, allow_redirects=True, timeout=30)
            if h.status_code == 200 and int(h.headers.get("content-length", "0")) > 100_000:
                return True
        except requests.RequestException:
            pass
        time.sleep(delay)
    return False


def refresh_ig_token(tok):
    """تمدید توکن ۶۰روزه + ذخیره خودکار در Secrets اگر GH_PAT موجود باشد"""
    pat = env("GH_PAT")
    if not pat:
        log("ℹ️ توکن اینستاگرام هر ۶۰ روز می‌میرد. برای تمدید خودکار، سیکرت GH_PAT را اضافه کن (مرحله ۶).")
        return tok
    r = requests.get("https://graph.instagram.com/refresh_access_token",
                     params={"grant_type": "ig_refresh_token", "access_token": tok}, timeout=60)
    if r.status_code != 200:
        log(f"⚠️ تمدید توکن نشد (اگر توکن تازه است طبیعی است): {r.text[:200]}")
        return tok
    new_tok = r.json()["access_token"]
    try:
        from nacl import encoding, public
        repo = os.environ["GITHUB_REPOSITORY"]
        hdr = {"Authorization": f"Bearer {pat}", "Accept": "application/vnd.github+json"}
        k = requests.get(f"https://api.github.com/repos/{repo}/actions/secrets/public-key",
                         headers=hdr, timeout=60).json()
        sealed = public.SealedBox(public.PublicKey(k["key"].encode(), encoding.Base64Encoder())
                                  ).encrypt(new_tok.encode())
        requests.put(f"https://api.github.com/repos/{repo}/actions/secrets/IG_ACCESS_TOKEN",
                     headers=hdr, timeout=60,
                     json={"encrypted_value": base64.b64encode(sealed).decode(),
                           "key_id": k["key_id"]})
        log("🔄 توکن اینستاگرام تمدید و در Secrets ذخیره شد")
    except Exception as e:
        log(f"⚠️ ذخیره توکن جدید نشد: {e}")
    return new_tok


def instagram_publish(cfg, meta):
    tok, uid = env("IG_ACCESS_TOKEN"), env("IG_USER_ID")
    if not (tok and uid):
        log("⏭️ سیکرت‌های اینستاگرام تنظیم نشده‌اند — از اینستاگرام رد شدیم (مرحله ۶ راهنما)")
        return
    if not cfg.get("enable_instagram", True):
        log("⏭️ اینستاگرام در config خاموش است")
        return

    tok = refresh_ig_token(tok)

    video_url = None
    for u in public_urls(meta["filename"]):
        log(f"⏳ منتظر در دسترس شدن: {u}")
        if wait_public(u):
            video_url = u
            break
    if not video_url:
        die("ویدیو عمومی نشد. GitHub Pages را فعال کرده‌ای؟ (مرحله ۳ راهنما)")

    caption = meta["caption"] + "\n\n" + " ".join(cfg.get("hashtags", []))
    r = requests.post(f"{GRAPH}/{uid}/media", timeout=120, data={
        "media_type": "REELS", "video_url": video_url,
        "caption": caption[:2100], "share_to_feed": "true",
        "access_token": tok})
    if r.status_code != 200:
        die(f"ساخت کانتینر ریلز شکست خورد: {r.text[:400]}")
    cid = r.json()["id"]

    for _ in range(40):
        s = requests.get(f"{GRAPH}/{cid}",
                         params={"fields": "status_code,status", "access_token": tok},
                         timeout=60).json()
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code == "ERROR":
            die(f"اینستاگرام ویدیو را رد کرد: {s}")
        time.sleep(10)
    else:
        die("پردازش ریلز خیلی طول کشید")

    p = requests.post(f"{GRAPH}/{uid}/media_publish", timeout=120,
                      data={"creation_id": cid, "access_token": tok})
    if p.status_code != 200:
        die(f"انتشار ریلز شکست خورد: {p.text[:400]}")
    log(f"📸 ریلز منتشر شد (media id: {p.json().get('id')})")


# =====================================================
# main
# =====================================================
def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "render"
    cfg = load_config()

    if mode == "render":
        script = generate_script(cfg)
        fname = build_video(cfg, script)
        cleanup_old(cfg)
        META.write_text(json.dumps({
            "filename": fname,
            "title": script["title"],
            "caption": script.get("caption", script["title"]),
            "made_at": datetime.now(timezone.utc).isoformat(),
        }, ensure_ascii=False, indent=1), encoding="utf-8")
        youtube_upload(cfg, script, fname)

    elif mode == "publish":
        if not META.exists():
            die("meta.json نیست؛ اول render اجرا شود")
        instagram_publish(cfg, json.loads(META.read_text(encoding="utf-8")))

    else:
        die("حالت نامعتبر؛ render یا publish")


if __name__ == "__main__":
    main()
