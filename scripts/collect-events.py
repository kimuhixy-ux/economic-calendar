#!/usr/bin/env python3
"""各国の公式サイトから経済指標・金融政策の予定を集め、events.json と calendar.ics を作る。"""
import datetime as dt
import hashlib
import html
import json
import os
import pathlib
import re
import sys
import time
import urllib.parse
import urllib.request
import urllib.robotparser
from zoneinfo import ZoneInfo

USER_AGENT = 'economic-calendar/0.1 (personal calendar tool; contact: github.com/kimuhixy-ux)'
# 相手のサーバーに負担をかけないよう、1件ごとに間を空ける
REQUEST_INTERVAL_SECONDS = 3
# 過去の予定も少し残し、発表直後にカレンダーから消えないようにする
KEEP_PAST_DAYS = 90
ROOT = pathlib.Path(__file__).resolve().parent.parent
EVENTS_PATH = ROOT / 'data' / 'events.json'
ICS_PATH = ROOT / 'calendar.ics'
NEW_YORK = ZoneInfo('America/New_York')
LONDON = ZoneInfo('Europe/London')
MONTHS = ('Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec')
WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
# BLS は自動取得を禁じているため、公式 API の FRED 経由で公表日を得る。時刻は FRED にないので固定で補う
FRED_RELEASES = (
    (10, '消費者物価指数（CPI）'),
    (50, '雇用統計'),
    (46, '生産者物価指数（PPI）'),
)
robots_cache = {}


def wait_for_turn(url):
    """robots.txt に従い、禁止されていれば例外にする。"""
    parts = urllib.parse.urlsplit(url)
    host = f'{parts.scheme}://{parts.netloc}'
    if host not in robots_cache:
        parser = urllib.robotparser.RobotFileParser(f'{host}/robots.txt')
        request = urllib.request.Request(f'{host}/robots.txt', headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                parser.parse(response.read().decode('utf-8', 'replace').splitlines())
        except urllib.error.URLError:
            parser.parse([])
        robots_cache[host] = parser
    parser = robots_cache[host]
    if not parser.can_fetch(USER_AGENT, url):
        raise PermissionError(f'robots.txt で禁止: {parts.netloc}{parts.path}')
    time.sleep(max(REQUEST_INTERVAL_SECONDS, parser.crawl_delay(USER_AGENT) or 0))


def fetch(url):
    wait_for_turn(url)
    request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read().decode('utf-8', 'replace')


def month_number(name):
    # 'Feb' のような略記と 'February' の両方が使われるので、頭3文字で判定する
    return MONTHS.index(name[:3]) + 1


def strip_tags(text):
    without_blocks = re.sub(r'(?is)<(script|style|head|noscript)\b.*?</\1>', ' ', text)
    return html.unescape(re.sub(r'<[^>]+>', '\n', without_blocks))


def event(region, label, moment, source_url, note=''):
    """moment が date なら終日予定、aware な datetime なら時刻つき予定にする。"""
    all_day = not isinstance(moment, dt.datetime)
    start = moment.isoformat() if all_day else moment.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
    return {'region': region, 'label': label, 'start': start, 'allDay': all_day, 'sourceUrl': source_url, 'note': note}


def collect_fomc():
    url = 'https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
    page = fetch(url)
    events = []
    for section in re.split(r'(?=<a id="\d+">\d{4} FOMC Meetings)', page)[1:]:
        year = int(re.search(r'(\d{4}) FOMC Meetings', section).group(1))
        for month, day, star in re.findall(
                r'fomc-meeting__month[^>]*><strong>([^<]+)</strong>.*?fomc-meeting__date[^>]*>\d+-(\d+)(\*?)<',
                section, re.S):
            when = dt.datetime(year, month_number(month.split('/')[-1]), int(day), 14, 0, tzinfo=NEW_YORK)
            note = '経済見通し（SEP）あり。' if star else ''
            events.append(event('米国', 'FOMC 政策金利発表', when, url, note + '14:00は例年の時刻（公式日程に時刻の記載なし）'))
    return events


def collect_bea():
    url = 'https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics'
    text = fetch(url).replace('\r\n', '\n').replace('\n ', '')
    events = []
    for block in text.split('BEGIN:VEVENT')[1:]:
        summary = re.search(r'^SUMMARY:(.*)$', block, re.M).group(1).replace('\\,', ',')
        start = re.search(r'^DTSTART[^:]*:(\d{8}T\d{6})Z', block, re.M).group(1)
        when = dt.datetime.strptime(start, '%Y%m%dT%H%M%S').replace(tzinfo=dt.timezone.utc)
        if re.match(r'(Gross Domestic Product|GDP )', summary):
            events.append(event('米国', 'GDP', when, url, summary))
        elif summary.startswith('Personal Income and Outlays'):
            events.append(event('米国', '個人所得・支出（PCE）', when, url, summary))
    return events


def collect_census():
    events = []
    for path in ('calendar-listview-2026.html', 'calendar-listview.html'):
        url = f'https://www.census.gov/economic-indicators/{path}'
        for key in re.findall(
                r'Advance Monthly Sales for Retail and Food Services</a></td>\s*<td sorttable_customkey="(\d{12})"', fetch(url)):
            when = dt.datetime.strptime(key, '%Y%m%d%H%M').replace(tzinfo=NEW_YORK)
            events.append(event('米国', '小売売上高', when, url))
    return events


def collect_fred():
    """キーがない間は取得しない。未設定は失敗ではないので空を返す。"""
    key = os.environ.get('FRED_API_KEY')
    if not key:
        print('  FRED_API_KEY が未設定のため米国CPI・雇用統計・PPIを省略')
        return []
    today = dt.date.today().isoformat()
    events = []
    for release_id, label in FRED_RELEASES:
        query = urllib.parse.urlencode({
            'release_id': release_id, 'api_key': key, 'file_type': 'json', 'sort_order': 'asc',
            'include_release_dates_with_no_data': 'true', 'realtime_start': today, 'realtime_end': '9999-12-31'})
        data = json.loads(fetch(f'https://api.stlouisfed.org/fred/release/dates?{query}'))
        for row in data['release_dates']:
            day = dt.date.fromisoformat(row['date'])
            when = dt.datetime(day.year, day.month, day.day, 8, 30, tzinfo=NEW_YORK)
            events.append(event('米国', label, when, 'https://www.bls.gov/schedule/news_release/', '8:30は例年の時刻（FREDに時刻の記載なし）'))
    return events


def collect_ecb():
    url = 'https://www.ecb.europa.eu/press/calendars/mgcgc/html/index.en.html'
    events = []
    for day in re.findall(
            r'<dt>\s*(\d{2}/\d{2}/\d{4})\s*</dt>\s*<dd>\s*Governing Council of the ECB: monetary policy meeting[^<]*Day 2',
            fetch(url)):
        date = dt.datetime.strptime(day, '%d/%m/%Y')
        when = date.replace(hour=14, minute=15, tzinfo=ZoneInfo('Europe/Berlin'))
        events.append(event('ユーロ圏', 'ECB 政策金利発表', when, url, '14:15は例年の時刻（公式日程に時刻の記載なし）'))
    return events


def collect_eurostat():
    today = dt.date.today()
    query = urllib.parse.urlencode({
        'start': f'{today}T00:00:00Z', 'end': f'{today + dt.timedelta(days=370)}T00:00:00Z',
        'theme': '', 'category': '', 'keywords': '', 'isEuroindicator': 'true', 'authorInclude': '', 'authorExclude': ''})
    page_url = 'https://ec.europa.eu/eurostat/news/release-calendar'
    events = []
    for row in json.loads(fetch(f'https://ec.europa.eu/eurostat/o/calendars/eventsJson?{query}')):
        title = row['title']
        if 'flash' not in title.lower() or not re.search('inflation|GDP', title):
            continue
        # 末尾に Z とあるが値は現地の公表時刻（11:00）なので、ルクセンブルク時間として読む
        local = dt.datetime.strptime(row['start'], '%Y-%m-%dT%H:%MZ').replace(tzinfo=ZoneInfo('Europe/Luxembourg'))
        label = 'HICP速報（消費者物価）' if 'inflation' in title else 'GDP速報'
        events.append(event('ユーロ圏', label, local, page_url, f"{title} / {row.get('period', '')}"))
    return events


def collect_boj():
    url = 'https://www.boj.or.jp/mopo/mpmsche_minu/index.htm'
    events = []
    for section in re.split(r'(?=表\s*20\d\d年)', strip_tags(fetch(url)))[1:]:
        year = int(re.match(r'表\s*(20\d\d)年', section).group(1))
        for month, _, second_month, last_day in re.findall(r'(\d+)月(\d+)日（.）・(?:(\d+)月)?(\d+)日（.）', section):
            day = dt.date(year, int(second_month or month), int(last_day))
            events.append(event('日本', '日銀 金融政策決定会合（結果公表）', day, url, '公表時刻は会合ごとに異なるため終日予定'))
    return events


def collect_japan_cpi():
    url = 'https://www.e-stat.go.jp/release-calendar/statistics/00200573'
    lines = [line.strip() for line in strip_tags(fetch(url)).splitlines() if line.strip()]
    # 同じ表に東京都区部の速報も並ぶので、全国の区間だけを読む
    lines = lines[lines.index('全国'):lines.index('東京都区部（中旬速報値）')]
    events = []
    for i, line in enumerate(lines[:-3]):
        if re.fullmatch(r'20\d\d年\d+月分', line) and lines[i + 1] == '公表日':
            if not re.fullmatch(r'\d{4}-\d{2}-\d{2}', lines[i + 2]) or not re.fullmatch(r'\d{2}:\d{2}', lines[i + 3]):
                continue
            when = dt.datetime.fromisoformat(f'{lines[i + 2]}T{lines[i + 3]}').replace(tzinfo=ZoneInfo('Asia/Tokyo'))
            events.append(event('日本', '全国消費者物価指数（CPI）', when, url, line))
    return events


def collect_japan_gdp():
    url = 'https://www.esri.cao.go.jp/jp/sna/kouhyou/kouhyou_top.html'
    section = strip_tags(fetch(url)).split('四半期別GDP速報')[1].split('国民経済計算年次推計')[0]
    events = []
    for period, year, month, day, hour, minute in re.findall(
            r'(20\d\d年[\d-]+月期（[^）]+）)\s*(20\d\d)（[^）]+）年(\d+)月(\d+)日（.）\s*(\d+)時(\d+)分', section):
        when = dt.datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=ZoneInfo('Asia/Tokyo'))
        events.append(event('日本', 'GDP速報', when, url, period))
    return events


def collect_boe():
    url = 'https://www.bankofengland.co.uk/monetary-policy/upcoming-mpc-dates'
    text = strip_tags(fetch(url))
    events = []
    for section in re.split(r'(?=20\d\d (?:confirmed|provisional) dates)', text)[1:]:
        year = int(section[:4])
        body = section.split('Monetary Policy Committee Reports')[0]
        for day, month in re.findall(r'(?:Mon|Tues|Wednes|Thurs|Fri)day\s+(\d{1,2})\s+([A-Z][a-z]+)', body):
            when = dt.datetime(year, month_number(month), int(day), 12, 0, tzinfo=LONDON)
            events.append(event('英国', 'BoE 政策金利発表', when, url, '12:00は例年の時刻（公式日程に時刻の記載なし）'))
    return events


def collect_ons():
    targets = (
        ('consumer price inflation', r'Consumer price inflation, UK: [^,]+$', '消費者物価指数（CPI）'),
        ('GDP first quarterly estimate', r'GDP first quarterly estimate, UK: [^,]+$', 'GDP速報'),
        ('GDP monthly estimate', r'GDP monthly estimate, UK: \w+ \d{4}$', '月次GDP'),
        ('uk labour market', r'UK Labour Market: \w+ \d{4}$', '雇用統計'),
    )
    events = []
    for keywords, pattern, label in targets:
        query = urllib.parse.urlencode({'keywords': keywords, 'release-type': 'type-upcoming', 'sort': 'date-newest', 'limit': 100})
        url = f'https://www.ons.gov.uk/releasecalendar?{query}'
        for title, day, clock in re.findall(
                r'data-gtm-release-title = "([^"]*)".*?data-gtm-release-date="(\d{8})"\s*data-gtm-release-time="(\d\d:\d\d)"',
                fetch(url), re.S):
            plain = re.sub(r'<[^>]+>', '', html.unescape(html.unescape(title)))
            if re.fullmatch(pattern, plain):
                when = dt.datetime.strptime(day + clock, '%Y%m%d%H:%M').replace(tzinfo=LONDON)
                events.append(event('英国', label, when, 'https://www.ons.gov.uk/releasecalendar', plain))
    return events


def collect_rba():
    url = 'https://www.rba.gov.au/schedules-events/calendar/'
    events = []
    for stamp in re.findall(r'Monetary Policy Decision Statement.*?datetime="([^"]+)"', fetch(url), re.S):
        events.append(event('豪州', 'RBA 政策金利発表', dt.datetime.fromisoformat(stamp), url))
    return events


def collect_abs():
    labels = {
        'Consumer Price Index, Australia': '消費者物価指数（CPI）',
        'Labour Force, Australia': '雇用統計',
        'Australian National Accounts: National Income, Expenditure and Product': 'GDP',
        'Wage Price Index, Australia': '賃金指数',
    }
    first = dt.date.today().replace(day=1)
    events = []
    for offset in range(6):
        month_index = first.month - 1 + offset
        ym = f'{first.year + month_index // 12}{month_index % 12 + 1:02d}'
        url = f'https://www.abs.gov.au/release-calendar/future-releases/{ym}'
        for stamp, name in re.findall(
                r'<time datetime="([^"]+)" class="datetime">.*?event-name">\s*([^<]+?)\s*</h3>', fetch(url), re.S):
            if html.unescape(name) in labels:
                when = dt.datetime.fromisoformat(stamp.replace('Z', '+00:00'))
                events.append(event('豪州', labels[html.unescape(name)], when, url))
    return events


def collect_boc():
    url = 'https://www.bankofcanada.ca/core-functions/monetary-policy/key-interest-rate/'
    events = []
    for section in re.split(r'(?=Schedule for 20\d\d)', fetch(url).replace('&nbsp;', ' '))[1:]:
        year = int(section[13:17])
        table = section.split('</table>')[0]
        for month, day in re.findall(r'<td>([A-Z][a-z]+) (\d{1,2})</td>', table):
            when = dt.datetime(year, month_number(month), int(day), 9, 45, tzinfo=NEW_YORK)
            events.append(event('カナダ', 'BoC 政策金利発表', when, url, '9:45は例年の時刻（公式日程に時刻の記載なし）'))
    return events


def collect_statcan():
    """公式の予定表は約2週間先までしかないため、カナダの統計は直前にならないと載らない。"""
    url = 'https://www150.statcan.gc.ca/n1/dai-quo/cal2-eng.htm'
    text = html.unescape(fetch(url)).replace('\xa0', ' ')
    header_month, header_year = re.search(r'([A-Z][a-z]+) \d+\s+to\s+.*?(20\d\d)', strip_tags(text), re.S).groups()
    targets = (('Consumer Price Index', '消費者物価指数（CPI）'), ('Labour Force Survey', '雇用統計'), ('Gross domestic product', 'GDP'))
    events = []
    for section in re.split(r'(?=<h3 class="hd1">)', text)[1:]:
        month, day = re.match(r'<h3 class="hd1">([A-Z][a-z]+) (\d+)', section).groups()
        # 12月〜1月をまたぐ表では、見出しより前の月を翌年として扱う
        year = int(header_year) + (1 if month_number(month) < month_number(header_month) else 0)
        for title in re.findall(r'<div class="rtitle">(.*?)</div>', section, re.S):
            plain = re.sub(r'<[^>]+>', '', title).strip()
            for prefix, label in targets:
                if plain.startswith(prefix):
                    when = dt.datetime(year, month_number(month), int(day), 8, 30, tzinfo=NEW_YORK)
                    events.append(event('カナダ', label, when, url, plain))
    return events


def collect_snb():
    url = 'https://www.snb.ch/en/services-events/digital-services/event-schedule'
    events = []
    for day, clock, title in re.findall(
            r'publication-date[^>]*>(\d{2}\.\d{2}\.\d{4})</span>\s*<span class="publication-type[^>]*>(\d{2}:\d{2})</span>.*?<h3[^>]*>([^<]+)</h3>',
            fetch(url), re.S):
        if title.startswith('Monetary policy assessment') and 'press release' in title:
            when = dt.datetime.strptime(f'{day} {clock}', '%d.%m.%Y %H:%M').replace(tzinfo=ZoneInfo('Europe/Zurich'))
            events.append(event('スイス', 'SNB 政策金利発表', when, url))
    return events


def parse_nbs_row(page, name, year):
    """月ごとのセルから日付を取り出す。1セルに2日分ある月があるので、曜日で読み違いを検出する。"""
    row = re.search(re.escape(name) + r'[^<]*</td>(.*?)</tr>\s*<tr[^>]*>\s*<td[^>]*>\s*(\d{1,2}:\d{2})', page, re.S)
    cells = re.findall(r'<td[^>]*>(.*?)</td>', row.group(1), re.S)
    hour, minute = map(int, row.group(2).split(':'))
    moments = []
    for month, cell in enumerate(cells[:12], start=1):
        for day, weekday in re.findall(r'(\d{1,2})/(%s)' % '|'.join(WEEKDAYS), re.sub(r'<[^>]+>', '', cell)):
            moment = dt.datetime(year, month, int(day), hour, minute, tzinfo=ZoneInfo('Asia/Shanghai'))
            if WEEKDAYS[moment.weekday()] != weekday:
                raise ValueError(f'曜日が一致しない: {name} {year}-{month}-{day} {weekday}')
            moments.append(moment)
    return moments


def collect_nbs():
    base = 'https://www.stats.gov.cn/english/PressRelease/ReleaseCalendar/'
    href, year = re.search(r'href="\./([^"]+)"[^>]*>\s*\d+\.Regular Press Release Calendar of NBS in (\d{4})', fetch(base)).groups()
    url = base + href
    # 表のセルが p や span で細かく包まれていて読みにくいので、先に取り除く
    page = re.sub(r'</?(?:p|span|font)\b[^>]*>', '', fetch(url))
    targets = (
        ('National Economic Performance', '主要経済指標（GDP・工業生産・小売）'),
        ('Monthly Report on Purchasing Managers', '製造業PMI'),
        ('Monthly Report on Consumer Price Index', '消費者物価指数（CPI）'),
    )
    events = []
    for name, label in targets:
        for moment in parse_nbs_row(page, name, int(year)):
            events.append(event('中国', label, moment, url))
    return events


COLLECTORS = {
    'fomc': collect_fomc, 'bea': collect_bea, 'census': collect_census, 'fred': collect_fred,
    'ecb': collect_ecb, 'eurostat': collect_eurostat, 'boj': collect_boj, 'japan-cpi': collect_japan_cpi,
    'japan-gdp': collect_japan_gdp, 'boe': collect_boe, 'ons': collect_ons, 'rba': collect_rba,
    'abs': collect_abs, 'boc': collect_boc, 'statcan': collect_statcan, 'snb': collect_snb, 'nbs': collect_nbs,
}


def parse_start(item):
    if item['allDay']:
        return dt.datetime.fromisoformat(item['start']).replace(tzinfo=dt.timezone.utc)
    return dt.datetime.fromisoformat(item['start'].replace('Z', '+00:00'))


def merge_source(name, fresh, previous, now):
    """成功したら未来分を置き換え、過去分は残す。失敗時は前回の未来分も残し、予定が消えないようにする。"""
    earliest = now - dt.timedelta(days=KEEP_PAST_DAYS)
    kept = [item for item in previous if item['source'] == name and earliest <= parse_start(item) and (fresh is None or parse_start(item) < now)]
    new_items = [{**item, 'source': name} for item in (fresh or []) if parse_start(item) >= now]
    unique = {(item['label'], item['start']): item for item in kept + new_items}
    return list(unique.values())


def ics_escape(text):
    return text.replace('\\', '\\\\').replace(';', '\;').replace(',', '\\,').replace('\n', '\\n')


def ics_fold(line):
    """仕様上1行75バイトまでなので、文字の途中で切れないよう分割する。"""
    parts, current, size = [], '', 0
    for char in line:
        width = len(char.encode('utf-8'))
        if size + width > 74:
            parts.append(current)
            current, size = ' ', 1
        current += char
        size += width
    parts.append(current)
    return '\r\n'.join(parts)


def event_title(item):
    return f"【{item['region']}】{item['label']}"


def ics_event(item):
    uid = hashlib.sha1(f"{item['source']}|{item['label']}|{item['start']}".encode()).hexdigest()
    start = parse_start(item)
    if item['allDay']:
        next_day = start + dt.timedelta(days=1)
        when = [f"DTSTART;VALUE=DATE:{start:%Y%m%d}", f"DTEND;VALUE=DATE:{next_day:%Y%m%d}"]
    else:
        when = [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DURATION:PT30M"]
    description = '\n'.join(part for part in (item['note'], item['sourceUrl']) if part)
    # 内容が同じなら差分を出さないよう、DTSTAMP は実行時刻ではなく開始日時にする
    lines = ['BEGIN:VEVENT', f'UID:{uid}@economic-calendar', f"DTSTAMP:{start:%Y%m%dT%H%M%SZ}", *when,
             f'SUMMARY:{ics_escape(event_title(item))}', f'DESCRIPTION:{ics_escape(description)}',
             'TRANSP:TRANSPARENT', 'END:VEVENT']
    return [ics_fold(line) for line in lines]


def build_ics(events):
    lines = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'PRODID:-//economic-calendar//JA', 'CALSCALE:GREGORIAN',
             ics_fold('X-WR-CALNAME:世界の経済指標'), 'X-WR-TIMEZONE:Asia/Tokyo']
    for item in events:
        lines.extend(ics_event(item))
    lines.append('END:VCALENDAR')
    return '\r\n'.join(lines) + '\r\n'


def main():
    previous = json.loads(EVENTS_PATH.read_text(encoding='utf-8'))['events'] if EVENTS_PATH.exists() else []
    now = dt.datetime.now(dt.timezone.utc)
    events, failed = [], []
    for name, collector in COLLECTORS.items():
        print(name)
        try:
            fresh = collector()
            # FRED はキー未設定で空になるのが正常。それ以外の0件はページ構造の変化を疑う
            if not fresh and name != 'fred':
                raise ValueError('0件')
            print(f'  {len(fresh)}件')
        except Exception as error:
            print(f'  失敗: {type(error).__name__}: {error}', file=sys.stderr)
            failed.append(name)
            fresh = None
        events.extend(merge_source(name, fresh, previous, now))
    events.sort(key=lambda item: (item['start'][:10], item['start'], item['label']))
    EVENTS_PATH.write_text(json.dumps({'failedSources': failed, 'events': events}, ensure_ascii=False, indent=1) + '\n', encoding='utf-8')
    with open(ICS_PATH, 'w', encoding='utf-8', newline='') as file:
        file.write(build_ics(events))
    print(f'合計 {len(events)}件 / 失敗: {failed or "なし"}')
    if len(failed) == len(COLLECTORS):
        sys.exit(1)


if __name__ == '__main__':
    main()
