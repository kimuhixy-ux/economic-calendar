const JST_DATE = new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', month: 'long', day: 'numeric', weekday: 'short' });
const JST_TIME = new Intl.DateTimeFormat('ja-JP', { timeZone: 'Asia/Tokyo', hour: '2-digit', minute: '2-digit' });
const JST_ISO_DATE = new Intl.DateTimeFormat('sv-SE', { timeZone: 'Asia/Tokyo' });

// 終日予定は日付だけを持つので、時差でずれないよう日本時間の0時として読む
function toDate(item) {
  return new Date(item.allDay ? `${item.start}T00:00:00+09:00` : item.start);
}

function createEventLink(item) {
  const link = document.createElement('a');
  link.className = 'event';
  link.href = item.sourceUrl;
  link.target = '_blank';
  link.rel = 'noopener';
  const time = document.createElement('div');
  time.className = 'event-time';
  time.textContent = item.allDay ? '時刻未定' : JST_TIME.format(toDate(item));
  const title = document.createElement('div');
  title.textContent = `【${item.region}】${item.label}`;
  link.append(time, title);
  return link;
}

function renderEvents(events) {
  const list = document.getElementById('event-list');
  const today = JST_ISO_DATE.format(new Date());
  const upcoming = events.filter((item) => JST_ISO_DATE.format(toDate(item)) >= today);
  upcoming.sort((a, b) => toDate(a) - toDate(b));
  let currentDay = '';
  for (const item of upcoming) {
    const day = JST_DATE.format(toDate(item));
    if (day !== currentDay) {
      const heading = document.createElement('h3');
      heading.className = 'day-heading';
      heading.textContent = day;
      list.append(heading);
      currentDay = day;
    }
    list.append(createEventLink(item));
  }
}

async function copySubscribeUrl() {
  const result = document.getElementById('copy-result');
  const url = new URL('calendar.ics', location.href).href;
  try {
    await navigator.clipboard.writeText(url);
    result.textContent = 'コピーしました。';
  } catch {
    // クリップボードが使えない環境でも手で写せるよう、URLを表示する
    result.textContent = url;
  }
}

async function init() {
  document.getElementById('copy-button').addEventListener('click', copySubscribeUrl);
  try {
    const response = await fetch('data/events.json');
    const data = await response.json();
    renderEvents(data.events);
    if (data.failedSources.length > 0) {
      const warning = document.getElementById('warning');
      warning.textContent = `一部の発表元を更新できず、前回の予定を表示しています: ${data.failedSources.join(', ')}`;
      warning.hidden = false;
    }
  } catch {
    document.getElementById('event-list').textContent = '予定を読み込めませんでした。';
  }
}

init();

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js');
}
