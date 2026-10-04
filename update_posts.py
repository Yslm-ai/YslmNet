#!/usr/bin/env python3
"""Забирает посты канала через Telegram-бота, хранит последние N в posts.md
и (опционально) обновляет этот файл в репозитории на GitVerse.

Подготовка (один раз):
  1. В Telegram откройте @BotFather -> /newbot -> получите токен.
  2. Добавьте бота в свой канал АДМИНИСТРАТОРОМ.
  3. Положите токен в .env (см. ..env) или в переменную TG_BOT_TOKEN.
  4. Для синхронизации с GitVerse:
     - Создайте токен с правами «Запись» в настройках GitVerse
       (Профиль → Настройки → Управление токенами → отметьте «Репозитории»).
     - Укажите репозиторий, ветку и токен через переменные окружения
       или аргументы командной строки.

Запуск:
    python update_posts.py              # забрать новые посты и обновить posts.md
    python update_posts.py --watch      # работать постоянно и обновлять файл сразу
    python update_posts.py --gitverse-owner alice --gitverse-repo my-project \
        --gitverse-token <TOKEN>        # дополнительно запушить в GitVerse

ВАЖНО про Bot API: бот видит только посты, опубликованные ПОСЛЕ его добавления
в канал, а Telegram хранит необработанные обновления не дольше 24 часов.
Поэтому запускайте скрипт хотя бы раз в сутки или держите --watch.
Токен никогда не пишите в код и не коммитьте .env.
"""
import argparse, base64, json, os, re, sys, time
import urllib.error, urllib.request
from datetime import datetime, timezone

HEADER = ("<!-- Файл создаётся скриптом update_posts.py. "
          "Посты разделены строкой ---, править вручную не нужно. -->\n\n")
MARK = re.compile(r"<!--\s*post:(\d+)\s*-->")
ALLOWED = ["channel_post", "edited_channel_post"]

GITVERSE_API = "https://api.gitverse.ru"


# ---------- .env ----------
def load_dotenv(path=".env"):
    if not os.path.isfile(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


# ---------- Telegram Bot API ----------
class TgError(Exception):
    pass


def api(token, method, **params):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=params.get("timeout", 0) + 30) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        try:
            data = json.load(e)
        except Exception:
            raise TgError(f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise TgError(f"нет соединения с Telegram: {e.reason}")
    if not data.get("ok"):
        code = data.get("error_code")
        desc = data.get("description", "")
        if code == 401:
            raise TgError("токен не принят (401). Проверьте TG_BOT_TOKEN.")
        if code == 409:
            raise TgError("у бота включён webhook, getUpdates не работает. Отключите его: "
                          "откройте в браузере https://api.telegram.org/bot<ТОКЕН>/deleteWebhook")
        raise TgError(f"{code}: {desc}")
    return data["result"]


# ---------- GitVerse API ----------
class GitVerseError(Exception):
    pass


def gitverse_request(token, method, path, body=None):
    """Универсальный запрос к GitVerse API."""
    url = f"{GITVERSE_API}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.gitverse.object+json;version=1",
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode("utf-8") if body is not None else None

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.load(e)
        except Exception:
            err = {}
        code = e.code
        msg = err.get("message", err.get("error", str(e)))
        if code == 401:
            raise GitVerseError("Токен GitVerse не принят (401). Проверьте GITVERSE_TOKEN.")
        if code == 403:
            raise GitVerseError("Нет прав на запись в репозиторий (403).")
        if code == 404:
            raise GitVerseError("Репозиторий, ветка или файл не найдены (404).")
        if code == 409:
            raise GitVerseError("SHA не совпадает с текущей версией файла (409). "
                                "Возможно, файл был изменён извне.")
        raise GitVerseError(f"GitVerse API {code}: {msg}")
    except urllib.error.URLError as e:
        raise GitVerseError(f"нет соединения с GitVerse: {e.reason}")


def gitverse_get_file_sha(token, owner, repo, path, branch=None):
    """Возвращает SHA файла в репозитории или None, если файл не найден."""
    query = f"?ref={branch}" if branch else ""
    try:
        data = gitverse_request(
            token, "GET",
            f"/repos/{owner}/{repo}/contents/{path}{query}"
        )
        return data.get("sha")
    except GitVerseError as e:
        if "404" in str(e):
            return None
        raise


def gitverse_update_file(token, owner, repo, path, content, message, branch=None, sha=None):
    """Создаёт или обновляет файл в репозитории GitVerse."""
    body = {
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "message": message,
    }
    if branch:
        body["branch"] = branch
    if sha:
        body["sha"] = sha

    return gitverse_request(
        token, "PUT",
        f"/repos/{owner}/{repo}/contents/{path}",
        body=body
    )


# ---------- сущности Telegram -> Markdown ----------
def esc(s):
    return re.sub(r"([\\*`\[\]])", r"\\\1", s)


def to_markdown(text, entities):
    """Смещения entities в Telegram считаются в UTF-16, поэтому считаем позиции так же."""
    ents = []
    for e in entities or []:
        s, ln, t = e["offset"], e["length"], e["type"]
        if t == "bold":
            ents.append((s, s + ln, "**", "**", False))
        elif t == "italic":
            ents.append((s, s + ln, "*", "*", False))
        elif t == "code":
            ents.append((s, s + ln, "`", "`", True))
        elif t == "pre":
            ents.append((s, s + ln, "\n```\n", "\n```\n", True))
        elif t == "text_link" and re.match(r"https?://[^\s)]+$", e.get("url", "")):
            ents.append((s, s + ln, "[", f"]({e['url']})", False))

    out, pos, in_code = [], 0, [0]
    total = len(text.encode("utf-16-le")) // 2

    def events_at(p):
        for en in sorted((x for x in ents if x[1] == p), key=lambda x: -x[0]):
            out.append(en[3])
            if en[4]:
                in_code[0] -= 1
        for en in sorted((x for x in ents if x[0] == p), key=lambda x: -x[1]):
            out.append(en[2])
            if en[4]:
                in_code[0] += 1

    for ch in text:
        events_at(pos)
        out.append(ch if in_code[0] else esc(ch))
        pos += len(ch.encode("utf-16-le")) // 2
    events_at(total)
    md = "".join(out)
    md = re.sub(r"\*\*(\s*)\*\*", r"\1", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


# ---------- posts.md ----------
def load_existing(path):
    if not os.path.isfile(path):
        return {}
    raw = open(path, encoding="utf-8-sig").read().replace("\r\n", "\n")
    parts = MARK.split(raw)
    out = {}
    for i in range(1, len(parts) - 1, 2):
        out[int(parts[i])] = re.sub(r"\n---\s*$", "", parts[i + 1].strip()).strip()
    return out


def save(path, blocks, limit):
    ids = sorted(blocks, reverse=True)[:limit]
    body = "".join(f"<!-- post:{i} -->\n{blocks[i]}\n\n---\n\n" for i in ids)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(HEADER + body)
    return len(ids)


def post_link(chat, mid):
    if chat.get("username"):
        return f"https://t.me/{chat['username']}/{mid}"
    cid = str(chat.get("id", ""))
    return f"https://t.me/c/{cid[4:] if cid.startswith('-100') else cid.lstrip('-')}/{mid}"


def same_channel(chat, wanted):
    if not wanted:
        return True
    w = wanted.lstrip("@").lower()
    return str(chat.get("id")) == w or (chat.get("username") or "").lower() == w


def make_block(msg):
    text = msg.get("text")
    ents = msg.get("entities")
    if text is None:
        text, ents = msg.get("caption"), msg.get("caption_entities")
    if not text or not text.strip():
        return None
    date = datetime.fromtimestamp(msg["date"], timezone.utc).isoformat()
    md = to_markdown(text, ents)
    return f"### {date}\n\n{md}\n\n[Открыть в Telegram]({post_link(msg['chat'], msg['message_id'])})"


# ---------- GitVerse sync ----------
def sync_to_gitverse(args, local_path):
    """Читает локальный posts.md и обновляет его в репозитории GitVerse."""
    if not (args.gitverse_token and args.gitverse_owner and args.gitverse_repo):
        return

    if not os.path.isfile(local_path):
        print("Локальный posts.md не найден, синхронизация GitVerse пропущена.")
        return

    with open(local_path, encoding="utf-8") as f:
        content = f.read()

    try:
        sha = gitverse_get_file_sha(
            args.gitverse_token,
            args.gitverse_owner,
            args.gitverse_repo,
            args.gitverse_path,
            args.gitverse_branch,
        )
    except GitVerseError as e:
        print(f"Не удалось получить SHA файла на GitVerse: {e}")
        return

    if sha:
        # Проверяем, изменилось ли содержимое: сравниваем с удалённой версией
        try:
            remote = gitverse_request(
                args.gitverse_token, "GET",
                f"/repos/{args.gitverse_owner}/{args.gitverse_repo}/contents/{args.gitverse_path}"
                + (f"?ref={args.gitverse_branch}" if args.gitverse_branch else "")
            )
            remote_content = base64.b64decode(remote.get("content", "")).decode("utf-8")
            if remote_content == content:
                print("posts.md на GitVerse уже актуален, пропускаем.")
                return
        except Exception:
            pass  # если не удалось прочитать — просто обновляем

    try:
        gitverse_update_file(
            args.gitverse_token,
            args.gitverse_owner,
            args.gitverse_repo,
            args.gitverse_path,
            content,
            message=f"Update posts.md via update_posts.py ({datetime.now(timezone.utc).isoformat()})",
            branch=args.gitverse_branch,
            sha=sha,
        )
        print(f"posts.md успешно обновлён в {args.gitverse_owner}/{args.gitverse_repo}.")
    except GitVerseError as e:
        print(f"Ошибка при обновлении файла на GitVerse: {e}")


# ---------- основной цикл ----------
def process(token, args, timeout=0):
    """Забирает обновления, пишет posts.md, подтверждает обработанные."""
    blocks = load_existing(args.out)
    changed, last_id, offset = 0, None, None
    while True:
        params = {"limit": 100, "timeout": timeout, "allowed_updates": ALLOWED}
        if offset is not None:
            params["offset"] = offset
        updates = api(token, "getUpdates", **params)
        for u in updates:
            last_id = u["update_id"]
            msg = u.get("channel_post") or u.get("edited_channel_post")
            if not msg or not same_channel(msg["chat"], args.channel):
                continue
            block = make_block(msg)
            if block and blocks.get(msg["message_id"]) != block:
                blocks[msg["message_id"]] = block
                changed += 1
        if len(updates) < 100:
            break
        offset = last_id + 1

    if changed or not os.path.isfile(args.out):
        n = save(args.out, blocks, args.limit)
        print(f"Обновлено постов: {changed}. Всего в {args.out}: {n}")
        sync_to_gitverse(args, args.out)
    else:
        # даже если новых постов нет, но файл отсутствует на GitVerse — создадим
        if args.gitverse_token and args.gitverse_owner and args.gitverse_repo:
            sync_to_gitverse(args, args.out)

    if last_id is not None:
        api(token, "getUpdates", offset=last_id + 1, limit=1, timeout=0,
            allowed_updates=ALLOWED)
    return changed


def main():
    load_dotenv()
    ap = argparse.ArgumentParser(description="Посты канала через Telegram-бота -> posts.md (+ GitVerse)")
    ap.add_argument("-o", "--out", default="posts.md", help="выходной .md файл")
    ap.add_argument("-n", "--limit", type=int, default=10,
                    help="сколько последних постов хранить (по умолчанию 10)")
    ap.add_argument("-c", "--channel", default=os.environ.get("TG_CHANNEL", ""),
                    help="@username или id канала (по умолчанию TG_CHANNEL; пусто — любой канал бота)")
    ap.add_argument("--watch", action="store_true", help="работать постоянно (long polling)")

    # GitVerse
    gv = ap.add_argument_group("GitVerse")
    gv.add_argument("--gitverse-owner", default=os.environ.get("GITVERSE_OWNER", ""),
                    help="владелец репозитория GitVerse (по умолчанию GITVERSE_OWNER)")
    gv.add_argument("--gitverse-repo", default=os.environ.get("GITVERSE_REPO", ""),
                    help="название репозитория GitVerse (по умолчанию GITVERSE_REPO)")
    gv.add_argument("--gitverse-token", default=os.environ.get("GITVERSE_TOKEN", ""),
                    help="токен GitVerse с правами записи (по умолчанию GITVERSE_TOKEN)")
    gv.add_argument("--gitverse-branch", default=os.environ.get("GITVERSE_BRANCH", "main"),
                    help="ветка GitVerse (по умолчанию main)")
    gv.add_argument("--gitverse-path", default=os.environ.get("GITVERSE_PATH", "posts.md"),
                    help="путь к файлу в репозитории (по умолчанию posts.md)")

    args = ap.parse_args()

    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    if not token:
        sys.exit("Не задан токен: добавьте TG_BOT_TOKEN в .env или в переменные окружения.")

    try:
        me = api(token, "getMe")
        gv_info = ""
        if args.gitverse_token and args.gitverse_owner and args.gitverse_repo:
            gv_info = f"; GitVerse: {args.gitverse_owner}/{args.gitverse_repo} ({args.gitverse_branch})"
        print(f"Бот: @{me.get('username')}; канал: {args.channel or 'любой'}; "
              f"хранить: {args.limit}{gv_info}")

        if not args.watch:
            if process(token, args) == 0:
                print("Новых постов нет.")
            return

        print("Режим --watch, остановка: Ctrl+C")
        while True:
            try:
                process(token, args, timeout=50)
            except TgError as e:
                print(f"Ошибка: {e}. Повтор через 15 с.")
                time.sleep(15)
    except TgError as e:
        sys.exit(f"Ошибка Telegram: {e}")
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()