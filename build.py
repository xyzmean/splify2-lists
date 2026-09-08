#!/usr/bin/env python3
"""Собирает lists.json — единственный файл, который этот репозиторий отдаёт роутеру.

ЧТО ЗДЕСЬ ПРОИСХОДИТ, В ТРЁХ АБЗАЦАХ.

Первое: свои списки. Каждый файл в lists/ — это один список. `.lst` берётся как есть
(домены и подсети вперемешку — splify2 разбирает сам), `.asn` превращается в подсети:
сборка спрашивает у RIPEstat, что анонсирует названная автономная система, и кладёт ответ
рядом файлом `<имя>.asn.lst`. Оба вида попадают в lists.json ссылкой на raw-файл этого
репозитория.

Второе: чужие списки. sources.txt называет репозитории, из ПОСЛЕДНЕГО релиза которых
берутся наборы `.srs` (sing-box). К себе мы их не перекладываем: в lists.json уходит прямая
ссылка на файл релиза с тем тегом, который был последним в момент сборки. Читать `.srs`
splify2 умеет сам (`steer srs-read` раскладывает набор на домены и подсети).

Третье: формат. lists.json — это тот же формат, который splify2 читает у своего манифеста:
`categories` (списки подсетей) и `domain_lists` (списки доменов), связанные через
`same_as_ip`, когда обе половины пришли из одного источника. Новых полей ровно три —
`format`, `url` и `tag`, — и нужны они только чужим наборам: «скачай вот это, разложи как
srs, возьми свою половину».

Зависимостей нет: только стандартная библиотека Python 3.8+. Запуск: `python3 build.py`.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
LISTS_DIR = os.path.join(ROOT, "lists")
SOURCES = os.path.join(ROOT, "sources.txt")
OUT = os.path.join(ROOT, "lists.json")

# Владелец репозитория подставляется из окружения Actions, чтобы форк работал БЕЗ единой
# правки: у форка свой base_url, и списки поедут из его дерева, а не из чужого.
REPO = os.environ.get("GITHUB_REPOSITORY", "xyzmean/splify2-lists")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
BASE_URL = "https://raw.githubusercontent.com/%s/%s/lists" % (REPO, BRANCH)

UA = {"User-Agent": "splify2-lists build"}

# Человеческие названия для тех чужих наборов, которые мы знаем в лицо. Всё, чего здесь
# нет, называется по имени файла — это не беда, а честность: придумывать перевод за
# издателя мы не вправе, а увидев английское имя, человек его узнает.
TITLES = {
    "anime": "Аниме",
    "block": "Заблокированное в РФ",
    "cloudflare": "Cloudflare",
    "cloudfront": "Amazon CloudFront",
    "digitalocean": "DigitalOcean",
    "discord": "Discord",
    "geoblock": "Геоблокировка (не пускают из РФ)",
    "google_ai": "Google AI",
    "google_meet": "Google Meet",
    "google_play": "Google Play",
    "hdrezka": "HDRezka",
    "hetzner": "Hetzner",
    "hodca": "H.O.D.C.A (Hetzner, OVH, DigitalOcean, Cloudflare, AWS, Akamai)",
    "meta": "Meta (Facebook, Instagram)",
    "news": "Новости",
    "ovh": "OVH",
    "porn": "Взрослое",
    "roblox": "Roblox",
    "russia_inside": "Россия: изнутри (сборный)",
    "russia_outside": "Россия: снаружи (сборный)",
    "telegram": "Telegram",
    "tiktok": "TikTok",
    "twitter": "Twitter (X)",
    "ukraine_inside": "Украина: заблокированное",
    "youtube": "YouTube",
    "adobe": "Adobe",
    "anthropic": "Anthropic (Claude)",
    "apple": "Apple",
    "blizzard": "Blizzard",
    "bungie": "Bungie (Destiny)",
    "ccp": "CCP (EVE Online)",
    "electronicarts": "Electronic Arts",
    "epicgames": "Epic Games",
    "google": "Google",
    "nintendo": "Nintendo",
    "play2go": "Play2Go",
    "riot": "Riot Games",
    "sony": "Sony (PlayStation)",
    "taketwo": "Take-Two (Rockstar)",
    "ubisoft": "Ubisoft",
    "valve": "Valve (Steam)",
    "wargaming": "Wargaming",
    "xbox": "Xbox",
}


def say(msg):
    print(msg, flush=True)


def get(url, headers=None, tries=3):
    """GET с повторами. Сеть чужая, и один отказ не повод ронять сборку целиком."""
    last = None
    for n in range(tries):
        try:
            req = urllib.request.Request(url, headers=dict(UA, **(headers or {})))
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001 — причина уходит человеку строкой ниже
            last = e
            time.sleep(2 * (n + 1))
    raise RuntimeError("%s: %s" % (url, last))


def gh_json(path):
    """Запрос к API GitHub. Токен берётся из окружения, если он есть: без него лимит
    шестьдесят запросов в час на адрес, и на общем раннере он выбирается чужими сборками."""
    headers = {"Accept": "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        headers["Authorization"] = "Bearer " + tok
    return json.loads(get("https://api.github.com" + path, headers))


# ---- свои списки ---------------------------------------------------------------------

HEAD_RE = re.compile(r"^#\s*([a-z_]+)\s*:\s*(.+?)\s*$")
# Адресная строка: IPv4 с маской или без, диапазон. Ровно то же различение, что у движка
# (spec_line_is_addr): доменные строки берёт резолвер, адресные — компилятор набора.
ADDR_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?$|^\d{1,3}(\.\d{1,3}){3}-\d{1,3}(\.\d{1,3}){3}$")


def read_head(lines):
    """Шапка файла: строки-комментарии вида `# ключ: значение` до первой записи."""
    head = {}
    for ln in lines:
        s = ln.strip()
        if not s:
            continue
        if not s.startswith("#"):
            break
        m = HEAD_RE.match(s)
        if m:
            head[m.group(1)] = m.group(2)
    return head


def read_body(lines):
    """Записи файла: без комментариев и пустых строк."""
    return [ln.strip() for ln in lines if ln.strip() and not ln.strip().startswith("#")]


def asn_prefixes(asns):
    """Подсети, которые анонсирует автономная система. RIPEstat, потому что он отвечает
    без ключа и без регистрации; отказ по одному номеру не роняет остальные."""
    out = []
    for asn in asns:
        num = asn.upper().replace("AS", "").strip()
        if not num.isdigit():
            say("    номер AS не разобран: %s" % asn)
            continue
        try:
            d = json.loads(get(
                "https://stat.ripe.net/data/announced-prefixes/data.json?resource=AS" + num))
        except Exception as e:  # noqa: BLE001
            say("    AS%s: %s" % (num, e))
            continue
        for p in d.get("data", {}).get("prefixes", []):
            pref = p.get("prefix", "")
            if ":" in pref:      # IPv6 движок в наборах пока не держит
                continue
            if pref:
                out.append(pref)
    return sorted(set(out))


def own_lists():
    """Списки этого репозитория: `.lst` как есть, `.asn` — через RIPEstat в `.asn.lst`."""
    cats, doms = [], []
    if not os.path.isdir(LISTS_DIR):
        return cats, doms
    for fn in sorted(os.listdir(LISTS_DIR)):
        path = os.path.join(LISTS_DIR, fn)
        if not os.path.isfile(path) or fn.endswith(".md"):
            continue
        # Сгенерированное из .asn пропускаем: оно попадёт в lists.json от своего .asn.
        if fn.endswith(".asn.lst"):
            continue
        base, ext = os.path.splitext(fn)
        if ext not in (".lst", ".asn"):
            continue
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        head = read_head(lines)
        body = read_body(lines)
        name = head.get("name") or base
        about = head.get("about")
        on = str(head.get("on", "no")).lower() in ("yes", "true", "1", "on")

        if ext == ".asn":
            say("  %s: спрашиваю подсети у RIPEstat" % fn)
            prefixes = asn_prefixes(body)
            gen = base + ".asn.lst"
            with open(os.path.join(LISTS_DIR, gen), "w", encoding="utf-8") as f:
                f.write("# Собрано из %s — правьте ЕГО, а не этот файл.\n" % fn)
                for p in prefixes:
                    f.write(p + "\n")
            say("    подсетей: %d" % len(prefixes))
            if prefixes:
                cats.append(entry(base, name, about, on, gen, len(prefixes)))
            continue

        addrs = [x for x in body if ADDR_RE.match(x)]
        names = [x for x in body if not ADDR_RE.match(x)]
        if addrs:
            cats.append(entry(base, name, about, on, fn, len(addrs)))
        if names:
            d = entry(base, name, about, on, fn, len(names))
            d["kind"] = "domains"
            if addrs:
                # Обе половины одного списка — один сервис в интерфейсе splify2.
                d["id"] = "svc_" + base
                d["same_as_ip"] = [base]
            doms.append(d)
        if not addrs and not names:
            say("  %s: ни одной записи — пропускаю" % fn)
    return cats, doms


def entry(lid, name, about, on, file, count):
    e = {"id": lid, "name_ru": name, "file": file, "count": count, "default_on": on}
    if about:
        e["description_ru"] = about
    return e


# ---- чужие релизы --------------------------------------------------------------------

def read_sources():
    out = []
    if not os.path.exists(SOURCES):
        return out
    for ln in open(SOURCES, encoding="utf-8").read().splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        parts = [p.strip() for p in s.split("|")]
        repo = parts[0]
        prefix = parts[1] if len(parts) > 1 and parts[1] else repo.split("/")[0]
        title = parts[2] if len(parts) > 2 and parts[2] else repo
        out.append((repo, prefix, title))
    return out


def upstream_lists(repo, prefix, title):
    """Наборы .srs из последнего релиза чужого репозитория.

    ТЕГ ЗАПИСЫВАЕТСЯ В ССЫЛКУ, а не берётся как `latest`: две сборки одного lists.json
    обязаны означать одно и то же, иначе «у меня вчера работало» не с чем сравнить.

    ЕСТЬ ЛИ У НАБОРА ПОДСЕТИ — подсказка, а не истина. У itdoginfo рядом с `.srs` лежат
    `.mrs` для mihomo, и по наличию `<имя>_ipcidr.mrs` видно, что подсети в наборе есть.
    Где `.mrs` нет вовсе (другой издатель), объявляем обе половины: пустую splify2 назовёт
    сам, разобрав набор движком, — а промолчать о существующей половине хуже.
    """
    rel = gh_json("/repos/%s/releases/latest" % repo)
    tag = rel.get("tag_name", "")
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
    has_mrs = any(n.endswith(".mrs") for n in assets)
    cats, doms = [], []
    for aname in sorted(assets):
        if not aname.endswith(".srs"):
            continue
        base = aname[:-4]
        lid = "%s:%s" % (prefix, base)
        name = TITLES.get(base, base.replace("_", " ").title())
        url = assets[aname]
        want_pref = (not has_mrs) or ("%s_ipcidr.mrs" % base in assets)
        common = {
            "format": "srs", "url": url, "tag": tag,
            "source": repo, "source_name": title, "default_on": False,
        }
        if want_pref:
            c = {"id": lid, "name_ru": name, "file": "%s/%s.lst" % (prefix, base)}
            c.update(common)
            cats.append(c)
        d = {"id": ("svc_%s_%s" % (prefix, base)) if want_pref else lid,
             "kind": "domains", "name_ru": name,
             "file": "%s/domains/%s.lst" % (prefix, base)}
        d.update(common)
        if want_pref:
            d["same_as_ip"] = [lid]
        doms.append(d)
    say("  %s: релиз %s, наборов %d" % (repo, tag, len(cats) + len(doms)))
    return cats, doms


# ---- сборка --------------------------------------------------------------------------

def check_ids(cats, doms):
    """Одинаковых имён быть не должно, и молчать про них нельзя.

    Идентификатор списка уезжает в настройку роутера: два списка с одним именем — это два
    разных набора под одной галочкой, и какой из них попадёт в правило, решит порядок в
    файле. Форк, где человек назвал свой список так же, как чужой, обязан узнать об этом
    от сборки, а не от роутера."""
    bad = []
    for kind, items in (("подсетей", cats), ("доменов", doms)):
        seen = set()
        for e in items:
            if e["id"] in seen:
                bad.append("%s: %s" % (kind, e["id"]))
            seen.add(e["id"])
    return bad


def main():
    say("свои списки:")
    cats, doms = own_lists()
    say("чужие релизы:")
    for repo, prefix, title in read_sources():
        try:
            c, d = upstream_lists(repo, prefix, title)
        except Exception as e:  # noqa: BLE001
            # Недоступный источник не роняет сборку: лучше lists.json без него, чем
            # никакого. Молчать при этом нельзя — иначе список исчезнет незаметно.
            say("  %s: НЕ ВЫШЛО — %s" % (repo, e))
            return 1
        cats += c
        doms += d

    bad = check_ids(cats, doms)
    if bad:
        say("одинаковые имена списков — переименуйте файл: %s" % ", ".join(bad))
        return 1

    now = datetime.now(timezone.utc)
    out = {
        "version": now.strftime("%Y-%m-%d_%H-%M"),
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "base_url": BASE_URL,
        "categories": cats,
        "domain_lists": doms,
    }

    # НИЧЕГО НЕ ИЗМЕНИЛОСЬ — НИЧЕГО И НЕ ТРОГАЕМ.
    #
    # Сравнивается СОДЕРЖАНИЕ, без времени сборки и без номера версии: они меняются каждую
    # ночь по построению, и по ним «изменилось» значило бы «сборка была», а не «списки
    # другие». Совпало — файл не переписывается вовсе, значит `git status` чист, значит
    # ночная сборка не делает ни коммита, ни релиза. Это не экономия байтов: релиз,
    # переизданный без изменений, отменяет главное свойство версии — «одна версия, одно
    # содержимое», и человек, у которого «вчера работало», не может сказать, что поменялось.
    #
    # Сами наборы `.srs` мы не качаем ни при каких условиях: у чужого релиза спрашивается
    # только его тег и перечень файлов, а ссылка на файл уезжает в lists.json как есть.
    keep = lambda d: {k: v for k, v in d.items() if k not in ("version", "generated_at")}
    if os.path.exists(OUT):
        try:
            old = json.load(open(OUT, encoding="utf-8"))
        except Exception:  # noqa: BLE001 — испорченный файл нам не судья, пишем новый
            old = None
        if old and keep(old) == keep(out):
            say("изменений нет — lists.json не тронут (%s)" % old.get("version", "?"))
            return 0

    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, sort_keys=False)
        f.write("\n")
    say("lists.json обновлён: подсетей %d, доменов %d" % (len(cats), len(doms)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
