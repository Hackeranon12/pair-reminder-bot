#!/usr/bin/env python3
"""Напоминания о парах в Telegram.

Команды:
  python3 reminder.py setup              — найти твой chat_id и прислать тестовое сообщение
  python3 reminder.py                    — проверить, не пора ли что-то прислать (это запускает launchd раз в минуту)
  python3 reminder.py analyze            — показать перерывы по дням
  python3 reminder.py preview 2026-11-11 — показать, какие напоминания будут в этот день (ничего не отправляет)
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEDULE_FILE = os.path.join(HERE, "schedule.json")
ENV_FILE = os.path.join(HERE, ".env")
STATE_FILE = os.path.join(HERE, ".sent.json")

FIRST_PAIR_LEAD = 60     # за сколько минут до первой пары дня присылать план на день
LONG_GAP_LEAD = 15       # за сколько минут до пары после обеда/окна присылать повторное напоминание
# во сколько присылать план на завтра: (время, часовой пояс; None = время этого Mac)
EVENING_PREVIEWS = [("21:00", None), ("22:00", "Europe/Moscow")]
WEEK_CHECK = "18:00"     # в воскресенье в это время — проверить, есть ли расписание на новую неделю
GRACE_MIN = 30           # если Mac спал, напоминание всё ещё отправится в течение 30 минут

WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------- данные ----------

def load_env():
    env = {}
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def save_env_value(key, value):
    lines = []
    if os.path.exists(ENV_FILE):
        with open(ENV_FILE, encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines() if not l.startswith(key + "=")]
    lines.append("%s=%s" % (key, value))
    with open(ENV_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def load_schedule():
    with open(SCHEDULE_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return set(json.load(f))
    except (OSError, ValueError):
        return set()


def save_state(sent):
    # храним только последние 14 дней
    cutoff = (date.today() - timedelta(days=14)).isoformat()
    keep = sorted(s for s in sent if s[:10] >= cutoff)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False)


def at(day, hhmm, tz=None):
    """Время hhmm в день day; если указан tz — переводится в местное время Mac."""
    h, m = map(int, hhmm.split(":"))
    if tz is None:
        return datetime(day.year, day.month, day.day, h, m)
    from zoneinfo import ZoneInfo
    aware = datetime(day.year, day.month, day.day, h, m, tzinfo=ZoneInfo(tz))
    return aware.astimezone().replace(tzinfo=None)


def classes_for(sched, day):
    """Пары на день с временем начала/конца, по порядку."""
    out = []
    for c in sched["days"].get(day.isoformat(), []):
        start, end = sched["pairs"][str(c["pair"])]
        item = dict(c)
        item["start"] = at(day, start)
        item["end"] = at(day, end)
        out.append(item)
    return sorted(out, key=lambda c: c["start"])


def building(room):
    for sep in ("-", "_"):
        if sep in room:
            return room.split(sep, 1)[0]
    return None


def fmt_minutes(m):
    m = int(m)
    if m < 60:
        return "%d мин" % m
    h, mm = divmod(m, 60)
    return "%d ч %d мин" % (h, mm) if mm else "%d ч" % h


LESSON_TYPES = {"лек.": "🎤 Лекция", "пр.": "✏️ Практика", "лаб.": "🔬 Лабораторная"}


def lesson_type(c):
    return LESSON_TYPES.get(c["type"], c["type"])


def describe(c):
    return "%s: %s\n🕐 %s–%s · 📍 ауд. %s\n👤 %s" % (
        lesson_type(c), c["subject"], c["start"].strftime("%H:%M"), c["end"].strftime("%H:%M"),
        c["room"], c["teacher"])


def day_plan(classes):
    rows = []
    for c in classes:
        rows.append("%d) %s–%s  %s: %s — %s" % (
            c["pair"], c["start"].strftime("%H:%M"), c["end"].strftime("%H:%M"),
            lesson_type(c), c["subject"], c["room"]))
    return "\n".join(rows)


def day_title(day):
    return "%s, %s" % (WEEKDAYS[day.weekday()], day.strftime("%d.%m"))


# ---------- логика напоминаний ----------

def events_for(sched, day):
    """Все напоминания, которые относятся к этому дню: (id, время, текст)."""
    events = []
    classes = classes_for(sched, day)

    if classes:
        first = classes[0]
        text = "☀️ %s — пар: %d\n\n%s\n\nПервая пара через %s:\n%s" % (
            day_title(day), len(classes), day_plan(classes), fmt_minutes(FIRST_PAIR_LEAD), describe(first))
        events.append(("%s/morning" % day.isoformat(), first["start"] - timedelta(minutes=FIRST_PAIR_LEAD), text))

        for prev, nxt in zip(classes, classes[1:]):
            gap = (nxt["start"] - prev["end"]).total_seconds() / 60
            b1, b2 = building(prev["room"]), building(nxt["room"])
            move = ""
            if b1 and b2 and b1 != b2:
                move = "\n\n🏃 Смена корпуса: %s → %s" % (b1, b2)
                if gap <= 15:
                    move += " — перемена всего %s, не задерживайся!" % fmt_minutes(gap)
            elif prev["room"] == nxt["room"]:
                move = "\n\n🪑 Та же аудитория, никуда идти не надо."

            if gap <= 15:
                head = "🔔 Перемена %s. Следующая пара:" % fmt_minutes(gap)
            elif gap <= 60:
                head = "🍽 Большой перерыв %s (до %s). Потом:" % (fmt_minutes(gap), nxt["start"].strftime("%H:%M"))
            else:
                head = "🪟 Окно %s (до %s). Потом:" % (fmt_minutes(gap), nxt["start"].strftime("%H:%M"))
            events.append(("%s/after-%d" % (day.isoformat(), prev["pair"]), prev["end"],
                           "%s\n%s%s" % (head, describe(nxt), move)))

            # после обеда или окна — ещё одно напоминание прямо перед парой
            if gap > 15:
                events.append(("%s/before-%d" % (day.isoformat(), nxt["pair"]),
                               nxt["start"] - timedelta(minutes=LONG_GAP_LEAD),
                               "⏰ Через %s:\n%s" % (fmt_minutes(LONG_GAP_LEAD), describe(nxt))))

        last = classes[-1]
        events.append(("%s/done" % day.isoformat(), last["end"],
                       "✅ Пары на сегодня закончились."))

    # вечером — план на завтра
    tomorrow = day + timedelta(days=1)
    t_classes = classes_for(sched, tomorrow)
    if t_classes:
        text = "🌙 Завтра (%s) пар: %d, первая в %s, ауд. %s\n\n%s" % (
            day_title(tomorrow), len(t_classes), t_classes[0]["start"].strftime("%H:%M"),
            t_classes[0]["room"], day_plan(t_classes))
        for i, (hhmm, tz) in enumerate(EVENING_PREVIEWS):
            ev_id = "%s/evening" % day.isoformat() + ("" if i == 0 else "-%d" % i)
            events.append((ev_id, at(day, hhmm, tz), text))

    # в воскресенье — проверить, есть ли расписание на следующую неделю
    if day.weekday() == 6:
        next_week = [(day + timedelta(days=i)).isoformat() for i in range(1, 8)]
        if not any(d in sched["days"] for d in next_week):
            events.append(("%s/need-schedule" % day.isoformat(), at(day, WEEK_CHECK),
                           "📅 На сайте УлГТУ пока нет расписания на следующую неделю "
                           "(или его не удалось скачать). Проверь вручную на time.ulstu.ru."))
    return events


# ---------- Telegram ----------

def tg(token, method, **params):
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params).encode() if params else None
    with urllib.request.urlopen(url, data=data, timeout=20) as r:
        return json.load(r)


def send(env, text):
    tg(env["TELEGRAM_TOKEN"], "sendMessage", chat_id=env["CHAT_ID"], text=text)


# ---------- команды ----------

def cmd_setup():
    env = load_env()
    token = env.get("TELEGRAM_TOKEN", "")
    if not token or token.startswith("ВСТАВЬ"):
        sys.exit("Сначала вставь токен бота в файл .env (строка TELEGRAM_TOKEN=...)")
    upd = tg(token, "getUpdates").get("result", [])
    chats = [u["message"]["chat"] for u in upd if "message" in u]
    if not chats:
        sys.exit("Бот пока не получил ни одного сообщения. Напиши своему боту /start в Telegram и запусти setup ещё раз.")
    chat = chats[-1]
    save_env_value("CHAT_ID", chat["id"])
    env["CHAT_ID"] = str(chat["id"])
    send(env, "✅ Бот напоминаний о парах подключён!")
    print("Готово: chat_id сохранён, тестовое сообщение отправлено (%s)." % chat.get("first_name", chat["id"]))


def cmd_run(now=None):
    env = load_env()
    if not env.get("TELEGRAM_TOKEN") or not env.get("CHAT_ID"):
        sys.exit("Нет TELEGRAM_TOKEN или CHAT_ID в .env — запусти: python3 reminder.py setup")
    now = now or datetime.now()
    sent = load_state()
    sched = load_schedule()
    for ev_id, when, text in events_for(sched, now.date()):
        if ev_id in sent:
            continue
        if when <= now < when + timedelta(minutes=GRACE_MIN):
            try:
                send(env, text)
                sent.add(ev_id)
            except Exception as e:  # сеть упала — попробуем через минуту
                print("Ошибка отправки %s: %s" % (ev_id, e), file=sys.stderr)
    save_state(sent)


def cmd_preview(day_str):
    sched = load_schedule()
    day = date.fromisoformat(day_str)
    for _, when, text in sorted(events_for(sched, day), key=lambda e: e[1]):
        print("──── %s ────\n%s\n" % (when.strftime("%d.%m %H:%M"), text))


def cmd_analyze():
    sched = load_schedule()
    for d in sorted(sched["days"]):
        day = date.fromisoformat(d)
        classes = classes_for(sched, day)
        if not classes:
            print("%s: пар нет" % day_title(day))
            continue
        span = classes[-1]["end"] - classes[0]["start"]
        print("%s: %d пар, %s–%s (на учёбе %s)" % (
            day_title(day), len(classes), classes[0]["start"].strftime("%H:%M"),
            classes[-1]["end"].strftime("%H:%M"), fmt_minutes(span.total_seconds() / 60)))
        for prev, nxt in zip(classes, classes[1:]):
            gap = (nxt["start"] - prev["end"]).total_seconds() / 60
            b1, b2 = building(prev["room"]), building(nxt["room"])
            note = "смена корпуса %s→%s" % (b1, b2) if b1 != b2 else "тот же корпус"
            print("   перерыв %s: %s → %s (%s)" % (fmt_minutes(gap), prev["room"], nxt["room"], note))


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        cmd_run()
    elif args[0] == "setup":
        cmd_setup()
    elif args[0] == "analyze":
        cmd_analyze()
    elif args[0] == "preview" and len(args) > 1:
        cmd_preview(args[1])
    else:
        print(__doc__)
