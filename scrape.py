#!/usr/bin/env python3
"""BOYS AND MEN・でらボ公式イベントカレンダー取得スクリプト(改良版)。"""
import hashlib
import json
import os
import re
import sys
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode, urljoin, urldefrag, urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============================================================
# 基本設定
# ============================================================

JST = timezone(timedelta(hours=9))
TODAY = datetime.now(JST).date()

START = date(
    TODAY.year - 1, TODAY.month,
    min(TODAY.day, monthrange(TODAY.year - 1, TODAY.month)[1])
)
END = date(
    TODAY.year + 1, TODAY.month,
    min(TODAY.day, monthrange(TODAY.year + 1, TODAY.month)[1])
)

BOYS_BASE = "https://boysandmen.jp"
BOYS_CALENDAR = BOYS_BASE + "/vertical_calendar"

DERA_BASE = "https://dxboys.bitfan.id"
DERA_SCHEDULE = DERA_BASE + "/schedules/menu/184780"
DERA_API_BASE = "https://user-api.bitfan.id"

MEMBERS = ["辻本達規", "田村侑久", "本田剛文", "平松賢人", "吉原雅斗"]

# ★ 自分の連絡先(メールアドレスやGitHubのURLなど)に書き換えてください
CONTACT = "X:@0hnawaba_"

HEADERS = {
    "User-Agent": f"BoysAndMenCalendarBot/1.0 (personal use; contact: {CONTACT})",
    "Accept-Language": "ja,en-US;q=0.8,en;q=0.6",
}

# リクエスト間隔(秒)。サイトへの負荷を抑えるため、短くしすぎないこと
REQUEST_INTERVAL = 0.8
TIMEOUT = 25

# 詳細ページとして取得を許可するホスト
ALLOWED_DETAIL_HOSTS = {
    "boysandmen.jp",
    "www.boysandmen.jp",
    "dxboys.bitfan.id",
}

SESSION = requests.Session()
SESSION.headers.update(HEADERS)
_retry = Retry(
    total=3,
    backoff_factor=1.5,
    status_forcelist=(429, 500, 502, 503, 504),
    allowed_methods=("GET",),
    respect_retry_after_header=True,
)
SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
SESSION.mount("http://", HTTPAdapter(max_retries=_retry))

_LAST_REQUEST_AT = 0.0
DETAIL_CACHE = {}

EXCLUDED_PATHS = (
    "/about", "/help", "/contact", "/faq", "/privacy",
    "/terms", "/law", "/sitemap", "/login", "/signup",
    "/account", "/cart", "/mypage", "/company",
)

SKIP_TITLES = [
    "F.ENT IDについて", "お客さまへのお願い",
    "お支払いについて", "よくあるご質問", "よくある質問",
    "特商法に基づく表示", "お問い合わせ", "お問合せ",
    "会社概要", "プライバシーポリシー", "利用規約",
    "サイトマップ", "ログイン", "ログアウト", "会員登録",
    "Powered by", "前月へ", "次月へ", "HOME",
    "トップページ", "SCHEDULE", "NEWS",
]

NOTE_KEYWORDS = (
    "撮影指定ポーズ", "指定ポーズ", "衣装",
    "ドレスコード", "服装", "注意事項",
)

PLACE_LABELS = ("会場", "開催場所", "住所", "所在地")

# ============================================================
# 共通処理
# ============================================================

def normalize(text):
    return " ".join((text or "").replace("\xa0", " ").split())


def throttled_get(url, **kwargs):
    """前回のリクエストから一定時間あけてGETする。"""
    global _LAST_REQUEST_AT
    wait = REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST_AT)
    if wait > 0:
        time.sleep(wait)
    kwargs.setdefault("timeout", TIMEOUT)
    try:
        return SESSION.get(url, **kwargs)
    finally:
        _LAST_REQUEST_AT = time.monotonic()


def is_allowed_detail_url(url):
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc.lower() in ALLOWED_DETAIL_HOSTS
    )


def get_soup(url):
    response = throttled_get(url)
    response.raise_for_status()
    response.encoding = response.apparent_encoding or response.encoding
    return BeautifulSoup(response.text, "html.parser")


def parse_full_date(text):
    """年を含む日付のみを取得。年の推測はしない。"""
    text = normalize(text)
    match = re.search(
        r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?",
        text,
    )
    if not match:
        return None
    try:
        return date(
            int(match.group(1)),
            int(match.group(2)),
            int(match.group(3)),
        )
    except ValueError:
        return None


def member_for(title, detail_text=""):
    matches = [member for member in MEMBERS if member in title]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        return "その他"

    matches = [member for member in MEMBERS if member in detail_text]
    if len(matches) == 1:
        return matches[0]
    return "グループ全体"


def classify(text):
    text = normalize(text)
    if any(x in text for x in ["特典会", "お渡し会", "サイン会", "リミスタ"]):
        return "特典会"
    if any(x in text for x in ["ラジオ", "RADIO", "FM "]):
        return "ラジオ"
    if any(x in text for x in ["テレビ", "TV出演", "NHK", "東海テレビ", "TBS"]):
        return "テレビ"
    if any(x in text for x in ["舞台", "ミュージカル", "公演"]):
        return "舞台"
    if any(x in text for x in ["ライブ", "コンサート", "単独公演"]):
        return "ライブ"
    if any(x in text for x in ["配信", "YouTube"]):
        return "配信"
    if any(x in text for x in ["リリース", "発売記念"]):
        return "リリース"
    return "イベント"


def should_skip_title(title):
    title = normalize(title)
    if len(title) < 4 or title.startswith("▼"):
        return True
    return any(word.casefold() in title.casefold() for word in SKIP_TITLES)


def is_excluded_boys_url(url):
    parsed = urlparse(url)
    if parsed.netloc.lower() not in {"boysandmen.jp", "www.boysandmen.jp"}:
        return True

    path = parsed.path.rstrip("/") or "/"
    if path == "/vertical_calendar" or path.startswith("/vertical_calendar/"):
        return True

    return any(
        path == prefix or path.startswith(prefix + "/")
        for prefix in EXCLUDED_PATHS
    )


def make_event(source, event_date, title, url, detail_text=""):
    title = normalize(title)
    if should_skip_title(title):
        return None
    if not START <= event_date <= END:
        return None
    if source == "boys" and is_excluded_boys_url(url):
        return None

    raw_id = f"{source}|{event_date.isoformat()}|{title}|{url}"
    event_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
