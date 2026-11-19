import json, os, urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

PLAYER = "Avdija"
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "change-me-to-a-secret-name")
BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
STATUS_FILE = "status.json"
IL = ZoneInfo("Asia/Jerusalem")

# ---- settings you may want to change ----
REPORT_HOUR = 19          # daily report at 19:00 Israel time, the evening before the game
ALARM_TIME = (5, 30)      # time of the "דני" alarm on your iPhone (regular alarm minus 90 min), (hour, minute)
CHECK_BEFORE_ALARM = 20   # minutes. If the game starts after the alarm time, the final check runs this long before the alarm
NOTIFY_ON_CHANGE = False  # True = also notify on every status change between the report and the final check
ISRAEL_CHANNEL = ""       # the TV channel that airs the games for you in Israel, e.g. "ספורט 5". Shown in the 19:00 report if not empty
GUIDE_URL = ""            # optional: a URL that opens when you tap the 19:00 notification (e.g. a TV guide). Empty = no link
# -----------------------------------------


def get(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def notify(title, msg, click=None):
    headers = {"Title": title.encode("utf-8"), "Priority": "high"}
    if click:
        headers["Click"] = click   # tapping the notification opens this URL
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=msg.encode("utf-8"),
        headers=headers,
    )
    urllib.request.urlopen(req, timeout=20)


def opponent_name(ev):
    try:
        for c in ev["competitions"][0]["competitors"]:
            team = c["team"]
            if team.get("abbreviation", "").upper() != "POR":
                return team.get("displayName") or team.get("shortDisplayName")
    except (KeyError, IndexError):
        pass
    return ev.get("name", "?")


def us_broadcasts(ev):
    """US TV / streaming names from ESPN's data (this is NOT the Israeli channel). Handles both data shapes."""
    try:
        comp = ev["competitions"][0]
    except (KeyError, IndexError):
        return ""
    names = []
    for b in comp.get("broadcasts") or []:
        names.extend(b.get("names") or [])
        short = (b.get("media") or {}).get("shortName")
        if short:
            names.append(short)
    for b in comp.get("geoBroadcasts") or []:
        short = (b.get("media") or {}).get("shortName")
        if short:
            names.append(short)
    seen, out = set(), []
    for n in names:
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return ", ".join(out)


def find_next_game(now, ahead_hours=36, back_hours=8):
    data = get(f"{BASE}/teams/por/schedule")
    for ev in data.get("events", []):
        start = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
        if now - timedelta(hours=back_hours) <= start <= now + timedelta(hours=ahead_hours):
            return {
                "id": str(ev.get("id")),
                "name": ev.get("name"),
                "opponent": opponent_name(ev),
                "tv_us": us_broadcasts(ev),
                "start": start,
            }
    return None


def timeline(start):
    """Returns (report_at, check_at, alarm_at), all in Israel time."""
    start_il = start.astimezone(IL)
    report_at = (start_il - timedelta(days=1)).replace(
        hour=REPORT_HOUR, minute=0, second=0, microsecond=0)
    # the first occurrence of the alarm time after (game start - 8h)
    ref = start_il - timedelta(hours=8)
    alarm_at = ref.replace(hour=ALARM_TIME[0], minute=ALARM_TIME[1], second=0, microsecond=0)
    if alarm_at <= ref:
        alarm_at += timedelta(days=1)
    # final check at game time, or shortly before the alarm if the game starts after it
    check_at = min(start_il, alarm_at - timedelta(minutes=CHECK_BEFORE_ALARM))
    return report_at, check_at, alarm_at


def player_status():
    """Injury status text, or None if he is not on the injury list."""
    data = get(f"{BASE}/injuries")
    for team in data.get("injuries", []):
        if "Portland" not in team.get("displayName", ""):
            continue
        for inj in team.get("injuries", []):
            if PLAYER.lower() in inj["athlete"]["displayName"].lower():
                return inj.get("status", "Unknown")
    return None


def participation(event_id):
    """From the live box score: 'played', 'dnp', or None if it is not available yet."""
    try:
        data = get(f"{BASE}/summary?event={event_id}")
    except Exception:
        return None
    for team in data.get("boxscore", {}).get("players", []):
        for grp in team.get("statistics", []):
            for a in grp.get("athletes", []):
                if PLAYER.lower() in a.get("athlete", {}).get("displayName", "").lower():
                    if a.get("didNotPlay"):
                        return "dnp"
                    if a.get("starter") or a.get("stats"):
                        return "played"
    return None


def final_decision(event_id):
    """Returns (state, label): state is 'alarm', 'out' or 'uncertain'."""
    part = participation(event_id)
    if part == "played":
        return "alarm", "בהרכב"
    if part == "dnp":
        return "out", "לא משחק"
    st = player_status()
    if st is None:
        return "alarm", "לא ברשימת הפציעות"
    if st.lower() == "out":
        return "out", st
    return "uncertain", st


def decide(now, game, old):
    """Returns (new_status_dict, [(title, message), ...])."""
    msgs = []
    if not game:
        return {
            "state": "no_game", "alarm": False, "game_id": None, "game": None,
            "opponent": None, "game_start_utc": None, "injury_status": None,
            "report_game": old.get("report_game"), "updated_utc": now.isoformat(),
        }, msgs

    report_at, check_at, alarm_at = timeline(game["start"])
    same = old.get("game_id") == game["id"]
    old_state = old.get("state") if same else None
    status = old.get("injury_status") if same else None
    report_game = old.get("report_game")
    state = "waiting"

    when = game["start"].astimezone(IL).strftime("%H:%M, %d/%m")
    check_txt = check_at.strftime("%H:%M")

    def body(label, extra="", tv=False):
        lines = [f"נגד: {game['opponent']}",
                 f"שעת המשחק (שעון ישראל): {when}"]
        if tv and ISRAEL_CHANNEL:
            lines.append(f"ערוץ בישראל: {ISRAEL_CHANNEL}")
        if tv and game.get("tv_us"):
            lines.append(f'שידור בארה"ב: {game["tv_us"]}')
        lines.append(f"סטטוס דני: {label}{extra}")
        if tv and GUIDE_URL:
            lines.append("לוח השידורים של הוט: לחיצה על ההתראה")
        return "\n".join(lines)

    if now >= alarm_at:
        state = "done"
    elif now >= check_at:
        # final check: runs every time until the alarm time, so a late change is still caught
        state, label = final_decision(game["id"])
        status = label
        if state != old_state:
            if state == "alarm":
                msgs.append(("דני משחק - השעון יופעל", body(label)))
            elif state == "out":
                msgs.append(("דני לא משחק - אין שעון", body(label)))
            else:
                msgs.append(("דני עדיין בסימן שאלה - אין שעון",
                             body(label, "\nממשיך לבדוק עד זמן השעון.")))
    elif now >= report_at and (report_game != game["id"] or NOTIFY_ON_CHANGE):
        st = player_status()
        label = st or "לא ברשימת הפציעות"
        if report_game != game["id"]:
            if st is None:
                title, extra = "דני צפוי לשחק", f"\nבדיקה סופית בשעת המשחק ({check_txt})."
            elif st.lower() == "out":
                title, extra = "דני לא משחק", f"\nבדיקה סופית בשעת המשחק ({check_txt})."
            else:
                title, extra = "דני בסימן שאלה", f"\nלא הוגדר שעון. בדיקה סופית בשעת המשחק ({check_txt})."
            msgs.append((title, body(label, extra, tv=True), GUIDE_URL or None))
            report_game = game["id"]
        elif label != status:
            msgs.append(("דני - שינוי בסטטוס", body(label)))
        status = label

    new = {
        "state": state,
        "alarm": state == "alarm",
        "game_id": game["id"],
        "game": game["name"],
        "opponent": game["opponent"],
        "game_start_utc": game["start"].isoformat(),
        "final_check_il": check_at.isoformat(),
        "alarm_at_il": alarm_at.isoformat(),
        "injury_status": status,
        "report_game": report_game,
        "updated_utc": now.isoformat(),
    }
    return new, msgs


def main(now=None):
    now = now or datetime.now(timezone.utc)
    old = json.load(open(STATUS_FILE)) if os.path.exists(STATUS_FILE) else {}
    try:
        game = find_next_game(now)
        new, msgs = decide(now, game, old)
    except Exception as e:
        print("check failed, keeping the previous state:", e)
        return

    for item in msgs:          # (title, message) or (title, message, click_url)
        notify(*item)

    # write the file only when something changed, so the repo does not get a commit every run
    def core(d):
        return {k: v for k, v in d.items() if k != "updated_utc"}
    if core(new) != core(old):
        json.dump(new, open(STATUS_FILE, "w"), ensure_ascii=False, indent=2)
    print(json.dumps(core(new), ensure_ascii=False))


if __name__ == "__main__":
    main()
