# -*- coding: utf-8 -*-
"""
🏭 کارخانه ویدیوی خودکار — نسخه ۵
همه‌چیز رایگان: Gemini (سناریو) + edge-tts (صدا) + Pexels/Pixabay (تصویر) + FFmpeg (مونتاژ)
+ YouTube Data API + Instagram Graph API
اجرا روی GitHub Actions — دو فاز:
    python factory.py render    → ساخت ویدیو + آپلود یوتیوب
    python factory.py publish   → انتشار در اینستاگرام (بعد از push شدن فایل)
"""

import os, re, sys, json, time, base64, asyncio, subprocess, random, hashlib
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
CACHE_DIR = ROOT / "cache"
STOCK_CACHE = CACHE_DIR / "stock_search.json"

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
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        die("config.yaml باید یک YAML object معتبر باشد")

    return data


def env(name):
    v = os.environ.get(name, "").strip()
    return v or None


# =====================================================
# ۱) تولید سناریو با Gemini (رایگان)
# =====================================================
def _formal_terms_in_script(data):
    """عبارت‌های خیلی رسمی را در متن گفتاری پیدا می‌کند."""
    formal_terms = (
        "می‌باشد",
        "می باشد",
        "می‌باشند",
        "می باشند",
        "می‌گردد",
        "می گردد",
        "گردید",
        "لذا",
        "بدین ترتیب",
        "واقع شده است",
        "واقع گردیده",
        "به شمار می‌رود",
        "به شمار می رود",
        "نامبرده",
        "ایشان",
        "آنان",
    )

    joined = " ".join(
        str(scene.get("narration", ""))
        for scene in data.get("scenes", [])
        if isinstance(scene, dict)
    )

    return sorted({
        term
        for term in formal_terms
        if term in joined
    })


def _script_search_text(data):
    parts = [
        str(data.get("title", "")),
        str(data.get("caption", "")),
        str(data.get("comment", "")),
    ]
    for scene in data.get("scenes", []):
        if isinstance(scene, dict):
            parts.append(str(scene.get("narration", "")))
            parts.append(str(scene.get("keywords", "")))
    return " ".join(parts)


def _find_excluded_religious_terms(data, excluded_terms):
    """عبارت‌های مذهبی خارج از محدوده کانال را در خروجی پیدا می‌کند."""
    haystack = _script_search_text(data).casefold()
    found = []
    for item in excluded_terms:
        term = str(item).strip()
        if term and term.casefold() in haystack:
            found.append(term)
    return sorted(set(found))


def _is_fragrance_script(data):
    markers = (
        "عطر",
        "ادکلن",
        "رایحه",
        "پرفیوم",
        "perfume",
        "fragrance",
        "cologne",
        "eau de parfum",
        "eau de toilette",
    )
    haystack = _script_search_text(data).casefold()
    return any(marker.casefold() in haystack for marker in markers)


def _is_religious_script(data, cfg):
    religious_cfg = cfg.get("religious_content") or {}
    markers = [
        str(item).strip()
        for item in religious_cfg.get(
            "detection_terms",
            [
                "قرآن",
                "نهج‌البلاغه",
                "نهج البلاغه",
                "امام علی",
                "امام حسین",
                "اربعین",
                "عاشورا",
                "کربلا",
                "حضرت زینب",
                "حضرت فاطمه",
            ],
        )
        if str(item).strip()
    ]
    haystack = _script_search_text(data).casefold()
    return any(marker.casefold() in haystack for marker in markers)


def _fallback_youtube_comment(data, cfg):
    """کامنت جایگزین متنوع، وقتی مدل comment مناسب برنگرداند."""
    title = re.sub(r"\s+", " ", str(data.get("title", "این موضوع"))).strip()
    title = re.sub(r"#shorts", "", title, flags=re.I).strip()

    if _is_religious_script(data, cfg):
        templates = [
            "کدوم پیام این موضوع رو بیشتر می‌شه وارد زندگی روزمره کرد؟",
            "شما از این نکته چه برداشت عملی‌ای برای زندگی امروز دارید؟",
            "کدوم بخش این روایت بیشتر آدم رو به فکر می‌بره؟",
            "این مفهوم توی تصمیم‌های روزمره چه کمکی می‌تونه بکنه؟",
            "از نگاه شما مهم‌ترین درس این موضوع برای امروز چیه؟",
            "کدوم قسمت این موضوع براتون آرامش‌بخش‌تر یا تأمل‌برانگیزتر بود؟",
        ]
    elif _is_fragrance_script(data):
        templates = [
            "شما برای این فصل بیشتر رایحه خنک می‌پسندید یا گرم؟",
            "برای استفاده روزمره، ماندگاری براتون مهم‌تره یا پخش بو؟",
            "کدوم خانواده بویایی بیشتر با سلیقه‌تون جور درمیاد؟",
            "این نوع رایحه رو برای روز ترجیح می‌دید یا شب؟",
            "موقع انتخاب عطر اول به فصل توجه می‌کنید یا موقعیت استفاده؟",
            "برای هدیه‌دادن عطر، رایحه امن‌تر رو انتخاب می‌کنید یا خاص‌تر؟",
        ]
    else:
        templates = [
            f"کدوم بخش موضوع «{title}» بیشتر توجهتون رو جلب کرد؟",
            "این نکته رو قبلاً می‌دونستید یا براتون تازه بود؟",
            "به‌نظرتون کاربردی‌ترین بخش این موضوع کدومه؟",
            "شما تجربه مشابهی درباره این موضوع داشتید؟",
            "اگه قرار بود فقط یک نکته از این ویدیو یادتون بمونه، کدوم رو انتخاب می‌کردید؟",
            "کدوم بخش این داستان به نظرتون عجیب‌تر یا جالب‌تر بود؟",
            "این موضوع چه سؤالی توی ذهنتون ایجاد کرد؟",
            "شما این موضوع رو از چه زاویه‌ای می‌بینید؟",
        ]

    seed_text = title + str(env("GITHUB_RUN_NUMBER") or "")
    index = int(hashlib.sha256(seed_text.encode("utf-8")).hexdigest(), 16)
    return templates[index % len(templates)]


def _clean_generated_comment(comment):
    comment = re.sub(r"\s+", " ", str(comment or "")).strip()
    comment = re.sub(r"https?://\S+", "", comment).strip()
    comment = re.sub(r"#[\w\u0600-\u06FF_]+", "", comment).strip()
    return comment[:450]


def _normalize_hashtag(value):
    """هشتگ را به شکل استاندارد و بدون فاصله تبدیل می‌کند."""
    value = str(value or "").strip().lstrip("#")
    value = re.sub(r"\s+", "_", value)
    value = re.sub(
        r"[^\w\u0600-\u06FF_]",
        "",
        value,
        flags=re.UNICODE,
    )
    value = re.sub(r"_+", "_", value).strip("_")

    if not value:
        return ""

    return f"#{value}"


def _video_hashtags(cfg, script):
    """هشتگ‌های ثابت را با تعداد تنظیم‌شده هشتگ موضوعی ترکیب می‌کند."""
    fixed_hashtags = cfg.get("hashtags") or []
    dynamic_count = int(cfg.get("dynamic_hashtag_count", 5))
    dynamic_hashtags = list(script.get("hashtags") or [])[:dynamic_count]

    result = []
    seen = set()

    for item in list(fixed_hashtags) + dynamic_hashtags:
        hashtag = _normalize_hashtag(item)
        if not hashtag:
            continue

        key = hashtag.casefold()
        if key in seen:
            continue

        seen.add(key)
        result.append(hashtag)

    expected_max = len(fixed_hashtags) + dynamic_count
    return result[:expected_max]


_HASHTAG_STOP_WORDS = {
    "این", "اون", "آن", "برای", "درباره", "چرا", "چطور", "یک", "چه",
    "با", "از", "در", "به", "رو", "را", "و", "یا", "که", "روی",
    "بهترین", "مناسب", "موضوع", "ویدیو", "است", "هست", "چیست",
    "the", "a", "an", "and", "or", "for", "with", "of", "in", "to",
}


def _fallback_dynamic_hashtags(data, cfg, count, fixed_keys):
    """از عنوان، کپشن و کلیدواژه صحنه‌ها هشتگ موضوعی جایگزین می‌سازد."""
    candidates = []

    title = str(data.get("title", ""))
    caption = str(data.get("caption", ""))
    combined = f"{title} {caption}"

    # یک هشتگ فشرده از عنوان
    title_words = [
        word
        for word in re.findall(r"[\w\u0600-\u06FF]+", title)
        if len(word) >= 3 and word.casefold() not in _HASHTAG_STOP_WORDS
    ]
    if title_words:
        candidates.append("#" + "_".join(title_words[:4]))

    # کلمات مهم فارسی عنوان و کپشن
    for word in re.findall(r"[\w\u0600-\u06FF]+", combined):
        if len(word) < 3 or word.casefold() in _HASHTAG_STOP_WORDS:
            continue
        candidates.append("#" + word)

    # کلیدواژه‌های انگلیسی صحنه‌ها
    for scene in data.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        keywords = str(scene.get("keywords", ""))
        for word in re.findall(r"[A-Za-z][A-Za-z0-9]+", keywords):
            if len(word) >= 3 and word.casefold() not in _HASHTAG_STOP_WORDS:
                candidates.append("#" + word)

    # در موضوع عطر چند fallback دقیق‌تر
    if _is_fragrance_script(data):
        candidates.extend([
            "#عطر",
            "#رایحه",
            "#انتخاب_عطر",
            "#ماندگاری_عطر",
            "#پخش_بو",
        ])

    normalized = []
    seen = set()
    for item in candidates:
        hashtag = _normalize_hashtag(item)
        if not hashtag:
            continue
        key = hashtag.casefold()
        if key in fixed_keys or key in seen:
            continue
        seen.add(key)
        normalized.append(hashtag)
        if len(normalized) >= count:
            break

    return normalized


def generate_script(cfg):
    key = env("GEMINI_API_KEY") or die(
        "سیکرت GEMINI_API_KEY تنظیم نشده (مرحله ۲ راهنما)"
    )
    model = cfg.get("model", "gemini-2.0-flash")

    hint = ""
    hints = cfg.get("topic_hints") or []
    if hints:
        hint = (
            "\nپیشنهاد موضوع امروز "
            f"(می‌تونی خلاقانه تغییرش بدی): {random.choice(hints)}"
        )

    n = int(cfg.get("scenes_count", 5))
    dynamic_hashtag_count = int(
        cfg.get("dynamic_hashtag_count", 5)
    )

    fixed_hashtags = [
        _normalize_hashtag(item)
        for item in cfg.get("hashtags", [])
        if _normalize_hashtag(item)
    ]
    fixed_hashtags_text = " ".join(fixed_hashtags)
    spoken_notes = cfg.get("spoken_style_notes", "")
    spoken_dialect = cfg.get(
        "spoken_dialect",
        "فارسی محاوره‌ای تهرانیِ خنثی",
    )

    religious_cfg = cfg.get("religious_content") or {}
    religious_prompt_notes = str(
        religious_cfg.get("prompt_notes", "")
    ).strip()
    excluded_religious_topics = [
        str(item).strip()
        for item in religious_cfg.get("excluded_topics", [])
        if str(item).strip()
    ]
    excluded_religious_text = (
        "، ".join(excluded_religious_topics)
        if excluded_religious_topics
        else "ندارد"
    )

    comment_style_notes = str(
        cfg.get(
            "youtube_comment_style_notes",
            (
                "کامنت باید کاملاً متناسب با موضوع همان ویدیو باشد، "
                "هر بار ساختار متفاوتی داشته باشد و از جمله‌های ثابت استفاده نکند."
            ),
        )
    ).strip()

    fragrance_cfg = cfg.get("fragrance_content") or {}
    fragrance_prompt_notes = str(
        fragrance_cfg.get("prompt_notes", "")
    ).strip()

    prompt = f"""تو برای یک کانال فارسی، سناریوی شورت می‌نویسی.
متن باید دقیقاً شبیه حرف‌زدن طبیعی یک آدم ایرانی با دوستش باشد؛
نه مقاله، نه کتاب درسی، نه اخبار و نه گویندگی رسمی.

نیچ کانال: {cfg['niche']}
سبک گفتار درخواستی: {spoken_dialect}
{cfg.get('extra_style_notes', '')}
{spoken_notes}

قواعد اختصاصی محتوای مذهبی:
{religious_prompt_notes}

موضوع‌ها و نام‌های مذهبیِ خارج از محدوده این کانال:
{excluded_religious_text}

قواعد کامنت یوتیوب:
{comment_style_notes}

قواعد اختصاصی موضوع‌های عطر و ادکلن:
{fragrance_prompt_notes}
{hint}

خروجی فقط و فقط JSON با دقیقاً این ساختار باشد:
{{
  "title": "عنوان جذاب فارسی، حداکثر ۸۵ کاراکتر",
  "caption": "کپشن فارسی ۱ تا ۲ جمله",
  "comment": "یک سؤال کوتاه و طبیعی، مخصوص موضوع همین ویدیو، برای کامنت یوتیوب",
  "hashtags": [
    "#هشتگ_موضوعی_اول",
    "#هشتگ_موضوعی_دوم",
    "#هشتگ_موضوعی_سوم",
    "#هشتگ_موضوعی_چهارم",
    "#هشتگ_موضوعی_پنجم"
  ],
  "scenes": [
    {{
      "narration": "متن گفتاری و خیلی خودمانی این صحنه",
      "keywords": "2-4 english words for stock video"
    }}
  ]
}}

قوانین قطعی:
- دقیقاً {n} صحنه تولید کن.
- جمله اول باید فوری کنجکاوی ایجاد کند؛ بدون سلام و مقدمه.
- هر narration یک یا دو جمله کوتاه و حداکثر ۲۲ کلمه باشد.
- فارسی گفتاری امروزی بنویس؛ مثل تعریف‌کردن یک داستان برای دوست نزدیک.
- واژه‌ها و صرف فعل‌ها را با سبک گفتار درخواستی هماهنگ کن،
  اما متن باید برای همه فارسی‌زبان‌ها قابل‌فهم بماند.
- لهجه را با غلط‌نویسی افراطی، کشیدن حروف یا تکیه‌کلام کلیشه‌ای تقلید نکن.
- شکل‌های طبیعی گفتاری مجازند، مثل:
  «می‌دونی»، «فکرشو بکن»، «این‌جوری»، «اون موقع»،
  «می‌ساختن»، «می‌گفتن»، «می‌شه»، «هستن»، «رو».
- در کل ویدیو، عبارت‌هایی مثل «می‌دونی»، «ببین» یا
  «جالبه که» را هرکدام بیشتر از یک‌بار استفاده نکن.
- لحن خودمانی باشد، ولی لوس، کوچه‌بازاری یا پر از تکیه‌کلام نباشد.
- از این لحن‌ها و کلمات رسمی استفاده نکن:
  «می‌باشد»، «می‌گردد»، «گردید»، «لذا»،
  «موجب»، «واقع شده است»، «به شمار می‌رود».
- به‌جای «این بنا در دوره صفوی احداث گردید» بنویس:
  «این بنا رو زمان صفوی ساختن.»
- به‌جای «این موضوع موجب کاهش دما می‌شد» بنویس:
  «همین کار دما رو پایین می‌آورد.»
- از ویرگول، نقطه و علامت سؤال برای مکث طبیعی استفاده کن.
- عددها را با حروف فارسی بنویس، نه رقم.
- مخفف و واژه خارجی را طوری بنویس که فارسی‌زبان درست تلفظش کند.
- هیچ ایموجی، ستاره یا هشتگ داخل narration نباشد.
- keywords انگلیسی و مناسب ویدیوی استوک باشد.
- صحنه آخر با یک سؤال طبیعی یا جمله باز تمام شود.
- مقدار comment باید مخصوص موضوع همین ویدیو باشد، نه یک متن عمومی و تکراری.
- comment باید یک سؤال کوتاه، طبیعی، محترمانه و مرتبط با نکته اصلی ویدیو باشد.
- comment حداکثر دو جمله و حداکثر ۱۸۰ کاراکتر باشد.
- پایان comment باید در هر ویدیو متفاوت و متناسب با موضوع باشد.
- از جمله‌های ثابت و تکراری مثل
  «نظرتون رو توی کامنتا بگید»،
  «موافقید یا مخالفید؟»
  و «شما چی فکر می‌کنید؟»
  به‌صورت همیشگی استفاده نکن.
- برای موضوع‌های آموزشی، درباره تجربه، اشتباه رایج یا کاربرد مهارت سؤال کن.
- برای موضوع‌های تاریخی، درباره برداشت مخاطب یا بخش جالب داستان سؤال کن.
- برای موضوع‌های مذهبی، سؤال تأملی و کاربردی برای زندگی روزمره بنویس؛
  نه سؤال فرقه‌ای، جدلی یا تحریک‌آمیز.
- در comment از هشتگ، لینک، منبع کلیپ و درخواست سابسکرایب استفاده نکن.
- درباره موضوع‌ها و نام‌های خارج از محدوده مذهبی کانال چیزی تولید نکن.

قوانین hashtags:
- دقیقاً {dynamic_hashtag_count} هشتگ موضوعی تولید کن.
- هشتگ‌ها باید مستقیماً مربوط به موضوع همین ویدیو باشند.
- هشتگ‌های ثابت زیر را دوباره تولید نکن:
  {fixed_hashtags_text}
- هشتگ عمومی و نامرتبط فقط برای افزایش بازدید تولید نکن.
- داخل هر هشتگ فاصله نگذار و برای چند کلمه از زیرخط استفاده کن.
- علامت # باید ابتدای تمام هشتگ‌ها باشد.
- هشتگ‌ها باید کوتاه، قابل‌جست‌وجو و طبیعی باشند.
- برای موضوع مذهبی، نام همان شخصیت، کتاب، مناسبت یا مفهوم را استفاده کن.
- برای خیاطی، نوع آموزش، ابزار یا تکنیک دوخت را استفاده کن.
- برای رقص عربی، سبک، حرکت یا جنبه آموزشی را استفاده کن.
- برای موضوع زنان، حوزه فعالیت یا نام زن مورد بحث را استفاده کن.
- برای موضوع عطر، دقیقاً از جنسیت یا کاربرد، فصل، خانواده بویایی،
  ماندگاری، پخش بو یا موقعیت استفاده همان ویدیو هشتگ بساز."""
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "response_mime_type": "application/json",
            "temperature": 1.05,
            "topP": 0.95,
        },
    }

    last_err = ""
    for attempt in range(5):
        response = requests.post(url, json=body, timeout=90)
        if response.status_code != 200:
            last_err = response.text[:500]
            time.sleep(5)
            continue

        try:
            raw_text = (
                response.json()["candidates"][0]["content"]["parts"][0]["text"]
            )
            raw_text = re.sub(
                r"^```(json)?|```$",
                "",
                raw_text.strip(),
                flags=re.M,
            )
            data = json.loads(raw_text)

            scenes = data.get("scenes", [])
            assert data.get("title")
            assert isinstance(scenes, list)
            assert len(scenes) == n
            assert all(
                isinstance(scene, dict)
                and scene.get("narration")
                and scene.get("keywords")
                for scene in scenes
            )

            raw_hashtags = data.get("hashtags", [])
            if not isinstance(raw_hashtags, list):
                raise ValueError(
                    "hashtags باید یک فهرست باشد"
                )

            fixed_hashtag_keys = {
                item.casefold()
                for item in fixed_hashtags
            }

            dynamic_hashtags = []
            dynamic_seen = set()

            for item in raw_hashtags:
                hashtag = _normalize_hashtag(item)
                if not hashtag:
                    continue

                key = hashtag.casefold()

                if key in fixed_hashtag_keys:
                    continue

                if key in dynamic_seen:
                    continue

                dynamic_seen.add(key)
                dynamic_hashtags.append(hashtag)

            if len(dynamic_hashtags) < dynamic_hashtag_count:
                fallback_hashtags = _fallback_dynamic_hashtags(
                    data,
                    cfg,
                    dynamic_hashtag_count,
                    fixed_hashtag_keys | dynamic_seen,
                )
                for hashtag in fallback_hashtags:
                    key = hashtag.casefold()
                    if key not in fixed_hashtag_keys and key not in dynamic_seen:
                        dynamic_seen.add(key)
                        dynamic_hashtags.append(hashtag)
                    if len(dynamic_hashtags) >= dynamic_hashtag_count:
                        break

            if len(dynamic_hashtags) < dynamic_hashtag_count:
                raise ValueError(
                    "Gemini و fallback نتوانستند دقیقاً "
                    f"{dynamic_hashtag_count} هشتگ موضوعی غیرتکراری بسازند؛ "
                    f"تعداد معتبر فعلی: {len(dynamic_hashtags)}"
                )

            data["hashtags"] = dynamic_hashtags[:dynamic_hashtag_count]
            log(
                "🏷️ هشتگ‌های موضوعی: "
                + " ".join(data["hashtags"])
            )

            comment = _clean_generated_comment(data.get("comment"))
            generic_comments = {
                "نظرتون رو توی کامنتا بگید؛ موافقید یا مخالفید؟",
                "نظرتون رو توی کامنتا بگید، موافقید یا مخالفید؟",
                "شما چی فکر می‌کنید؟",
                "موافقید یا مخالفید؟",
            }
            if (
                not comment
                or comment in generic_comments
                or len(comment) < 12
            ):
                comment = _fallback_youtube_comment(data, cfg)

            if not comment.endswith(("؟", "?", ".")):
                comment += "؟"

            data["comment"] = comment[:450]

            excluded_hits = _find_excluded_religious_terms(
                data,
                excluded_religious_topics,
            )
            if excluded_hits:
                last_err = (
                    "خروجی وارد موضوع مذهبی خارج از محدوده شد: "
                    + "، ".join(excluded_hits)
                )
                log(f"🔁 بازنویسی به‌خاطر موضوع خارج از محدوده: {last_err}")
                time.sleep(2)
                continue

            formal_hits = _formal_terms_in_script(data)
            if formal_hits:
                last_err = (
                    "متن هنوز رسمی بود: "
                    + "، ".join(formal_hits)
                )
                log(f"🔁 بازنویسی به‌خاطر لحن رسمی: {last_err}")
                time.sleep(2)
                continue

            log(f"📝 سناریو آماده شد: {data['title']}")
            return data

        except Exception as exc:
            last_err = str(exc)
            time.sleep(3)

    die(f"Gemini جواب گفتاری و معتبر نداد: {last_err}")


_SPOKEN_REPLACEMENTS = (
    (r"می\s*‌?\s*باشد", "هست"),
    (r"می\s*‌?\s*باشند", "هستن"),
    (r"می\s*‌?\s*گردد", "می‌شه"),
    (r"می\s*‌?\s*شود", "می‌شه"),
    (r"می\s*‌?\s*تواند", "می‌تونه"),
    (r"می\s*‌?\s*توانست", "می‌تونست"),
    (r"می\s*‌?\s*کنند", "می‌کنن"),
    (r"می\s*‌?\s*کردند", "می‌کردن"),
    (r"خواهد شد", "می‌شه"),
    (r"وجود دارد", "هست"),
    (r"وجود داشت", "بود"),
    (r"به همین دلیل", "برای همین"),
    (r"بنابراین", "پس"),
    (r"زیرا", "چون"),
    (r"اما", "ولی"),
    (r"آن‌ها", "اونا"),
    (r"آنها", "اونا"),
    (r"این‌ها", "اینا"),
    (r"آن موقع", "اون موقع"),
    (r"هستند", "هستن"),
    (r"بودند", "بودن"),
    (r"گردید", "شد"),
)


def clean_for_tts(value):
    """متن را برای تلفظ طبیعی‌تر و گفتاری‌تر آماده می‌کند."""
    value = str(value or "")
    value = re.sub(r"[\U00010000-\U0010FFFF]", "", value)
    value = re.sub(r"[*_#`\"«»]", "", value)

    for pattern, replacement in _SPOKEN_REPLACEMENTS:
        value = re.sub(pattern, replacement, value)

    # «را» در گفتار معمولاً طبیعی‌تر است که «رو» خوانده شود.
    value = re.sub(r"(?<!\w)را(?!\w)", "رو", value)

    # فاصله‌گذاری علائم نگارشی برای مکث طبیعی‌تر TTS
    value = re.sub(r"\s+([،؛.!؟?])", r"\1", value)
    value = re.sub(r"([،؛.!؟?])(?=\S)", r"\1 ", value)
    value = re.sub(r"[؛]+", "،", value)
    value = re.sub(r"\.{3,}", "…", value)
    value = re.sub(r"\s+", " ", value).strip()

    if value and value[-1] not in ".!؟?…":
        value += "."

    return value


# =====================================================
# ۲) صداگذاری با edge-tts + زمان‌بندی کلمه‌به‌کلمه
# =====================================================
def choose_voice_profile(cfg):
    """یک پروفایل صدا برای کل ویدیو انتخاب می‌کند.

    روی GitHub Actions، انتخاب براساس GITHUB_RUN_NUMBER می‌چرخد تا
    اجراهای پشت‌سرهم تا حد ممکن یک پروفایل یکسان نداشته باشند.
    """
    raw_profiles = cfg.get("voice_profiles") or []

    # سازگاری با config قدیمی
    if not raw_profiles:
        raw_profiles = [{
            "label": "legacy",
            "voice": cfg.get("voice", "fa-IR-FaridNeural"),
            "rate": cfg.get("rate", "+8%"),
            "pitch": cfg.get("pitch", "+0Hz"),
            "volume": cfg.get("volume", "+0%"),
        }]

    profiles = []
    for index, item in enumerate(raw_profiles, start=1):
        if isinstance(item, str):
            item = {"voice": item}
        if not isinstance(item, dict):
            log(f"⚠️ پروفایل صدای شماره {index} نامعتبر است و رد شد")
            continue

        voice = str(item.get("voice", "")).strip()
        if not voice:
            log(f"⚠️ پروفایل صدای شماره {index} voice ندارد و رد شد")
            continue

        profiles.append({
            "label": str(item.get("label") or f"profile-{index}"),
            "gender": str(item.get("gender") or "unknown"),
            "voice": voice,
            "rate": str(item.get("rate") or "+0%"),
            "pitch": str(item.get("pitch") or "+0Hz"),
            "volume": str(item.get("volume") or "+0%"),
        })

    if not profiles:
        die("هیچ پروفایل صدای معتبری در config.yaml وجود ندارد")

    forced = env("VOICE_PROFILE")
    if forced:
        for profile in profiles:
            if forced in {profile["label"], profile["voice"]}:
                selected = profile
                break
        else:
            die(
                f"VOICE_PROFILE={forced} در voice_profiles پیدا نشد"
            )
    else:
        run_number = env("GITHUB_RUN_NUMBER")
        if run_number and run_number.isdigit():
            selected = profiles[(int(run_number) - 1) % len(profiles)]
        else:
            selected = random.choice(profiles)

    log(
        "🎙️ صدای این ویدیو: "
        f"{selected['label']} | {selected['voice']} | "
        f"rate={selected['rate']} | pitch={selected['pitch']}"
    )
    return selected


async def tts_scene(text, profile, mp3_path):
    com = edge_tts.Communicate(
        text,
        profile["voice"],
        rate=profile["rate"],
        pitch=profile["pitch"],
        volume=profile["volume"],
        boundary="WordBoundary",
    )
    words = []
    with open(mp3_path, "wb") as f:
        async for ch in com.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] == "WordBoundary":
                words.append(
                    (ch["offset"] / 1e7, ch["duration"] / 1e7, ch["text"])
                )
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
# ۴) کلیپ استوک از Pexels + Pixabay
# =====================================================
STOCK_CACHE_TTL = 24 * 60 * 60


def _load_stock_cache():
    if not STOCK_CACHE.exists():
        return {}

    try:
        data = json.loads(STOCK_CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_stock_cache(cache):
    CACHE_DIR.mkdir(exist_ok=True)
    STOCK_CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )


def _cache_get(source, query):
    cache = _load_stock_cache()
    key = f"{source}:{query.strip().lower()}"
    item = cache.get(key)

    if not isinstance(item, dict):
        return None

    saved_at = float(item.get("saved_at", 0))
    if time.time() - saved_at > STOCK_CACHE_TTL:
        return None

    results = item.get("results")
    return results if isinstance(results, list) else None


def _cache_put(source, query, results):
    cache = _load_stock_cache()
    key = f"{source}:{query.strip().lower()}"
    cache[key] = {
        "saved_at": time.time(),
        "results": results,
    }

    # کش قدیمی و خراب را جمع کن تا فایل بی‌نهایت بزرگ نشود.
    now = time.time()
    cache = {
        k: v
        for k, v in cache.items()
        if isinstance(v, dict)
        and now - float(v.get("saved_at", 0)) <= STOCK_CACHE_TTL
    }
    _save_stock_cache(cache)


def _download_video(url, dest):
    headers = {"User-Agent": "video-factory/2.0"}
    with requests.get(url, headers=headers, stream=True, timeout=180) as dl:
        dl.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in dl.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)

    if not dest.exists() or dest.stat().st_size < 100_000:
        raise RuntimeError("فایل ویدیویی دانلودشده معتبر نیست")


def _pick_best_file(files):
    files = [
        f for f in files
        if f.get("url")
        and int(f.get("width") or 0) > 0
        and int(f.get("height") or 0) >= 720
    ]
    if not files:
        return None

    def score(item):
        width = int(item.get("width") or 0)
        height = int(item.get("height") or 0)
        is_vertical = height > width
        target_height = 1920 if is_vertical else 1080
        vertical_bonus = 10_000 if is_vertical else 0
        # نزدیک‌ترین نسخه به Full HD را بردار؛ دانلود 4K فقط حجم را زیاد می‌کند.
        size_score = -abs(height - target_height)
        return vertical_bonus + size_score

    return max(files, key=score)


def search_pexels(query, used_ids):
    key = env("PEXELS_API_KEY")
    if not key:
        log("⚠️ PEXELS_API_KEY موجود نیست؛ Pexels رد شد")
        return []

    cached = _cache_get("pexels", query)
    if cached is not None:
        return [x for x in cached if x.get("uid") not in used_ids]

    r = requests.get(
        "https://api.pexels.com/v1/videos/search",
        headers={"Authorization": key},
        params={
            "query": query,
            "orientation": "portrait",
            "per_page": 15,
        },
        timeout=60,
    )

    if r.status_code != 200:
        log(f"⚠️ خطای Pexels برای «{query}»: {r.status_code} {r.text[:180]}")
        return []

    results = []
    for video in r.json().get("videos", []):
        uid = f"pexels:{video.get('id')}"
        files = [
            {
                "url": f.get("link"),
                "width": f.get("width"),
                "height": f.get("height"),
            }
            for f in video.get("video_files", [])
            if f.get("file_type") == "video/mp4"
        ]
        selected_file = _pick_best_file(files)
        if not selected_file:
            continue

        user = video.get("user") or {}
        results.append({
            "source": "pexels",
            "uid": uid,
            "id": video.get("id"),
            "download_url": selected_file["url"],
            "page_url": video.get("url", ""),
            "author": user.get("name", ""),
            "width": selected_file.get("width", 0),
            "height": selected_file.get("height", 0),
            "duration": video.get("duration", 0),
        })

    _cache_put("pexels", query, results)
    return [x for x in results if x.get("uid") not in used_ids]


def search_pixabay(query, used_ids):
    key = env("PIXABAY_API_KEY")
    if not key:
        log("⚠️ PIXABAY_API_KEY موجود نیست؛ Pixabay رد شد")
        return []

    cached = _cache_get("pixabay", query)
    if cached is not None:
        return [x for x in cached if x.get("uid") not in used_ids]

    r = requests.get(
        "https://pixabay.com/api/videos/",
        params={
            "key": key,
            "q": query[:100],
            "video_type": "film",
            "safesearch": "true",
            "order": "popular",
            "per_page": 20,
        },
        timeout=60,
    )

    if r.status_code != 200:
        log(f"⚠️ خطای Pixabay برای «{query}»: {r.status_code} {r.text[:180]}")
        return []

    results = []
    for video in r.json().get("hits", []):
        uid = f"pixabay:{video.get('id')}"
        renditions = []
        for size_name in ("medium", "large", "small", "tiny"):
            item = (video.get("videos") or {}).get(size_name) or {}
            if item.get("url"):
                renditions.append({
                    "url": item.get("url"),
                    "width": item.get("width"),
                    "height": item.get("height"),
                })

        selected_file = _pick_best_file(renditions)
        if not selected_file:
            continue

        results.append({
            "source": "pixabay",
            "uid": uid,
            "id": video.get("id"),
            "download_url": selected_file["url"],
            "page_url": video.get("pageURL", ""),
            "author": video.get("user", ""),
            "width": selected_file.get("width", 0),
            "height": selected_file.get("height", 0),
            "duration": video.get("duration", 0),
        })

    _cache_put("pixabay", query, results)
    return [x for x in results if x.get("uid") not in used_ids]


def stock_clip(keywords, dest, used_ids, source_counts):
    queries = [
        keywords,
        "cinematic nature",
        "abstract dark background",
    ]

    # در هر ویدیو منابع را متعادل نگه می‌داریم: تقریباً ۳/۲ یا ۲/۳.
    if source_counts["pexels"] < source_counts["pixabay"]:
        providers = [search_pexels, search_pixabay]
    elif source_counts["pixabay"] < source_counts["pexels"]:
        providers = [search_pixabay, search_pexels]
    else:
        providers = [search_pexels, search_pixabay]
        random.shuffle(providers)

    for query in queries:
        for provider in providers:
            candidates = provider(query, used_ids)
            if not candidates:
                continue

            # از میان نتایج خوب، فقط مورد اول را دائم انتخاب نکن.
            top = candidates[: min(6, len(candidates))]
            selected = random.choice(top)

            try:
                _download_video(selected["download_url"], dest)
            except (requests.RequestException, OSError, RuntimeError) as exc:
                log(
                    f"⚠️ دانلود {selected['source']}:{selected['id']} شکست خورد: {exc}"
                )
                used_ids.add(selected["uid"])
                continue

            used_ids.add(selected["uid"])
            source_counts[selected["source"]] += 1
            log(
                f"🎞️ کلیپ {selected['source']}: {query} "
                f"(id {selected['id']}, {selected['width']}x{selected['height']})"
            )
            return selected

    die(f"در Pexels و Pixabay هیچ کلیپی برای «{keywords}» پیدا نشد")


# =====================================================
# ۵) مونتاژ با FFmpeg
# =====================================================
def build_video(cfg, script):
    WORK.mkdir(exist_ok=True)
    OUT.mkdir(exist_ok=True)

    voice_profile = choose_voice_profile(cfg)
    script["voice_profile"] = voice_profile

    all_words, scene_files, audio_files = [], [], []
    used_ids = set()
    source_counts = {"pexels": 0, "pixabay": 0}
    stock_sources = []
    t_offset = 0.0

    for i, scene in enumerate(script["scenes"]):
        text = clean_for_tts(scene["narration"])
        if not text:
            continue
        mp3 = WORK / f"voice_{i}.mp3"
        words = asyncio.run(tts_scene(text, voice_profile, mp3))
        dur = ffprobe_duration(mp3) + 0.25

        raw = WORK / f"stock_{i}.mp4"
        selected_stock = stock_clip(
            scene.get("keywords", "cinematic"),
            raw,
            used_ids,
            source_counts,
        )
        stock_sources.append(selected_stock)

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

    burn_subtitles = bool(cfg.get("burn_subtitles", False))

    fname = f"video_{datetime.now(timezone.utc):%Y%m%d_%H%M}.mp4"
    final = OUT / fname

    if burn_subtitles:
        subs = WORK / "subs.ass"
        build_ass(
            all_words,
            subs,
            max_words=int(cfg.get("caption_words", 3)),
        )

        run([
            "ffmpeg", "-y",
            "-i", str(WORK / "silent.mp4"),
            "-i", str(WORK / "voice.m4a"),
            "-vf",
            f"subtitles={subs.as_posix()}:fontsdir={FONTS.as_posix()}",
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "22",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            "-shortest",
            str(final),
        ])
        log("📝 زیرنویس فارسی روی ویدیو چسبانده شد")
    else:
        # حالت پیش‌فرض: هیچ زیرنویسی روی تصویر قرار نمی‌گیرد.
        run([
            "ffmpeg", "-y",
            "-i", str(WORK / "silent.mp4"),
            "-i", str(WORK / "voice.m4a"),
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac",
            "-b:a", "160k",
            "-movflags", "+faststart",
            "-shortest",
            str(final),
        ])
        log("🚫 زیرنویس روی ویدیو غیرفعال است")

    log(f"✅ ویدیو ساخته شد: {fname} ({t_offset:.0f} ثانیه، {final.stat().st_size // 1024} KB)")
    if t_offset > 175:
        log("⚠️ ویدیو از ۳ دقیقه بلندتر است؛ شاید Short حساب نشود. scenes_count را کم کن.")
    script["stock_sources"] = stock_sources
    log(
        "📊 منابع این ویدیو: "
        f"Pexels={source_counts['pexels']} | Pixabay={source_counts['pixabay']}"
    )
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
def _youtube_channel_id(token):
    response = requests.get(
        "https://www.googleapis.com/youtube/v3/channels",
        params={"part": "id", "mine": "true", "maxResults": 1},
        headers={"Authorization": f"Bearer {token}"},
        timeout=60,
    )

    if response.status_code != 200:
        log(
            "⚠️ شناسه کانال برای ارسال کامنت دریافت نشد: "
            f"{response.status_code} {response.text[:300]}"
        )
        return None

    items = response.json().get("items", [])
    if not items:
        log("⚠️ هیچ کانال یوتیوبی برای این حساب پیدا نشد")
        return None

    return items[0].get("id")


def youtube_post_comment(cfg, token, video_id, comment_text):
    """یک کامنت سطح اول، متناسب با موضوع ویدیو، منتشر می‌کند."""
    if not cfg.get("enable_youtube_comment", True):
        log("⏭️ کامنت خودکار یوتیوب در config خاموش است")
        return False

    comment_text = re.sub(r"\s+", " ", str(comment_text or "")).strip()
    if not comment_text:
        log("⚠️ متن کامنت خالی است؛ ارسال کامنت رد شد")
        return False

    channel_id = _youtube_channel_id(token)
    if not channel_id:
        return False

    body = {
        "snippet": {
            "channelId": channel_id,
            "videoId": video_id,
            "topLevelComment": {
                "snippet": {
                    "textOriginal": comment_text[:9000],
                }
            },
        }
    }

    # گاهی ویدیو بلافاصله بعد از آپلود برای کامنت آماده نیست.
    for attempt in range(3):
        response = requests.post(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params={"part": "snippet"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json=body,
            timeout=60,
        )

        if response.status_code in (200, 201):
            log(f"💬 کامنت یوتیوب منتشر شد: {comment_text}")
            return True

        error_text = response.text[:500]

        # مشکل مجوز با صبرکردن حل نمی‌شود.
        if response.status_code in (401, 403):
            log(
                "⚠️ ارسال کامنت یوتیوب انجام نشد. "
                "Refresh Token باید مجوز youtube.force-ssl داشته باشد. "
                f"پاسخ API: {response.status_code} {error_text}"
            )
            return False

        if attempt < 2:
            log(
                "⏳ ویدیو هنوز برای کامنت آماده نیست؛ "
                f"تلاش دوباره {attempt + 2}/3"
            )
            time.sleep(12)

    log(
        "⚠️ کامنت یوتیوب بعد از سه تلاش منتشر نشد: "
        f"{response.status_code} {error_text}"
    )
    return False


def _youtube_description(cfg, script):
    """توضیحات ویدیو؛ منابع کلیپ فقط در صورت فعال‌بودن config افزوده می‌شوند."""
    parts = []

    caption = str(script.get("caption", "")).strip()
    if caption:
        parts.append(caption)

    hashtags = " ".join(
        _video_hashtags(cfg, script)
    )
    if hashtags:
        parts.append(hashtags)

    if cfg.get("include_stock_sources_in_description", False):
        stock_lines = [
            (
                f"- {item.get('source', '').title()}: "
                f"{item.get('author') or 'Unknown'} "
                f"{item.get('page_url', '')}"
            ).strip()
            for item in script.get("stock_sources", [])
        ]
        if stock_lines:
            parts.append("منابع کلیپ‌ها:\n" + "\n".join(stock_lines))

    return "\n\n".join(parts)


def youtube_upload(cfg, script, fname):
    cid = env("YT_CLIENT_ID")
    csec = env("YT_CLIENT_SECRET")
    rtok = env("YT_REFRESH_TOKEN")

    if not (cid and csec and rtok):
        log(
            "⏭️ سیکرت‌های یوتیوب تنظیم نشده‌اند — "
            "از یوتیوب رد شدیم (مرحله ۵ راهنما)"
        )
        return

    if not cfg.get("enable_youtube", True):
        log("⏭️ یوتیوب در config خاموش است")
        return

    response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "client_id": cid,
            "client_secret": csec,
            "refresh_token": rtok,
            "grant_type": "refresh_token",
        },
        timeout=60,
    )

    if response.status_code != 200:
        die(
            "گرفتن توکن یوتیوب شکست خورد "
            "(refresh token را دوباره بگیر — مرحله ۵): "
            f"{response.text[:300]}"
        )

    token = response.json()["access_token"]

    title = script["title"].strip()[:92]
    if "#shorts" not in title.lower():
        title += " #Shorts"

    tags = [
        hashtag.lstrip("#")
        for hashtag in _video_hashtags(cfg, script)
    ][:15]

    body = {
        "snippet": {
            "title": title,
            "description": _youtube_description(cfg, script),
            "tags": tags,
            "categoryId": str(cfg.get("category_id", 27)),
        },
        "status": {
            "privacyStatus": cfg.get("privacy_status", "public"),
            "selfDeclaredMadeForKids": False,
        },
    }

    init = requests.post(
        "https://www.googleapis.com/upload/youtube/v3/videos",
        params={
            "uploadType": "resumable",
            "part": "snippet,status",
        },
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json; charset=UTF-8",
        },
        json=body,
        timeout=60,
    )

    if init.status_code not in (200, 201):
        die(
            "شروع آپلود یوتیوب شکست خورد: "
            f"{init.status_code} {init.text[:400]}"
        )

    upload_url = init.headers["Location"]

    with open(OUT / fname, "rb") as video_file:
        upload = requests.put(
            upload_url,
            data=video_file,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "video/mp4",
            },
            timeout=600,
        )

    if upload.status_code not in (200, 201):
        die(
            "آپلود یوتیوب شکست خورد: "
            f"{upload.status_code} {upload.text[:400]}"
        )

    video_id = upload.json().get("id")
    log(
        "▶️ آپلود یوتیوب موفق: "
        f"https://youtube.com/watch?v={video_id}"
    )
    log(
        "ℹ️ اگر ویدیو private و قفل‌شده است، "
        "پروژه‌ات هنوز Audit گوگل را نگرفته (مرحله ۵ راهنما)."
    )

    if video_id:
        youtube_post_comment(
            cfg,
            token,
            video_id,
            script.get("comment"),
        )


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

    caption = (
        meta["caption"]
        + "\n\n"
        + " ".join(_video_hashtags(cfg, meta))
    )
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
            "comment": script.get("comment", ""),
            "hashtags": script.get("hashtags", []),
            "made_at": datetime.now(timezone.utc).isoformat(),
            "stock_sources": script.get("stock_sources", []),
            "voice_profile": script.get("voice_profile", {}),
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
