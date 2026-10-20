#!/usr/bin/env python3
"""BOYS AND MEN・でらボ公式イベントカレンダー取得スクリプト(改良版2)。"""
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

# APIのJSONの中で、会場が入っていそうな項目名
PLACE_KEYS = ("place", "venue", "location", "address", "spot", "会場")

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

    return {
        "id": f"{source}-{event_id}",
        "date": event_date.isoformat(),
        "time": "",
        "title": title,
        "member": member_for(title, detail_text),
        "type": classify(title),
        "place": "",
        "url": url,
        "notes": "公式ページから自動取得。詳細はリンク先で確認してください。",
        "source": source,
        "origin": "official",
        "unconfirmed": False,
    }


def empty_status(url, error=None):
    """全ソース共通の戻り値の形。"""
    return {
        "ok": False,
        "count": 0,
        "ticket_periods": 0,
        "places_found": 0,
        "detail_pages_checked": 0,
        "errors": [error] if error else [],
        "url": url,
    }


# ============================================================
# 詳細ページ解析:会場・住所・チケット受付期間
# ============================================================

def clean_place(value):
    """会場欄に混入したラベルや余計な空白を整える。"""
    value = normalize(value)
    value = re.sub(
        r"^(?:会場名?|開催場所|開催会場|場所|住所|所在地|Venue|Location)\s*[：:]\s*",
        "",
        value,
        flags=re.IGNORECASE,
    )
    value = value.strip(" ：:　")
    if not value or len(value) > 180:
        return ""
    if any(word in value for word in [
        "受付期間", "当選発表", "支払期間", "販売期間",
        "お問い合わせ", "注意事項", "出演者",
    ]):
        return ""
    return value


def extract_place(soup, lines):
    """公式詳細ページに明記された会場名または住所を探す。"""
    if soup:
        # テーブル形式:左セルが「会場」、右セルが会場名
        for row in soup.select("tr"):
            cells = row.find_all(["th", "td"], recursive=False)
            if len(cells) >= 2:
                label = normalize(cells[0].get_text(" ", strip=True))
                if any(word in label for word in PLACE_LABELS):
                    value = clean_place(cells[1].get_text(" ", strip=True))
                    if value:
                        return value

        # 定義リスト形式:dt「会場」→ dd「会場名」
        for node in soup.select("dt"):
            label = normalize(node.get_text(" ", strip=True))
            if any(word in label for word in PLACE_LABELS):
                sibling = node.find_next_sibling("dd")
                if sibling:
                    value = clean_place(sibling.get_text(" ", strip=True))
                    if value:
                        return value

    labels = (
        "開催会場", "開催場所", "会場名", "会場",
        "所在地", "住所", "Venue", "Location",
    )

    for index, line in enumerate(lines):
        for label in labels:
            match = re.match(
                rf"^{re.escape(label)}\s*[：:]\s*(.+)$",
                line,
                flags=re.IGNORECASE,
            )
            if match:
                value = clean_place(match.group(1))
                if value:
                    return value

            if line.strip().casefold() == label.casefold():
                # ラベルだけの行なら次の数行から値を探す
                for following in lines[index + 1:index + 3]:
                    value = clean_place(following)
                    if value and not any(
                        word in following
                        for word in ["受付期間", "出演者", "チケット情報"]
                    ):
                        return value

    return ""


def get_detail(url):
    """詳細ページを取得して解析し、URLごとにキャッシュする。

    BeautifulSoupオブジェクトは保持せず、必要な情報だけを残す。
    """
    if url in DETAIL_CACHE:
        return DETAIL_CACHE[url]

    empty = {"lines": [], "text": "", "place": ""}

    if not is_allowed_detail_url(url):
        print(f"WARN 許可されていないURLのため取得しません: {url}", file=sys.stderr)
        DETAIL_CACHE[url] = empty
        return empty

    try:
        soup = get_soup(url)

        for tag in soup.select(
            "script, style, noscript, nav, footer, header, iframe"
        ):
            tag.decompose()

        lines = []
        seen_lines = set()
        for node in soup.select(
            "h1, h2, h3, h4, p, li, dt, dd, th, td, "
            "strong, b, time, .schedule-detail, .event-detail"
        ):
            text = normalize(node.get_text(" ", strip=True))
            if text and text not in seen_lines:
                seen_lines.add(text)
                lines.append(text)

        # ページ内の情報が通常のタグに入っていない場合の補助
        if not lines:
            lines = [
                normalize(line)
                for line in soup.get_text("\n").splitlines()
                if normalize(line)
            ]

        result = {
            "lines": lines,
            "text": "\n".join(lines),
            "place": extract_place(soup, lines),
        }
        soup.decompose()

    except Exception as exc:
        print(f"WARN 詳細ページ取得 {url}: {exc}", file=sys.stderr)
        result = empty

    DETAIL_CACHE[url] = result
    return result


DATE_PATTERN = re.compile(
    r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"
    r"|"
    r"(20\d{2})[/.\-](\d{1,2})[/.\-](\d{1,2})"
)
TIME_PATTERN = re.compile(r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)")
RANGE_PATTERN = re.compile(r"～|〜|~|−|－|–|—|-|から")
TICKET_WORDS = (
    "受付", "チケット", "抽選", "先着", "販売",
    "プレイガイド", "FC", "ファンクラブ",
    "先行", "申込", "申し込み", "申込み",
)
PAYMENT_WORDS = (
    "当選発表", "当落", "支払期間", "支払期限",
    "入金期間", "入金期限", "決済期限",
)


def find_dates(text):
    found = []
    for match in DATE_PATTERN.finditer(text):
        groups = match.groups()
        if groups[0]:
            y, m, d = int(groups[0]), int(groups[1]), int(groups[2])
        else:
            y, m, d = int(groups[3]), int(groups[4]), int(groups[5])
        try:
            found.append((match.start(), match.end(), date(y, m, d)))
        except ValueError:
            continue
    return found


def ticket_kind(text):
    text = normalize(text)

    if "FC" in text or "ファンクラブ" in text:
        prefix = "FC"
    elif "先行" in text:
        prefix = "先行"
    elif "一般" in text:
        prefix = "一般"
    else:
        prefix = ""

    if "抽選" in text:
        return f"{prefix}抽選受付" if prefix else "抽選受付"
    if "先着" in text:
        return f"{prefix}先着販売" if prefix else "先着販売"
    if "販売" in text:
        return f"{prefix}販売" if prefix else "チケット販売"
    return f"{prefix}受付" if prefix else "チケット受付"


def extract_ticket_periods(detail, source, parent_title, source_url):
    """受付期間の開始日・終了日が明記されている場合だけ期間予定を作る。"""
    lines = detail.get("lines", [])
    if not lines:
        return []

    results = []
    seen = set()

    for index in range(len(lines)):
        # 別のHTML要素に分かれている日付範囲も調べる
        for span in (1, 2, 3):
            if index + span >= len(lines):
                continue

            candidate = " ".join(lines[index:index + span + 1])
            dates = find_dates(candidate)

            if len(dates) < 2:
                continue

            for first, second in zip(dates, dates[1:]):
                _, start_end, start_date = first
                end_pos, end_end, end_date = second

                if end_date < start_date:
                    continue

                between = candidate[start_end:end_pos]
                if not RANGE_PATTERN.search(between):
                    continue

                context_start = max(0, index - 2)
                context_end = min(len(lines), index + span + 4)
                context_lines = lines[context_start:context_end]
                context = " ".join(context_lines)

                # 公演日そのものを受付期間と誤認しないため、
                # 受付・販売に関する語が近くにある範囲だけ採用する。
                if not any(
                    word.casefold() in context.casefold()
                    for word in TICKET_WORDS
                ):
                    continue

                # 表示期間と重ならないものは除外
                if end_date < START or start_date > END:
                    continue

                start_time = ""
                end_time = ""

                start_time_matches = list(TIME_PATTERN.finditer(between))
                if start_time_matches:
                    start_time = start_time_matches[-1].group(0)

                end_time_match = TIME_PATTERN.search(candidate[end_end:])
                if end_time_match:
                    end_time = end_time_match.group(0)

                kind = ticket_kind(context)
                period_title = f"【{kind}】{parent_title}"
                period_id_source = (
                    f"{source}|ticket|{start_date}|{end_date}|"
                    f"{parent_title}|{source_url}|{kind}"
                )
                period_id = hashlib.sha256(
                    period_id_source.encode("utf-8")
                ).hexdigest()[:16]

                note_parts = [
                    f"受付期間:{start_date.isoformat()}"
                    + (f" {start_time}" if start_time else "")
                    + " ～ "
                    + f"{end_date.isoformat()}"
                    + (f" {end_time}" if end_time else ""),
                    f"対象イベント:{parent_title}",
                ]

                for extra in context_lines:
                    if any(word in extra for word in PAYMENT_WORDS):
                        if extra not in note_parts:
                            note_parts.append(extra)

                event = {
                    "id": f"{source}-ticket-{period_id}",
                    "date": start_date.isoformat(),
                    "start_date": start_date.isoformat(),
                    "end_date": end_date.isoformat(),
                    "period_type": "ticket",
                    "time": start_time,
                    "title": period_title,
                    "member": "",
                    "type": "チケット受付",
                    "place": "",
                    "url": source_url,
                    "notes": "\n".join(note_parts),
                    "source": source,
                    "origin": "official",
                    "unconfirmed": False,
                }

                key = (
                    event["start_date"],
                    event["end_date"],
                    event["title"],
                    event["url"],
                )
                if key not in seen:
                    seen.add(key)
                    results.append(event)

    return results


def collect_note_lines(detail):
    """衣装・注意事項など、参考になる本文行を集める。"""
    return list(dict.fromkeys(
        line
        for line in detail.get("lines", [])
        if len(line) <= 300 and any(word in line for word in NOTE_KEYWORDS)
    ))


def enrich_event(item, update_member=False):
    """イベント詳細を調べて会場・注意事項・チケット期間を追加する。

    戻り値: (チケット期間イベントのリスト, 会場が見つかったか)
    """
    detail = get_detail(item["url"])
    if not detail.get("text"):
        return [], False

    if update_member:
        # タイトルにメンバー名がない場合のみ、本文から判定する
        item["member"] = member_for(item["title"], detail["text"])

    place = detail.get("place", "")
    if place:
        item["place"] = place

    note_lines = collect_note_lines(detail)
    if note_lines:
        item["notes"] += "\n" + "\n".join(note_lines)

    periods = extract_ticket_periods(
        detail, item["source"], item["title"], item["url"]
    )
    return periods, bool(place)


# ============================================================
# BOYS AND MEN
# ============================================================

def iter_month_urls():
    year, month = START.year, START.month
    while (year, month) <= (END.year, END.month):
        yield f"{BOYS_CALENDAR}/{year}/{month}"
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1


def day_from_panel_id(node):
    node_id = node.get("id", "")
    match = re.search(
        r"(?:collapse|schedule|day)[-_]?((?:20\d{2})\d{4})",
        node_id,
        re.I,
    )
    if not match:
        return None
    try:
        return datetime.strptime(match.group(1), "%Y%m%d").date()
    except ValueError:
        return None


def nearest_heading_date(anchor):
    for tag in anchor.find_all_previous(
        ["h1", "h2", "h3", "h4", "strong", "b"], limit=20
    ):
        text = normalize(tag.get_text(" ", strip=True))
        if len(text) <= 45:
            event_date = parse_full_date(text)
            if event_date:
                return event_date
    return None


def boys_link_candidate(anchor, base_url):
    """アンカーからタイトルとURLを取り出す。対象外ならNone。"""
    title = normalize(anchor.get_text(" ", strip=True))
    event_url = urldefrag(urljoin(base_url, anchor.get("href", "")))[0]

    if should_skip_title(title) or not event_url.startswith("https://"):
        return None
    if is_excluded_boys_url(event_url):
        return None
    return title, event_url


def scrape_boys_month(url):
    soup = get_soup(url)
    found = {}
    panels = []

    for node in soup.find_all(id=True):
        event_date = day_from_panel_id(node)
        if event_date:
            panels.append((node, event_date))

    if panels:
        for panel, event_date in panels:
            for anchor in panel.select("a[href]"):
                candidate = boys_link_candidate(anchor, url)
                if not candidate:
                    continue
                title, event_url = candidate
                item = make_event("boys", event_date, title, event_url)
                if item:
                    found[(item["date"], item["title"], item["url"])] = item
    else:
        for anchor in soup.select("a[href]"):
            candidate = boys_link_candidate(anchor, url)
            if not candidate:
                continue
            title, event_url = candidate
            event_date = parse_full_date(title) or nearest_heading_date(anchor)
            if not event_date:
                continue
            item = make_event("boys", event_date, title, event_url)
            if item:
                found[(item["date"], item["title"], item["url"])] = item

    return list(found.values())


def scrape_boys():
    all_events, errors, pages_ok = [], [], 0
    urls = list(iter_month_urls())

    for url in urls:
        try:
            all_events.extend(scrape_boys_month(url))
            pages_ok += 1
        except Exception as exc:
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
            print(f"WARN BOYS {url}: {exc}", file=sys.stderr)

    unique = {}
    for item in all_events:
        unique[(item["date"], item["title"].casefold(), item["url"])] = item

    base_events = list(unique.values())
    ticket_events = []
    detail_checked = 0
    places_found = 0

    # 過去分を大量に取り直さないよう、直近30日～未来の詳細を調べる
    for item in base_events:
        event_date = date.fromisoformat(item["date"])
        if event_date < TODAY - timedelta(days=30):
            continue
        if event_date > END:
            continue
        if not item["url"].startswith(BOYS_BASE + "/contents/"):
            continue

        detail_checked += 1
        periods, found_place = enrich_event(item)
        ticket_events.extend(periods)
        if found_place:
            places_found += 1

    combined = base_events + ticket_events
    result = {}
    for item in combined:
        result[(item["id"], item["date"], item["title"])] = item

    status = empty_status(BOYS_CALENDAR)
    status.update({
        "ok": pages_ok > 0,
        "count": len(base_events),
        "ticket_periods": len(ticket_events),
        "places_found": places_found,
        "detail_pages_checked": detail_checked,
        "pages_ok": pages_ok,
        "pages_tried": len(urls),
        "errors": errors[:8],
    })
    return list(result.values()), status


# ============================================================
# でらボ(Bitfan API)
# ============================================================

def get_derabo_token(soup):
    node = soup.select_one('meta[name="jwt-token"]')
    if node:
        return node.get("content", "").strip()
    return ""


def get_derabo_api_schedules(token):
    """Bitfan公式APIから対象期間のスケジュールを取得する。"""
    api_url = f"{DERA_API_BASE}/fanclubs/dxboys/schedules"

    params = {
        "from_date": START.isoformat(),
        "to_date": (END + timedelta(days=1)).isoformat(),
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Referer": DERA_SCHEDULE,
        "Accept": "application/json, text/plain, */*",
    }

    response = throttled_get(api_url, params=params, headers=headers)

    # トークンはログに出さない(URLにも含まれない)
    print("DERABO_API_STATUS:", response.status_code, file=sys.stderr)

    response.raise_for_status()

    try:
        payload = response.json()
    except ValueError as exc:
        raise ValueError(
            f"Bitfan APIがJSON以外を返しました: {response.text[:300]}"
        ) from exc

    if not isinstance(payload, dict):
        raise ValueError(
            f"Bitfan APIのレスポンス形式が想定外です: {type(payload).__name__}"
        )

    schedules = payload.get("schedules")
    if not isinstance(schedules, list):
        raise ValueError(
            "Bitfan APIのschedulesがリストではありません。"
            f"レスポンスキー: {list(payload.keys())}"
        )

    return schedules


def parse_derabo_datetime(value):
    """Bitfan APIの日付・日時から開催日を取得する。"""
    if not value:
        return None

    parsed = parse_full_date(str(value))
    if parsed:
        return parsed

    try:
        parsed_datetime = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed_datetime.tzinfo:
            parsed_datetime = parsed_datetime.astimezone(JST)
        return parsed_datetime.date()
    except (ValueError, TypeError):
        return None


def build_derabo_detail_url(schedule):
    """APIのスケジュール情報から公式詳細ページURLを作る。"""
    schedule_id = schedule.get("id")
    if schedule_id is None:
        return ""

    detail_url = f"{DERA_BASE}/schedules/{schedule_id}"
    repeat_token = schedule.get("encoded_repeat_token")

    if repeat_token:
        detail_url += "?" + urlencode({"r": repeat_token})

    return detail_url


def _strings_from_json(obj, path=""):
    """JSON内の文字列を (キー名, 文字列) で再帰的に取り出す。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _strings_from_json(value, str(key))
    elif isinstance(obj, list):
        for value in obj:
            yield from _strings_from_json(value, path)
    elif isinstance(obj, str) and obj.strip():
        yield path, obj


def detail_from_schedule(schedule):
    """APIのスケジュールJSONから、get_detail()と同じ形の情報を作る。"""
    lines = []
    seen = set()
    place = ""

    for key, text in _strings_from_json(schedule):
        # HTMLが入っている項目はタグを除去して改行で分割
        if "<" in text and ">" in text:
            text = BeautifulSoup(text, "html.parser").get_text("\n")

        for raw in text.splitlines():
            line = normalize(raw)
            if line and line not in seen:
                seen.add(line)
                lines.append(line)

        # 会場らしいキー名の値は、そのまま会場候補にする
        if not place and any(k in key.lower() for k in PLACE_KEYS):
            place = clean_place(text)

    if not place:
        place = extract_place(None, lines)

    return {"lines": lines, "text": "\n".join(lines), "place": place}


def merge_details(html_detail, api_detail):
    """HTML解析とAPI由来の情報を統合する(重複行は除く)。"""
    lines = list(html_detail.get("lines", []))
    seen = set(lines)
    for line in api_detail.get("lines", []):
        if line not in seen:
            seen.add(line)
            lines.append(line)
    return {
        "lines": lines,
        "text": "\n".join(lines),
        "place": html_detail.get("place") or api_detail.get("place", ""),
    }


def scrape_derabo():
    events = []
    ticket_events = []
    errors = []
    places_found = 0
    detail_pages_checked = 0

    # 1. 公式スケジュールページからJWTを取得
    try:
        schedule_soup = get_soup(DERA_SCHEDULE)
        token = get_derabo_token(schedule_soup)
        if not token:
            raise ValueError("公式スケジュールページにJWTトークンがありません。")
    except Exception as exc:
        return [], empty_status(
            DERA_SCHEDULE, f"JWT取得失敗: {type(exc).__name__}: {exc}"
        )

    # 2. APIからスケジュール一覧を取得
    try:
        schedules = get_derabo_api_schedules(token)
    except Exception as exc:
        return [], empty_status(
            DERA_API_BASE + "/fanclubs/dxboys/schedules",
            f"API取得失敗: {type(exc).__name__}: {exc}",
        )

    print(f"DERA API: {len(schedules)} schedules received", file=sys.stderr)

    # APIが返す項目名を確認するためのログ(Actionsのログに出ます)
    if schedules:
        print(
            "DERA_SAMPLE:",
            json.dumps(schedules[0], ensure_ascii=False)[:1500],
            file=sys.stderr,
        )

    # 3. 開催日時・タイトルからイベントを作成
    for schedule in schedules:
        try:
            if not isinstance(schedule, dict):
                continue

            title = normalize(schedule.get("title", ""))
            event_date = parse_derabo_datetime(schedule.get("start_datetime"))

            if not title:
                errors.append(f"タイトルなし: {str(schedule)[:200]}")
                continue

            if not event_date:
                errors.append(
                    f"開催日解析失敗: {title} / {schedule.get('start_datetime')}"
                )
                continue

            detail_url = build_derabo_detail_url(schedule)
            if not detail_url:
                errors.append(f"詳細URL作成失敗: {title}")
                continue

            item = make_event("dera", event_date, title, detail_url)
            if not item:
                continue

            # 4. 詳細ページ(HTML)とAPIのJSONを統合して会場・受付期間を取得
            detail_pages_checked += 1
            html_detail = get_detail(detail_url)
            api_detail = detail_from_schedule(schedule)
            DETAIL_CACHE[detail_url] = merge_details(html_detail, api_detail)

            periods, found_place = enrich_event(item, update_member=True)
            ticket_events.extend(periods)
            if found_place:
                places_found += 1

            events.append(item)

        except Exception as exc:
            title_text = (
                schedule.get("title", "(タイトル不明)")
                if isinstance(schedule, dict) else "(不明)"
            )
            message = f"{title_text}: {type(exc).__name__}: {exc}"
            errors.append(message)
            print(f"WARN DERA {message}", file=sys.stderr)

    # 5. 重複を除去
    unique = {}
    for item in events + ticket_events:
        unique[(item["id"], item["date"], item["title"])] = item

    status = empty_status(DERA_SCHEDULE)
    status.update({
        "ok": True,
        "count": len(events),
        "ticket_periods": len(ticket_events),
        "places_found": places_found,
        "api_schedules_received": len(schedules),
        "detail_pages_checked": detail_pages_checked,
        "errors": errors[:10],
    })
    return list(unique.values()), status


# ============================================================
# JSON出力
# ============================================================

def main():
    events = []
    report = {}

    boys_events, report["BOYS AND MEN公式"] = scrape_boys()
    events.extend(boys_events)

    dera_events, report["でらボ公式スケジュール"] = scrape_derabo()
    events.extend(dera_events)

    report["でらボ公式NEWS"] = {
        "ok": True,
        "count": 0,
        "note": "NEWS掲載日を開催日と誤認しないため自動登録対象外。",
        "url": DERA_BASE + "/",
    }

    # 重複を除去
    unique = {}
    for item in events:
        unique[(item["id"], item["date"], item["title"])] = item

    sorted_events = sorted(
        unique.values(),
        key=lambda item: (
            item.get("date", ""),
            item.get("time", ""),
            item.get("title", "").casefold(),
        ),
    )

    data = {
        "updated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "range": {
            "from": START.isoformat(),
            "to": END.isoformat(),
        },
        "sources": report,
        "events": sorted_events,
    }

    os.makedirs("data", exist_ok=True)
    with open("data/events.json", "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)

    print(f"Wrote {len(sorted_events)} events")
    print(f"Places found: {sum(s.get('places_found', 0) for s in report.values())}")
    print(f"Ticket periods: {sum(s.get('ticket_periods', 0) for s in report.values())}")

    for name, status in report.items():
        print(
            f"- {name}: ok={status.get('ok')} "
            f"count={status.get('count')} "
            f"places={status.get('places_found', 0)} "
            f"tickets={status.get('ticket_periods', 0)}"
        )

    if not any(status.get("ok") for status in report.values()):
        sys.exit("All official sources failed; check network or site markup.")


if __name__ == "__main__":
    main()
