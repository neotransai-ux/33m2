from flask import Flask, render_template, jsonify, request, session
import requests
import time
import glob
import os
import json
from datetime import date, datetime, timedelta
from calendar import monthrange

app = Flask(__name__)
app.secret_key = "samsam2_secret_key_2026"
# 세션 쿠키 7일 유지 (브라우저 종료해도 유지)
app.permanent_session_lifetime = timedelta(days=7)


# ── 로그인 쿠키 디스크 저장 (Flask 세션 쿠키 4KB 한계 회피 + 서버 재시작/리로드 시 유지) ──
SESSION_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_sessions.json")


def _load_sessions():
    if not os.path.exists(SESSION_FILE):
        return {}
    try:
        with open(SESSION_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_sessions(data):
    try:
        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception:
        pass


def _get_user_cookies(email):
    if not email:
        return None
    return _load_sessions().get(email, {}).get("cookies")


def _set_user_cookies(email, cookies):
    sessions = _load_sessions()
    sessions[email] = {"cookies": cookies, "saved_at": datetime.now().isoformat()}
    _save_sessions(sessions)


def _clear_user_cookies(email):
    if not email:
        return
    sessions = _load_sessions()
    if email in sessions:
        sessions.pop(email, None)
        _save_sessions(sessions)


def _current_cookies():
    """현재 세션 사용자의 저장된 쿠키 반환 (없으면 None)"""
    return _get_user_cookies(session.get("email"))

# ── 공통 설정 ──────────────────────────────────────────────────────────
API_BASE         = "https://web.33m2.co.kr/v1/use-auth/rooms"
MAP_API_BASE     = "https://web.33m2.co.kr/v1/use-auth/map/rooms"   # 좌표 검색용
SCHEDULE_BASE    = "https://web.33m2.co.kr/v1/use-auth/rooms/{rid}/schedules"
ROOM_DETAIL_BASE = "https://web.33m2.co.kr/v1/use-auth/rooms"        # /{rid} 으로 상세 조회
IMG_CDN          = "https://d1pviohoskiraj.cloudfront.net"
IMG_QUERY        = "?b=samsamm2&d=640x427"
IMG_QUERY_LARGE  = "?b=samsamm2&d=1280x854"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ko-KR,ko;q=0.9",
    "Referer": "https://web.33m2.co.kr/",
    "Origin": "https://web.33m2.co.kr",
}


# ── Selenium 드라이버 (로그인 전용) ────────────────────────────────────
def _get_driver_path():
    wdm_dir = os.path.expanduser("~/.wdm/drivers/chromedriver/win64")
    paths = glob.glob(f"{wdm_dir}/**/chromedriver.exe", recursive=True)
    return paths[0] if paths else None


def _make_driver(headless=True):
    from selenium import webdriver
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.chrome.service import Service
    from webdriver_manager.chrome import ChromeDriverManager

    options = Options()
    if headless:
        options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--window-size=1400,900")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])
    options.add_experimental_option("useAutomationExtension", False)
    options.add_argument(f"user-agent={HEADERS['User-Agent']}")

    driver_path = _get_driver_path()
    service = Service(driver_path) if driver_path else Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    })
    return driver


# ── 매물 목록 크롤링 ───────────────────────────────────────────────────
def _zoom_to_bbox(lat, lng, zoom):
    """zoomLevel + 중심 좌표 → 바운딩 박스 (sw/ne) 계산"""
    # 지도 타일 1개 = 360 / 2^zoom 도
    # 화면 4×3 타일 크기를 기본값으로 사용 (넉넉하게 *6 적용)
    delta = 360 / (2 ** zoom) * 6
    return {
        "swLat": round(lat - delta / 2, 7),
        "swLng": round(lng - delta / 2, 7),
        "neLat": round(lat + delta / 2, 7),
        "neLng": round(lng + delta / 2, 7),
    }


def fetch_page(sort="POPULAR", property_types="OFFICETEL", page=1, size=20,
               keyword=None):
    """정렬순 / 키워드 검색 — 일반 rooms API"""
    params = {"sortBy": sort, "propertyTypes": property_types, "page": page, "size": size}
    if keyword:
        params["keyword"] = keyword
    resp = requests.get(API_BASE, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    body = resp.json()
    rooms_obj = body["data"]["rooms"]
    return rooms_obj.get("content", []), rooms_obj.get("last", True)


def fetch_map_page(lat, lng, zoom, sort="POPULAR", property_types="OFFICETEL", page=1, size=20):
    """좌표(바운딩 박스) 검색 — map/rooms API"""
    bbox = _zoom_to_bbox(lat, lng, zoom)
    params = {
        **bbox,
        "zoomLevel":     zoom,
        "sortBy":        sort,
        "propertyTypes": property_types,
        "page":          page,
        "size":          size,
    }
    resp = requests.get(MAP_API_BASE, params=params, headers=HEADERS, timeout=10)
    resp.raise_for_status()
    body = resp.json()
    data = body["data"]                   # map API: data.content (rooms 래퍼 없음)
    return data.get("content", []), data.get("last", True)


def build_image_url(pic_main, large=False):
    if not pic_main:
        return ""
    q = IMG_QUERY_LARGE if large else IMG_QUERY
    return f"{IMG_CDN}/{pic_main}{q}"


def fmt_price(won):
    if won is None:
        return ""
    man = won // 10000
    return f"{man:,}만원" if man else f"{won:,}원"


def parse_room(item):
    using_fee = item.get("usingFee", 0)   # 주(週) 단위 금액
    mgmt_fee  = item.get("mgmtFee", 0)
    parts = []
    if using_fee: parts.append(f"주당 {fmt_price(using_fee)}")
    if mgmt_fee:  parts.append(f"관리비 {fmt_price(mgmt_fee)}")

    pyeong = item.get("pyeongSize")
    sqm    = round(pyeong * 3.3058, 1) if pyeong else None
    area   = f"{pyeong}평 ({sqm}㎡)" if pyeong else ""

    layout_parts = []
    for key, label in [("roomCnt","침실"),("bathroomCnt","욕실"),("cookroomCnt","주방"),("sittingroomCnt","거실")]:
        v = item.get(key, 0)
        if v: layout_parts.append(f"{label} {v}")

    return {
        "rid":        item.get("rid", ""),
        "title":      item.get("roomName", "오피스텔"),
        "price":      " / ".join(parts) if parts else "가격 정보 없음",
        "using_fee":  using_fee,
        "mgmt_fee":   mgmt_fee,
        "address":    item.get("addrStreet") or item.get("addrLot", ""),
        "province":   item.get("province", ""),
        "town":       item.get("town", ""),
        "area":       area,
        "pyeong":     pyeong or 0,
        "layout":     " · ".join(layout_parts),
        "room_type":  item.get("propertyType", "오피스텔"),
        "image":      build_image_url(item.get("picMain")),
        "is_new":     item.get("isNew", False),
        "super_host": item.get("isSuperHost", False),
        "discount":   item.get("longtermDiscountPer", 0),
        "detail_url": f"https://web.33m2.co.kr/guest/room/{item.get('rid','')}",
    }


# ── 매물 상세 조회 ─────────────────────────────────────────────────────
def fetch_room_detail(rid, cookies=None):
    """단일 매물 상세 데이터 조회. /v1/use-auth/rooms/{rid}"""
    url = f"{ROOM_DETAIL_BASE}/{rid}"
    try:
        resp = requests.get(url, headers=HEADERS, cookies=cookies, timeout=10)
        if resp.status_code in (401, 403):
            return None, "인증 만료"
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as e:
        return None, str(e)
    return body.get("data", body), None


def _pick(d, *keys):
    """여러 후보 키 중 처음 나오는 non-empty 값 반환"""
    for k in keys:
        v = d.get(k)
        if v not in (None, "", 0):
            return v
    # 0도 유효한 값일 수 있음 (관리비 0원 등) → 0이라도 키가 있으면 반환
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def parse_room_detail(d):
    """상세 응답 → UI 친화 dict (필드명을 가능한 한 다양한 후보로 시도)"""
    if not isinstance(d, dict):
        return {}

    pyeong = _pick(d, "pyeongSize", "exclusivePyeong", "pyeong")
    sqm    = round(pyeong * 3.3058, 1) if isinstance(pyeong, (int, float)) and pyeong else None

    # 이미지 리스트 (여러 후보 키 + dict/str 혼합 처리)
    pics_raw = (
        d.get("pics") or d.get("pictures") or d.get("roomPics")
        or d.get("picList") or d.get("images") or []
    )
    image_urls = []
    if isinstance(pics_raw, list):
        for p in pics_raw:
            url_part = None
            if isinstance(p, dict):
                url_part = (p.get("picUrl") or p.get("url") or p.get("path")
                            or p.get("pic") or p.get("name"))
            elif isinstance(p, str):
                url_part = p
            if url_part:
                image_urls.append(build_image_url(url_part, large=False))
    if not image_urls and d.get("picMain"):
        image_urls.append(build_image_url(d.get("picMain"), large=False))

    # 좌표: 다양한 필드명 + 중첩 객체 모두 시도
    lat = _pick(d, "lat", "latitude", "yPos", "yCoord", "mapY", "geoY",
                "positionLat", "gpsLat", "yLocation", "y")
    lng = _pick(d, "lng", "longitude", "lon", "xPos", "xCoord", "mapX", "geoX",
                "positionLng", "gpsLng", "xLocation", "x")
    if lat is None or lng is None:
        for nest_key in ("location", "geo", "position", "coords", "coord", "geoLocation"):
            obj = d.get(nest_key)
            if isinstance(obj, dict):
                if lat is None: lat = _pick(obj, "lat", "latitude", "y")
                if lng is None: lng = _pick(obj, "lng", "longitude", "lon", "x")
                if lat is not None and lng is not None: break
    try:
        lat = float(lat) if lat not in (None, "") else None
        lng = float(lng) if lng not in (None, "") else None
        # 한국 영역 체크 (위도 33~39, 경도 124~132). 0,0 이나 명백히 벗어난 값 거름.
        if lat is not None and lng is not None:
            if not (33 <= lat <= 39 and 124 <= lng <= 132):
                lat = lng = None
    except (ValueError, TypeError):
        lat = lng = None

    floor     = _pick(d, "floor", "floorNo", "floorNum")
    addr_main = _pick(d, "addrStreet", "addrLot")
    full_addr = f"{addr_main} {floor}층" if addr_main and floor else (addr_main or "")

    return {
        "title":         _pick(d, "roomName", "title"),
        "full_address":  full_addr,
        "addr_street":   d.get("addrStreet"),
        "addr_lot":      d.get("addrLot"),
        "province":      d.get("province"),
        "town":          d.get("town"),
        "floor":         floor,
        "property_type": _pick(d, "propertyType", "buildingType"),
        "pyeong":        pyeong,
        "sqm":           sqm,
        "area_label":    f"{pyeong}평 ({sqm}㎡)" if pyeong and sqm else "",
        "room_cnt":         d.get("roomCnt"),
        "bathroom_cnt":     d.get("bathroomCnt"),
        "cookroom_cnt":     d.get("cookroomCnt"),
        "sittingroom_cnt":  d.get("sittingroomCnt"),
        "using_fee":     d.get("usingFee"),
        "mgmt_fee":      d.get("mgmtFee"),
        "deposit":       _pick(d, "deposit", "depositFee", "depositAmount"),
        "cleaning_fee":  _pick(d, "cleaningFee", "cleanFee"),
        "rating":        _pick(d, "avgRating", "rating", "ratingAvg"),
        "review_count":  _pick(d, "reviewCnt", "reviewCount"),
        "available_date": _pick(d, "availableDate", "availableFrom", "checkinAvailableDate"),
        "min_stay_days":  _pick(d, "minStayDays", "minStayDay", "minNight"),
        "max_stay_days":  _pick(d, "maxStayDays", "maxStayDay", "maxNight"),
        "is_super_host":  d.get("isSuperHost"),
        "longterm_discount_per": d.get("longtermDiscountPer"),
        "lat":           lat,
        "lng":           lng,
        "image_urls":    image_urls[:20],
    }


# ── 로그인 (Selenium) ──────────────────────────────────────────────────
def selenium_login(email, password):
    """Selenium으로 33m2 로그인, 세션 쿠키 반환"""
    from selenium.webdriver.common.by import By
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC

    driver = _make_driver(headless=True)
    error  = None
    cookies = {}
    try:
        driver.get("https://web.33m2.co.kr/sign-in")
        time.sleep(4)

        email_f = WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[type='email']"))
        )
        email_f.clear()
        email_f.send_keys(email)

        pw_f = driver.find_element(By.CSS_SELECTOR, "input[type='password']")
        pw_f.clear()
        pw_f.send_keys(password)

        driver.find_element(By.CSS_SELECTOR, "button[type='submit']").click()
        time.sleep(4)

        if "sign-in" in driver.current_url:
            return None, "이메일 또는 비밀번호가 올바르지 않습니다."

        cookies = {c["name"]: c["value"] for c in driver.get_cookies()}

    except Exception as e:
        error = str(e)
    finally:
        driver.quit()

    return (cookies if not error else None), error


# ── 예약 현황 API 조회 ─────────────────────────────────────────────────

# status 분류 상수
_BOOKING_STATUSES = {"BOOKING", "BOOKED", "RESERVED", "UNAVAILABLE", "CLOSED"}
_DISABLE_STATUSES = {"DISABLE", "DISABLED", "BLOCK", "BLOCKED"}


def fetch_schedules(rid, year, month, cookies_dict, include_disable=False):
    """
    /v1/use-auth/rooms/{rid}/schedules?year=YYYY&month=M 호출.
    include_disable=True 이면 status="disable" 날짜도 예약(blocked)으로 집계.
    반환: (result_dict, error_str)
    """
    url    = SCHEDULE_BASE.format(rid=rid)
    params = {"year": year, "month": month}
    try:
        resp = requests.get(
            url, params=params,
            headers=HEADERS,
            cookies=cookies_dict,
            timeout=10,
        )
        if resp.status_code in (401, 403):
            return None, "인증 만료: 다시 로그인해 주세요."
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as e:
        return None, str(e)

    # ── 응답 구조 파싱 ────────────────────────────────────────────────
    # 구조 A: {"data": [{"date":"2026-06-01","status":"booking"}, ...]}
    # 구조 B: {"data": {"schedules": [...] }}
    # 구조 C: {"data": {"2026-06-01": "booking", ...}}
    data = body.get("data", body)

    booking_dates = set()   # status=booking
    disable_dates = set()   # status=disable

    def _classify(d, st_raw):
        if not d:
            return
        st = st_raw.upper().strip()
        if any(s in st for s in _BOOKING_STATUSES):
            booking_dates.add(d)
        elif any(s in st for s in _DISABLE_STATUSES):
            disable_dates.add(d)
        elif st == "":
            # status 없음 → booking으로 간주
            booking_dates.add(d)

    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                _classify(
                    item.get("date", ""),
                    item.get("status") or item.get("type") or item.get("state") or ""
                )
    elif isinstance(data, dict):
        schedules = (
            data.get("schedules") or data.get("schedule") or
            data.get("bookings")  or data.get("dates") or None
        )
        if isinstance(schedules, list):
            for item in schedules:
                if isinstance(item, dict):
                    _classify(
                        item.get("date", ""),
                        item.get("status") or item.get("type") or item.get("state") or ""
                    )
        elif isinstance(schedules, dict):
            for d, st in schedules.items():
                _classify(d, st if isinstance(st, str) else "")
        elif schedules is None:
            for k, v in data.items():
                _classify(k, v if isinstance(v, str) else "")

    # 옵션에 따라 blocked 집합 결정
    booked_dates = booking_dates | (disable_dates if include_disable else set())

    total_days = monthrange(year, month)[1]
    prefix     = f"{year}-{month:02d}-"
    blocked    = sorted(d for d in booked_dates  if d.startswith(prefix))
    booking_only = sorted(d for d in booking_dates if d.startswith(prefix))
    disable_only = sorted(d for d in disable_dates if d.startswith(prefix))
    available  = [
        f"{year}-{month:02d}-{day:02d}"
        for day in range(1, total_days + 1)
        if f"{year}-{month:02d}-{day:02d}" not in booked_dates
    ]

    return {
        "total":            total_days,
        "blocked":          len(blocked),
        "available":        len(available),
        "rate":             round(len(blocked) / total_days * 100, 1) if total_days else 0,
        "blocked_dates":    blocked,
        "booking_dates":    booking_only,   # status=booking만
        "disable_dates":    disable_only,   # status=disable만
        "available_dates":  available,
        "_raw_sample":      body,           # 디버그용
    }, None


def analyze_room_bookings(rid, cookies_dict, until_month=12, until_year=None,
                          include_disable=False):
    """
    현재 월부터 until_year/until_month 까지 매월 schedule API 호출.
    include_disable=True 이면 status=disable 날짜도 blocked로 집계.
    반환: ({YYYY-MM: {...}}, error_str)
    """
    if until_year is None:
        until_year = date.today().year

    today      = date.today()
    monthly    = {}
    auth_error = None

    raw_m = today.month
    raw_y = today.year

    while True:
        if raw_y > until_year or (raw_y == until_year and raw_m > until_month):
            break

        ym_key = f"{raw_y}-{raw_m:02d}"
        result, err = fetch_schedules(rid, raw_y, raw_m, cookies_dict,
                                      include_disable=include_disable)
        if err:
            if "인증" in err:
                auth_error = err
                break
            monthly[ym_key] = {"error": err}
        elif result:
            # _raw_sample은 첫 달만 보존, 이후엔 제거해 응답 크기 줄이기
            if raw_y != today.year or raw_m != today.month:
                result.pop("_raw_sample", None)
            monthly[ym_key] = result

        raw_m += 1
        if raw_m > 12:
            raw_m = 1
            raw_y += 1

    if auth_error and not monthly:
        return None, auth_error

    return monthly, None


# ── Flask 라우트 ──────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/crawl")
def crawl():
    sort           = request.args.get("sort", "POPULAR")
    property_types = request.args.get("propertyTypes", "OFFICETEL")
    max_pages      = int(request.args.get("maxPages", 5))
    search_mode    = request.args.get("searchMode", "sort")  # sort | keyword | coord
    keyword        = request.args.get("keyword", "").strip() or None

    lat  = request.args.get("lat")
    lng  = request.args.get("lng")
    zoom = request.args.get("zoom")
    try:
        lat  = float(lat)  if lat  else None
        lng  = float(lng)  if lng  else None
        zoom = int(zoom)   if zoom else None
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "좌표 값이 올바르지 않습니다.", "rooms": []})

    if search_mode == "keyword" and not keyword:
        return jsonify({"success": False, "error": "키워드를 입력하세요.", "rooms": []})
    if search_mode == "coord" and (lat is None or lng is None):
        return jsonify({"success": False, "error": "좌표 URL을 올바르게 입력하세요.", "rooms": []})

    all_rooms = []
    seen_rids = set()
    try:
        for page in range(1, max_pages + 1):
            if search_mode == "coord":
                items, is_last = fetch_map_page(
                    lat=lat, lng=lng, zoom=zoom or 15,
                    sort=sort, property_types=property_types, page=page,
                )
            else:
                items, is_last = fetch_page(
                    sort=sort, property_types=property_types, page=page,
                    keyword=keyword if search_mode == "keyword" else None,
                )
            for item in items:
                rid = item.get("rid")
                if rid not in seen_rids:
                    seen_rids.add(rid)
                    all_rooms.append(parse_room(item))
            if is_last:
                break
    except Exception as e:
        return jsonify({"success": False, "error": str(e), "rooms": []})

    return jsonify({
        "success":   True,
        "rooms":     all_rooms,
        "total":     len(all_rooms),
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


@app.route("/api/login", methods=["POST"])
def api_login():
    data     = request.get_json()
    email    = data.get("email", "").strip()
    password = data.get("password", "")

    if not email or not password:
        return jsonify({"success": False, "error": "이메일과 비밀번호를 입력하세요."})

    cookies, error = selenium_login(email, password)
    if error:
        return jsonify({"success": False, "error": error})

    # 쿠키는 디스크에 저장, 세션엔 이메일만 (쿠키 4KB 한계 회피)
    _set_user_cookies(email, cookies)
    session.permanent = True
    session["email"]  = email
    return jsonify({"success": True, "email": email})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    _clear_user_cookies(session.get("email"))
    session.clear()
    return jsonify({"success": True})


@app.route("/api/session")
def api_session():
    """페이지 새로고침 시 프론트엔드가 로그인 상태를 복원하기 위한 엔드포인트"""
    email   = session.get("email")
    cookies = _get_user_cookies(email) if email else None
    if email and cookies:
        return jsonify({"loggedIn": True, "email": email})
    return jsonify({"loggedIn": False})


@app.route("/api/booking-analysis", methods=["POST"])
def booking_analysis():
    cookies = _current_cookies()
    if not cookies:
        return jsonify({"success": False, "error": "로그인이 필요합니다."})

    data            = request.get_json()
    rooms           = data.get("rooms", [])
    until_m         = int(data.get("until_month", 12))
    until_y         = int(data.get("until_year", date.today().year))
    include_disable = bool(data.get("include_disable", False))

    if not rooms:
        return jsonify({"success": False, "error": "분석할 매물이 없습니다."})

    results = []

    for room in rooms:
        rid   = room.get("rid")
        title = room.get("title", "")
        if not rid:
            continue

        # 1. 상세 정보 (물건 정보 섹션용) - 실패해도 분석은 계속 진행
        detail_raw, _   = fetch_room_detail(rid, cookies)
        detail_parsed   = parse_room_detail(detail_raw)

        # 2. 월별 예약 분석
        monthly, err = analyze_room_bookings(rid, cookies, until_m, until_y,
                                             include_disable=include_disable)
        if err:
            results.append({"rid": rid, "title": title, "error": err, "detail": detail_parsed})
            continue

        valid_months  = {k: v for k, v in (monthly or {}).items() if "error" not in v}
        total_days    = sum(v["total"]   for v in valid_months.values())
        total_blocked = sum(v["blocked"] for v in valid_months.values())

        results.append({
            "rid":           rid,
            "title":         title,
            "province":      room.get("province", ""),
            "town":          room.get("town", ""),
            "price":         room.get("price", ""),
            "image":         room.get("image", ""),
            "detail_url":    room.get("detail_url", ""),
            "using_fee":     room.get("using_fee", 0),   # 주당 임대료
            "mgmt_fee":      room.get("mgmt_fee", 0),    # 주당 관리비
            "detail":        detail_parsed,              # 물건 정보 (전체주소/면적/구조/보증금/청소비 등)
            "monthly":       monthly or {},
            "total_days":    total_days,
            "total_blocked": total_blocked,
            "overall_rate":  round(total_blocked / total_days * 100, 1) if total_days else 0,
        })

    return jsonify({
        "success":   True,
        "results":   results,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    })


# ── 매물 상세 조회 API ────────────────────────────────────────────────
@app.route("/api/room-detail/<rid>")
def api_room_detail(rid):
    """라이트박스에서 매물 상세 정보 표시용. 로그인 안 되어있어도 시도."""
    cookies = _current_cookies()  # 있으면 사용, 없어도 OK
    detail, err = fetch_room_detail(rid, cookies)
    if err and not detail:
        return jsonify({"success": False, "error": err})
    return jsonify({
        "success": True,
        "rid":     rid,
        "parsed":  parse_room_detail(detail),
        "raw":     detail,
    })


# ── 디버그: 특정 매물 특정 월 API 응답 확인 ───────────────────────────
@app.route("/api/debug-schedule/<rid>")
def debug_schedule(rid):
    """브라우저에서 직접 확인용: /api/debug-schedule/102555?year=2026&month=6"""
    cookies = _current_cookies()
    if not cookies:
        return jsonify({"error": "로그인 필요"}), 401

    year  = int(request.args.get("year",  date.today().year))
    month = int(request.args.get("month", date.today().month))

    url    = SCHEDULE_BASE.format(rid=rid)
    params = {"year": year, "month": month}
    try:
        resp = requests.get(url, params=params, headers=HEADERS,
                            cookies=cookies, timeout=10)
        raw  = resp.json()
    except Exception as e:
        return jsonify({"error": str(e)})

    result, err = fetch_schedules(rid, year, month, cookies)
    return jsonify({
        "raw_api_response": raw,
        "parsed":           result,
        "parse_error":      err,
    })


if __name__ == "__main__":
    print("=" * 50)
    print("33m2 크롤러 + 예약 분석 웹앱 시작")
    print("http://127.0.0.1:5000 에서 접속하세요")
    print("=" * 50)
    app.run(debug=True, port=5000)
