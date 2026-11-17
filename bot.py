#!/usr/bin/env python3
"""Telegram-бот напоминаний о парах УлГТУ для всех желающих.

Работает постоянно: отвечает на сообщения и раз в минуту рассылает напоминания.
  python3 bot.py        — запустить бота (так его запускает launchd)
"""
import json
import os
import sys
import time
import traceback
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fetch_schedule  # noqa: E402
from reminder import (WEEKDAYS, day_plan, day_title, describe, events_for,  # noqa: E402
                      classes_for, lesson_type, load_env, GRACE_MIN)

AUTHOR = "@Aceboynotlove"
USERS_FILE = os.path.join(HERE, "users.json")
SENT_FILE = os.path.join(HERE, ".sent.json")
CACHE_DIR = os.path.join(HERE, "cache")
UPDATE_EVERY_H = 6        # как часто обновлять расписание каждой группы
FAIL_ALERT_H = 24         # если группа не обновляется сутки — сообщить владельцу

PAIRS = {"1": ["08:30", "09:50"], "2": ["10:00", "11:20"], "3": ["11:30", "12:50"],
         "4": ["13:30", "14:50"], "5": ["15:00", "16:20"], "6": ["16:30", "17:50"],
         "7": ["18:00", "19:20"], "8": ["19:30", "20:50"]}

BTN_TODAY, BTN_TOMORROW, BTN_NEXT, BTN_WEEK, BTN_SETTINGS = (
    "📅 Сегодня", "📆 Завтра", "⏭ Следующая пара", "🗓 Неделя", "⚙️ Настройки")
MAIN_KB = {"keyboard": [[BTN_TODAY, BTN_TOMORROW], [BTN_NEXT, BTN_WEEK], [BTN_SETTINGS]],
           "resize_keyboard": True}

WELCOME = (
    "👋 Привет, {name}!\n\n"
    "Я бот-напоминалка о парах УлГТУ 🎓\n\n"
    "Что я умею:\n"
    "☀️ утром присылаю план на день\n"
    "🔔 после каждой пары — какая следующая и в какой аудитории\n"
    "🏃 предупреждаю, если на перемене нужно бежать в другой корпус\n"
    "⏰ напоминаю о паре после обеда или окна\n"
    "🌙 вечером присылаю план на завтра\n\n"
    "Расписание беру с time.ulstu.ru и обновляю сам.\n\n"
    "Создатель бота: " + AUTHOR + "\n\n"
    "Чтобы начать, напиши название своей группы, например: ИСТбд-12"
)


# ---------- хранилище ----------

def read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


class Store:
    def __init__(self, env):
        self.users = read_json(USERS_FILE, None)
        if self.users is None:  # первый запуск: переносим владельца из старой версии
            self.users = {}
            if env.get("CHAT_ID"):
                self.users[env["CHAT_ID"]] = {"group": "ИСТбд-12", "sg": 2, "on": True, "name": ""}
            self.save_users()
        self.sent = set(read_json(SENT_FILE, []))
        os.makedirs(CACHE_DIR, exist_ok=True)

    def save_users(self):
        write_json(USERS_FILE, self.users)

    def save_sent(self):
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        self.sent = {s for s in self.sent if s.split("|", 1)[-1][:10] >= cutoff}
        write_json(SENT_FILE, sorted(self.sent))

    def cache_path(self, group):
        return os.path.join(CACHE_DIR, group.replace("/", "_") + ".json")

    def schedule(self, group):
        return read_json(self.cache_path(group), None)


# ---------- Telegram ----------

class TG:
    def __init__(self, token):
        self.base = "https://api.telegram.org/bot%s/" % token

    def call(self, method, http_timeout=30, **params):
        req = urllib.request.Request(self.base + method, data=json.dumps(params).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=http_timeout) as r:
            return json.load(r).get("result")

    def send(self, chat_id, text, kb=None):
        params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
        if kb:
            params["reply_markup"] = kb
        return self.call("sendMessage", **params)


# ---------- бот ----------

class Bot:
    def __init__(self):
        self.env = load_env()
        self.tg = TG(self.env["TELEGRAM_TOKEN"])
        owners = self.env.get("OWNER_IDS") or self.env.get("CHAT_ID", "")
        self.owners = {x.strip() for x in owners.split(",") if x.strip()}
        self.pending_broadcast = {}  # chat_id владельца → текст рассылки, ждущий подтверждения
        self.store = Store(self.env)
        for u in self.store.users.values():  # кто уже выбрал подгруппу до этой версии — не «новый»
            if u.get("sg") is not None:
                u.setdefault("announced", True)
        self.session = None
        self.groups = []
        self.groups_at = None
        self.last_try = {}   # группа → когда последний раз пытались скачать
        self.fails = {}      # группа → сколько раз подряд не удалось скачать
        self.last_tick = None

    # --- расписание ---

    def uni(self):
        if self.session is None:
            self.session = fetch_schedule.Session()
        return self.session

    def all_groups(self):
        if not self.groups or datetime.now() - self.groups_at > timedelta(hours=24):
            try:
                self.groups = self.uni().groups()
                self.groups_at = datetime.now()
            except Exception:
                self.session = None
                if not self.groups:
                    raise
        return self.groups

    def refresh(self, group, force=False):
        """Скачать расписание группы, если оно устарело. Возвращает True, если данные есть."""
        cached = self.store.schedule(group)
        now = datetime.now()
        if cached and not force and now - datetime.fromisoformat(cached["updated"]) < timedelta(hours=UPDATE_EVERY_H):
            return True
        # повтор: для новой группы без расписания — через 5 мин, иначе через 30 мин
        retry = timedelta(minutes=5 if not cached else 30)
        if not force and now - self.last_try.get(group, datetime(2000, 1, 1)) < retry:
            return cached is not None
        self.last_try[group] = now
        try:
            days = self.uni().timetable(group)
        except Exception as e:
            self.session = None
            self.fails[group] = self.fails.get(group, 0) + 1
            print("[%s] не удалось обновить %s (%d раз подряд): %s" % (now, group, self.fails[group], e),
                  file=sys.stderr)
            if cached:
                stale = now - datetime.fromisoformat(cached["updated"]) > timedelta(hours=FAIL_ALERT_H)
            else:
                stale = self.fails[group] >= 6  # ~30 минут неудач подряд
            for owner in self.owners if stale else ():
                alert = "%s|%s/update-failed-%s" % (owner, now.date().isoformat(), group)
                if alert not in self.store.sent:
                    self.safe_send(owner, "⚠️ Не получается обновить расписание %s с сайта УлГТУ:\n%s" % (group, e))
                    self.store.sent.add(alert)
            return cached is not None
        self.fails.pop(group, None)
        old = cached["days"] if cached else {}
        if old:
            try:
                self.notify_changes(group, old, days, now)
            except Exception:
                traceback.print_exc()
        cutoff = (date.today() - timedelta(days=14)).isoformat()
        merged = {d: v for d, v in old.items() if d >= cutoff}
        merged.update(days)
        write_json(self.store.cache_path(group),
                   {"group": group, "updated": now.isoformat(timespec="minutes"), "days": merged})
        return True

    def notify_changes(self, group, old, new, now):
        """Сообщить пользователям группы о переносах, отменах и новых парах на ближайшую неделю."""
        today = now.date()
        days = sorted(d for d in old if d in new and today.isoformat() <= d <= (today + timedelta(days=7)).isoformat())
        if not days:
            return
        fields = (("type", "тип"), ("subject", "предмет"), ("teacher", "преподаватель"), ("room", "аудитория"))
        for chat_id, u in self.store.users.items():
            if u.get("group") != group or not u.get("on") or u.get("sg") is None:
                continue
            old_v = fetch_schedule.for_subgroup({d: old[d] for d in days}, u["sg"])
            new_v = fetch_schedule.for_subgroup({d: new[d] for d in days}, u["sg"])
            parts = []
            for d in days:
                day = date.fromisoformat(d)
                o = {c["pair"]: c for c in old_v[d]}
                n = {c["pair"]: c for c in new_v[d]}
                lines = []
                for p in sorted(set(o) | set(n)):
                    start, end = PAIRS[str(p)]
                    if d == today.isoformat() and end <= now.strftime("%H:%M"):
                        continue  # пара уже прошла
                    head = "%d) %s" % (p, start)
                    if p not in n:
                        lines.append("❌ Отменена: %s %s: %s" % (head, lesson_type(o[p]), o[p]["subject"]))
                    elif p not in o:
                        lines.append("➕ Добавлена: %s %s: %s — ауд. %s" % (
                            head, lesson_type(n[p]), n[p]["subject"], n[p]["room"]))
                    else:
                        for key, label in fields:
                            if o[p][key] != n[p][key]:
                                lines.append("🔄 %s %s: %s %s → %s" % (
                                    head, n[p]["subject"], label, o[p][key], n[p][key]))
                if lines:
                    parts.append("%s\n%s" % (day_title(day), "\n".join(lines)))
            if parts:
                self.safe_send(chat_id, "⚠️ Изменения в расписании %s:\n\n%s" % (group, "\n\n".join(parts)))

    def user_sched(self, u):
        cached = self.store.schedule(u["group"])
        if not cached:
            return None
        return {"pairs": PAIRS, "days": fetch_schedule.for_subgroup(cached["days"], u.get("sg", 0))}

    # --- отправка ---

    def safe_send(self, chat_id, text, kb=None):
        try:
            self.tg.send(chat_id, text, kb)
            return True
        except urllib.error.HTTPError as e:
            if e.code == 403:  # пользователь заблокировал бота
                u = self.store.users.get(str(chat_id))
                if u:
                    u["on"] = False
                    self.store.save_users()
            print("send %s: HTTP %s" % (chat_id, e.code), file=sys.stderr)
        except Exception as e:
            print("send %s: %s" % (chat_id, e), file=sys.stderr)
        return False

    # --- напоминания (раз в минуту) ---

    def tick(self):
        now = datetime.now()
        groups = {u["group"] for u in self.store.users.values() if u.get("group") and u.get("on")}
        for g in groups:
            self.refresh(g)
        for chat_id, u in self.store.users.items():
            if not u.get("on") or not u.get("group") or u.get("sg") is None:
                continue
            sched = self.user_sched(u)
            if not sched:
                continue
            for ev_id, when, text in events_for(sched, now.date()):
                key = "%s|%s" % (chat_id, ev_id)
                if key in self.store.sent:
                    continue
                if when <= now < when + timedelta(minutes=GRACE_MIN):
                    if ev_id.endswith("need-schedule"):
                        text = ("📅 На сайте УлГТУ пока нет расписания %s на следующую неделю. "
                                "Как только оно появится — я его подхвачу." % u["group"])
                    if self.safe_send(chat_id, text):
                        self.store.sent.add(key)
        self.store.save_sent()

    # --- ответы на сообщения ---

    def settings_text(self, u):
        sg = {0: "все пары", 1: "1-я", 2: "2-я"}.get(u.get("sg"), "не выбрана")
        return "⚙️ Настройки\n\nГруппа: %s\nПодгруппа: %s\nНапоминания: %s" % (
            u.get("group") or "не выбрана", sg, "включены ✅" if u.get("on") else "выключены ⛔️")

    def settings_kb(self, u):
        return {"inline_keyboard": [
            [{"text": "Сменить группу", "callback_data": "chgroup"},
             {"text": "Сменить подгруппу", "callback_data": "chsg"}],
            [{"text": "Выключить напоминания" if u.get("on") else "Включить напоминания",
              "callback_data": "toggle"}]]}

    SG_KB = {"inline_keyboard": [[{"text": "1-я", "callback_data": "sg:1"},
                                  {"text": "2-я", "callback_data": "sg:2"}],
                                 [{"text": "Нет подгрупп / все пары", "callback_data": "sg:0"}]]}

    def find_group(self, text):
        q = text.strip().lower().replace(" ", "")
        groups = self.all_groups()
        exact = [g for g in groups if g.lower() == q]
        if exact:
            return exact[0], []
        return None, [g for g in groups if q and q in g.lower()][:8]

    def plan_for(self, u, day):
        sched = self.user_sched(u)
        if not sched:
            return "Расписание группы %s ещё не загрузилось, попробуй через минуту." % u["group"]
        if day.isoformat() not in sched["days"]:
            return "%s: расписания на этот день на сайте пока нет." % day_title(day)
        classes = classes_for(sched, day)
        if not classes:
            return "%s: пар нет 🎉" % day_title(day)
        return "%s — пар: %d\n\n%s" % (day_title(day), len(classes), day_plan(classes))

    def next_class(self, u):
        sched = self.user_sched(u)
        if not sched:
            return "Расписание ещё не загрузилось, попробуй через минуту."
        now = datetime.now()
        for i in range(8):
            day = now.date() + timedelta(days=i)
            for c in classes_for(sched, day):
                if c["end"] > now:
                    if c["start"] <= now:
                        head = "🟢 Сейчас идёт (до %s):" % c["end"].strftime("%H:%M")
                    else:
                        mins = int((c["start"] - now).total_seconds() // 60)
                        when = "через %d ч %d мин" % divmod(mins, 60) if mins >= 60 else "через %d мин" % mins
                        head = "⏭ Следующая пара — %s, %s:" % (day_title(day), when)
                    return "%s\n%s" % (head, describe(c))
        return "В ближайшую неделю пар не найдено."

    def handle_message(self, msg):
        chat_id = str(msg["chat"]["id"])
        text = (msg.get("text") or "").strip()
        name = msg.get("from", {}).get("first_name", "")
        users = self.store.users
        u = users.setdefault(chat_id, {"group": None, "sg": None, "on": True, "name": name,
                                       "joined": date.today().isoformat()})
        u["name"] = name
        if u.get("username") != msg.get("from", {}).get("username"):
            u["username"] = msg.get("from", {}).get("username")
        self.store.save_users()

        if text.startswith("/start"):
            u["on"] = True
            u["awaiting"] = "group" if not u.get("group") else None
            self.store.save_users()
            self.safe_send(chat_id, WELCOME.format(name=name or "студент"),
                           MAIN_KB if u.get("group") else {"remove_keyboard": True})
            if u.get("group"):
                self.safe_send(chat_id, self.settings_text(u), self.settings_kb(u))
            return
        if text in ("/help", "/about"):
            self.safe_send(chat_id, WELCOME.format(name=name or "студент").rsplit("\n\n", 1)[0], MAIN_KB)
            return
        if text == "/stats" and chat_id in self.owners:
            active = [x for x in users.values() if x.get("on") and x.get("group")]
            groups = sorted({x["group"] for x in active})
            self.safe_send(chat_id, "👥 Пользователей: %d (активных %d)\nГруппы: %s" % (
                len(users), len(active), ", ".join(groups) or "—"))
            return
        if text == "/users" and chat_id in self.owners:
            self.send_users(chat_id)
            return
        if text.startswith("/broadcast") and chat_id in self.owners:
            body = text[len("/broadcast"):].strip()
            if not body:
                self.safe_send(chat_id, "Напиши текст после команды, например:\n/broadcast Завтра бот обновится 🚀")
                return
            self.pending_broadcast[chat_id] = body
            n = sum(1 for x in users.values() if x.get("on"))
            kb = {"inline_keyboard": [[{"text": "✅ Отправить", "callback_data": "bc:yes"},
                                       {"text": "❌ Отмена", "callback_data": "bc:no"}]]}
            self.safe_send(chat_id, "📣 Предпросмотр рассылки (получат: %d)\n\n%s" % (n, body), kb)
            return
        if text in ("/settings", BTN_SETTINGS):
            self.safe_send(chat_id, self.settings_text(u), self.settings_kb(u))
            return
        if text == "/group":
            u["awaiting"] = "group"
            self.store.save_users()
            self.safe_send(chat_id, "Напиши название группы, например: ИСТбд-12")
            return

        if u.get("awaiting") == "group" or not u.get("group"):
            try:
                found, similar = self.find_group(text)
            except Exception:
                self.safe_send(chat_id, "Сайт УлГТУ сейчас не отвечает 😕 Попробуй ещё раз через пару минут.")
                return
            if not found:
                if similar:
                    kb = {"inline_keyboard": [[{"text": g, "callback_data": "g:" + g}] for g in similar]}
                    self.safe_send(chat_id, "Не нашёл точно такую группу. Может, одна из этих?", kb)
                else:
                    self.safe_send(chat_id, "Группа «%s» не найдена 🤔 Напиши как на сайте, например: ИСТбд-12" % text)
                return
            self.set_group(chat_id, u, found)
            return

        if u.get("sg") is None:
            self.safe_send(chat_id, "Выбери подгруппу:", self.SG_KB)
            return
        if text in (BTN_TODAY, "/today"):
            self.safe_send(chat_id, self.plan_for(u, date.today()), MAIN_KB)
        elif text in (BTN_TOMORROW, "/tomorrow"):
            self.safe_send(chat_id, self.plan_for(u, date.today() + timedelta(days=1)), MAIN_KB)
        elif text in (BTN_NEXT, "/next"):
            self.safe_send(chat_id, self.next_class(u), MAIN_KB)
        elif text in (BTN_WEEK, "/week"):
            parts = [self.plan_for(u, date.today() + timedelta(days=i)) for i in range(7)]
            self.safe_send(chat_id, "\n\n".join(parts), MAIN_KB)
        else:
            self.safe_send(chat_id, "Не понял 🙂 Пользуйся кнопками внизу.", MAIN_KB)

    @staticmethod
    def who(u):
        name = u.get("name") or ""
        return "@%s (%s)" % (u["username"], name) if u.get("username") else (name or "без имени")

    @staticmethod
    def sg_name(u):
        return {0: "все пары", 1: "1-я п/г", 2: "2-я п/г"}.get(u.get("sg"), "п/г не выбрана")

    def send_users(self, chat_id):
        users = self.store.users
        active = [x for x in users.values() if x.get("on") and x.get("group")]
        lines = ["👥 Всего: %d · активных: %d · групп: %d" % (
            len(users), len(active), len({x["group"] for x in active}))]
        rows = sorted(users.values(), key=lambda x: (x.get("group") or "я", x.get("joined") or ""))
        for x in rows:
            joined = date.fromisoformat(x["joined"]).strftime("%d.%m") if x.get("joined") else "—"
            lines.append("%s %s — %s, %s, с %s" % (
                "✅" if x.get("on") else "⛔️", self.who(x), x.get("group") or "без группы", self.sg_name(x), joined))
        chunk = ""
        for line in lines:  # Telegram: не больше 4096 символов в сообщении
            if len(chunk) + len(line) > 4000:
                self.safe_send(chat_id, chunk)
                chunk = ""
            chunk += line + "\n"
        if chunk:
            self.safe_send(chat_id, chunk)

    def set_group(self, chat_id, u, group):
        u["group"], u["awaiting"], u["sg"] = group, None, None
        self.store.save_users()
        self.safe_send(chat_id, "Группа %s ✅ Загружаю расписание…" % group)
        ok = self.refresh(group, force=True)
        if not ok:
            self.safe_send(chat_id, "Не удалось скачать расписание прямо сейчас, попробую позже.")
        self.safe_send(chat_id, "В какой ты подгруппе?", self.SG_KB)

    def handle_callback(self, cb):
        chat_id = str(cb["message"]["chat"]["id"])
        data = cb.get("data", "")
        u = self.store.users.setdefault(chat_id, {"group": None, "sg": None, "on": True})
        try:
            self.tg.call("answerCallbackQuery", callback_query_id=cb["id"])
        except Exception:
            pass
        if data.startswith("g:"):
            if data[2:] not in self.all_groups():
                return
            self.set_group(chat_id, u, data[2:])
        elif data.startswith("sg:"):
            u["sg"] = int(data[3:])
            u["on"] = True
            new_user = not u.get("announced")
            u["announced"] = True
            self.store.save_users()
            self.safe_send(chat_id, "Готово! 🎉 Теперь я буду присылать напоминания.\n\n" + self.next_class(u), MAIN_KB)
            if new_user:
                for owner in self.owners - {chat_id}:
                    self.safe_send(owner, "🆕 %s — %s, %s. Всего пользователей: %d" % (
                        self.who(u), u["group"], self.sg_name(u), len(self.store.users)))
        elif data in ("bc:yes", "bc:no") and chat_id in self.owners:
            body = self.pending_broadcast.pop(chat_id, None)
            if data == "bc:no" or not body:
                self.safe_send(chat_id, "Рассылка отменена." if data == "bc:no" else "Нет рассылки для отправки.")
                return
            ok = fail = 0
            for cid, x in list(self.store.users.items()):
                if not x.get("on"):
                    continue
                if self.safe_send(cid, body):
                    ok += 1
                else:
                    fail += 1
                time.sleep(0.05)
            self.safe_send(chat_id, "📣 Рассылка: отправлено %d, не доставлено %d" % (ok, fail))
        elif data == "chgroup":
            u["awaiting"] = "group"
            self.store.save_users()
            self.safe_send(chat_id, "Напиши название новой группы, например: ИСТбд-12")
        elif data == "chsg":
            self.safe_send(chat_id, "Выбери подгруппу:", self.SG_KB)
        elif data == "toggle":
            u["on"] = not u.get("on")
            self.store.save_users()
            self.safe_send(chat_id, self.settings_text(u), self.settings_kb(u))

    # --- главный цикл ---

    def run(self):
        try:
            self.tg.call("setMyCommands", commands=[
                {"command": "start", "description": "Начать / приветствие"},
                {"command": "next", "description": "Следующая пара"},
                {"command": "today", "description": "Пары сегодня"},
                {"command": "tomorrow", "description": "Пары завтра"},
                {"command": "week", "description": "Пары на неделю"},
                {"command": "settings", "description": "Группа, подгруппа, вкл/выкл"}])
            self.tg.call("setMyShortDescription",
                         short_description="Напоминания о парах УлГТУ: следующая пара, аудитория, корпус. Автор: " + AUTHOR)
        except Exception as e:
            print("setMyCommands: %s" % e, file=sys.stderr)

        offset = None
        started = time.time()
        while True:
            minute = datetime.now().strftime("%Y%m%d%H%M")
            if minute != self.last_tick:
                self.last_tick = minute
                try:
                    self.tick()
                except Exception:
                    traceback.print_exc()
            try:
                # Короткое ожидание: соединение с Telegram иногда зависает,
                # и бот не должен долго его ждать (раньше было 25 с + 40 с таймаут + 5 с пауза).
                params = {"timeout": 10, "allowed_updates": ["message", "callback_query"]}
                if offset:
                    params["offset"] = offset
                updates = self.tg.call("getUpdates", http_timeout=15, **params) or []
            except Exception as e:
                print("getUpdates: %s" % e, file=sys.stderr)
                if "timed out" not in str(e):  # зависание — переподключаемся сразу
                    time.sleep(5)
                continue
            for upd in updates:
                offset = upd["update_id"] + 1
                try:
                    if "message" in upd:
                        if upd["message"].get("date", 0) < started - 600:
                            continue  # не отвечаем на старые сообщения после долгого простоя
                        self.handle_message(upd["message"])
                    elif "callback_query" in upd:
                        self.handle_callback(upd["callback_query"])
                except Exception:
                    traceback.print_exc()


if __name__ == "__main__":
    Bot().run()
