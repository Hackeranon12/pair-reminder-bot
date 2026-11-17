#!/usr/bin/env python3
"""Загрузка расписания с time.ulstu.ru (через вход в личный кабинет УлГТУ).

  python3 fetch_schedule.py ИСТбд-12   — скачать и показать расписание группы
"""
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from reminder import load_env  # noqa: E402

BASE = "https://time.ulstu.ru"
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) pair-reminder"

# Неделя "10" в API = 09.11.2026–15.11.2026, т.е. осенний семестр начался с недели 31.08.2026.
# В другом семестре — запасной вариант: первая неделя в ответе считается текущей.
FALL_ANCHOR = date(2026, 8, 31)


class LoginError(Exception):
    pass


class Session:
    """Сессия на time.ulstu.ru с входом через ЛК."""

    def __init__(self):
        env = load_env()
        self.user, self.password = env.get("LK_LOGIN", ""), env.get("LK_PASSWORD", "")
        if not self.user or self.user.startswith("ВСТАВЬ"):
            raise LoginError("В .env не заполнены LK_LOGIN и LK_PASSWORD")
        jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        self.opener.addheaders = [("User-Agent", UA)]
        self.logged_in = False

    def _open(self, req, tries=3):
        """Запрос с повторами: сайт УлГТУ иногда обрывает соединение."""
        for i in range(tries):
            try:
                r = self.opener.open(req, timeout=20)
                return r, r.read()
            except (urllib.error.URLError, OSError) as e:
                if isinstance(e, urllib.error.HTTPError) and e.code < 500:
                    raise
                if i == tries - 1:
                    raise
                time.sleep(3 * (i + 1))

    def _login(self):
        r, body = self._open(BASE + "/")
        page = body.decode("utf-8", "replace")
        if 'name="password"' in page:
            data = urllib.parse.urlencode({"login": self.user, "password": self.password}).encode()
            r, body = self._open(urllib.request.Request(r.geturl(), data=data))
            page = body.decode("utf-8", "replace")
            if 'name="password"' in page or "lk.ulstu.ru" in urllib.parse.urlparse(r.geturl()).netloc:
                raise LoginError("Не удалось войти в ЛК УлГТУ — проверь LK_LOGIN и LK_PASSWORD в .env")
        self.logged_in = True

    def api(self, path):
        if not self.logged_in:
            self._login()
        r, body = self._open(BASE + path)
        if "json" not in r.headers.get("Content-Type", ""):  # сессия истекла — входим заново
            self._login()
            r, body = self._open(BASE + path)
        return json.loads(body.decode("utf-8"))

    def groups(self):
        return self.api("/api/1.0/groups")["response"]

    def timetable(self, group):
        data = self.api("/api/1.0/timetable?filter=" + urllib.parse.quote(group))
        if not data.get("response") or not data["response"].get("weeks"):
            raise RuntimeError("Пустое расписание для %s: %s" % (group, data.get("error")))
        return convert(data, date.today())


def week_monday(week_no, all_weeks, today):
    if date(2026, 8, 1) <= today <= date(2027, 1, 31):
        return FALL_ANCHOR + timedelta(weeks=week_no)
    monday_now = today - timedelta(days=today.weekday())
    return monday_now + timedelta(weeks=week_no - min(all_weeks))


def parse_lesson(name):
    """'лаб. Компьютерная графика – 2 п/г' → ('лаб.', 'Компьютерная графика', 2)."""
    name = name.replace("e", "е").strip()  # на сайте иногда латинская «e»
    sg = None
    m = re.search(r"\s*[–-]\s*(\d)\s*п/г\s*$", name)
    if m:
        sg, name = int(m.group(1)), name[:m.start()]
    kind, _, subject = name.partition(" ")
    if not kind.endswith("."):
        kind, subject = "", name
    return kind, subject.strip(), sg


def convert(api, today):
    """Ответ API → {"YYYY-MM-DD": [{"pair", "type", "subject", "teacher", "room", "sg"}, ...]}."""
    weeks = {int(k): v for k, v in api["response"]["weeks"].items()}
    days = {}
    for wn, week in weeks.items():
        monday = week_monday(wn, weeks.keys(), today)
        for d in week["days"]:
            items = []
            for i, slot in enumerate(d["lessons"]):
                for les in slot:
                    kind, subject, sg = parse_lesson(les["nameOfLesson"])
                    items.append({"pair": i + 1, "type": kind, "subject": subject,
                                  "teacher": les["teacher"], "room": les["room"], "sg": sg})
            days[(monday + timedelta(days=d["day"])).isoformat()] = items
    return days


def for_subgroup(days, sg):
    """Оставить пары нужной подгруппы (sg=0 — все пары) и слить совпадающие по времени."""
    out = {}
    for day, items in days.items():
        by_pair = {}
        for it in items:
            if sg and it["sg"] and it["sg"] != sg:
                continue
            by_pair.setdefault(it["pair"], []).append(it)
        lessons = []
        for pair in sorted(by_pair):
            group = by_pair[pair]
            first = dict(group[0])
            if first["sg"]:
                first["subject"] += " (%d-я п/г)" % first["sg"]
            if len(group) > 1:  # sg=0: в одно время пары разных подгрупп
                first["subject"] = " / ".join(dict.fromkeys(
                    g["subject"] + (" (%d п/г)" % g["sg"] if g["sg"] else "") for g in group))
                first["teacher"] = " / ".join(dict.fromkeys(g["teacher"] for g in group))
                first["room"] = " / ".join(dict.fromkeys(g["room"] for g in group))
            lessons.append(first)
        out[day] = lessons
    return out


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    try:
        days = Session().timetable(sys.argv[1])
    except LoginError as e:
        sys.exit(str(e))
    for d, items in sorted(days.items()):
        print(d, [(i["pair"], i["subject"], i["room"], i["sg"]) for i in items])
