from __future__ import annotations

import csv
import io
import json
import sqlite3
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path("data/pokewiki.db")
CSV_BASE = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv"
DEFAULT_KO_LANG_ID = "3"
DEFAULT_EN_LANG_ID = "9"
HOST = "0.0.0.0"
PORT = 7860

STAT_ORDER = ["hp", "attack", "defense", "special-attack", "special-defense", "speed"]
STAT_LABELS = {
    "hp": "HP",
    "attack": "공격",
    "defense": "방어",
    "special-attack": "특수공격",
    "special-defense": "특수방어",
    "speed": "스피드",
}

INDEX_HTML = Path("templates/index.html").read_text(encoding="utf-8")
STYLE_CSS = Path("static/style.css").read_text(encoding="utf-8")
APP_JS = Path("static/app.js").read_text(encoding="utf-8")


def safe_int(v: str | None, default: int = 0) -> int:
    try:
        return int(v or default)
    except ValueError:
        return default


def resolve_language_ids(languages: list[dict[str, str]]) -> tuple[str, str]:
    ko = next((r["id"] for r in languages if r.get("identifier") == "ko"), DEFAULT_KO_LANG_ID)
    en = next((r["id"] for r in languages if r.get("identifier") == "en"), DEFAULT_EN_LANG_ID)
    return ko, en


def latest_localized_text(rows: list[dict[str, str]], id_key: str, text_key: str, lang_id: str, order_key: str) -> dict[str, str]:
    out: dict[str, tuple[int, str]] = {}
    for r in rows:
        if r.get("local_language_id") != lang_id:
            continue
        text = (r.get(text_key) or "").replace("\n", " ").strip()
        if not text:
            continue
        oid = safe_int(r.get(order_key), 0)
        key = r[id_key]
        prev = out.get(key)
        if prev is None or oid >= prev[0]:
            out[key] = (oid, text)
    return {k: v[1] for k, v in out.items()}


def fetch_csv(name: str) -> list[dict[str, str]]:
    url = f"{CSV_BASE}/{name}.csv"
    with urllib.request.urlopen(url, timeout=120) as res:
        raw = res.read().decode("utf-8")
    return list(csv.DictReader(io.StringIO(raw)))


def ensure_db() -> None:
    if DB_PATH.exists():
        try:
            con = sqlite3.connect(DB_PATH)
            count = con.execute("SELECT COUNT(*) FROM pokemon").fetchone()[0]
            pokemon_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon)").fetchall()}
            form_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_form_meta)").fetchall()}
            ability_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_ability)").fetchall()}
            level_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_level_move)").fetchall()}
            evo_cols = {r[1] for r in con.execute("PRAGMA table_info(evolution_member)").fetchall()}
            con.close()
            has_new_schema = {
                "display_name_ko",
                "species_id",
                "evolution_chain_id",
                "is_post_oras",
            }.issubset(pokemon_cols) and {"is_mega", "is_gmax", "introduced_generation"}.issubset(form_cols) and {
                "ability_effect_ko",
                "is_post_oras",
            }.issubset(ability_cols) and {"learn_level", "is_post_oras"}.issubset(level_cols) and {
                "chain_id",
                "pokemon_id",
                "depth",
            }.issubset(evo_cols)
            if count > 0 and has_new_schema:
                return
        except Exception:
            pass
        DB_PATH.unlink(missing_ok=True)

    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        build_database(DB_PATH)
    except Exception as exc:
        if DB_PATH.exists():
            DB_PATH.unlink()
        raise RuntimeError("데이터셋 초기화 실패: 네트워크에서 PokeAPI CSV를 내려받을 수 없습니다.") from exc


def build_database(path: Path) -> None:
    con = sqlite3.connect(path)
    cur = con.cursor()
    cur.executescript(
        """
        PRAGMA journal_mode=WAL;

        CREATE TABLE pokemon (
            id INTEGER PRIMARY KEY,
            identifier TEXT NOT NULL,
            korean_name TEXT NOT NULL,
            display_name_ko TEXT NOT NULL,
            species_id INTEGER NOT NULL,
            evolution_chain_id INTEGER,
            is_post_oras INTEGER NOT NULL
        );
        CREATE INDEX idx_pokemon_korean_name ON pokemon(korean_name);
        CREATE INDEX idx_pokemon_display_name ON pokemon(display_name_ko);

        CREATE TABLE pokemon_form_meta (
            pokemon_id INTEGER PRIMARY KEY,
            is_default INTEGER,
            is_mega INTEGER,
            is_gmax INTEGER,
            introduced_generation INTEGER,
            form_order INTEGER,
            sort_order INTEGER
        );

        CREATE TABLE pokemon_stat (pokemon_id INTEGER, stat_identifier TEXT, base_stat INTEGER, PRIMARY KEY(pokemon_id, stat_identifier));

        CREATE TABLE pokemon_ability (
            pokemon_id INTEGER,
            ability_id INTEGER,
            ability_name_ko TEXT,
            ability_effect_ko TEXT,
            is_post_oras INTEGER,
            is_hidden INTEGER
        );

        CREATE TABLE pokemon_type (pokemon_id INTEGER, type_id INTEGER, slot INTEGER, type_name_ko TEXT);
        CREATE TABLE type_efficacy (attack_type_id INTEGER, target_type_id INTEGER, damage_factor INTEGER);

        CREATE TABLE pokemon_egg_move (
            pokemon_id INTEGER,
            move_name_ko TEXT,
            move_identifier TEXT,
            type_name_ko TEXT,
            damage_class_ko TEXT,
            power INTEGER,
            accuracy INTEGER,
            pp INTEGER,
            effect_text_ko TEXT,
            is_post_oras INTEGER
        );

        CREATE TABLE pokemon_level_move (
            pokemon_id INTEGER,
            move_name_ko TEXT,
            type_name_ko TEXT,
            damage_class_ko TEXT,
            power INTEGER,
            accuracy INTEGER,
            pp INTEGER,
            effect_text_ko TEXT,
            learn_level INTEGER,
            is_post_oras INTEGER
        );

        CREATE TABLE evolution_member (
            chain_id INTEGER,
            depth INTEGER,
            pokemon_id INTEGER,
            display_name_ko TEXT,
            is_special INTEGER,
            sort_order INTEGER
        );
        """
    )

    languages = fetch_csv("languages")
    ko_lang_id, en_lang_id = resolve_language_ids(languages)

    pokemon = fetch_csv("pokemon")
    species = fetch_csv("pokemon_species")
    species_names = fetch_csv("pokemon_species_names")
    pokemon_forms = fetch_csv("pokemon_forms")
    form_names = fetch_csv("pokemon_form_names")
    version_groups = fetch_csv("version_groups")

    pokemon_stats = fetch_csv("pokemon_stats")
    stats = fetch_csv("stats")

    pokemon_abilities = fetch_csv("pokemon_abilities")
    abilities = fetch_csv("abilities")
    ability_names = fetch_csv("ability_names")
    ability_prose = fetch_csv("ability_prose")
    ability_flavor_text = fetch_csv("ability_flavor_text")

    pokemon_types = fetch_csv("pokemon_types")
    type_names = fetch_csv("type_names")
    type_efficacy = fetch_csv("type_efficacy")

    pokemon_moves = fetch_csv("pokemon_moves")
    move_methods = fetch_csv("pokemon_move_methods")
    moves = fetch_csv("moves")
    move_names = fetch_csv("move_names")
    move_effect_prose = fetch_csv("move_effect_prose")
    move_flavor_text = fetch_csv("move_flavor_text")
    damage_classes = fetch_csv("move_damage_classes")
    damage_class_names = fetch_csv("move_damage_class_prose")

    species_to_ko = {r["pokemon_species_id"]: r["name"] for r in species_names if r["local_language_id"] == ko_lang_id}
    species_id_to_identifier = {r["id"]: r["identifier"] for r in species}
    species_generation = {r["id"]: safe_int(r.get("generation_id"), 0) for r in species}
    species_chain = {r["id"]: safe_int(r.get("evolution_chain_id"), 0) for r in species}
    species_parent = {r["id"]: (r["evolves_from_species_id"] or "") for r in species}

    pokemon_to_species = {r["id"]: r["species_id"] for r in pokemon}
    pokemon_order = {r["id"]: safe_int(r.get("order"), 99999) for r in pokemon}

    version_group_to_gen = {r["id"]: safe_int(r.get("generation_id"), 0) for r in version_groups}
    forms_by_pokemon = {r["pokemon_id"]: r for r in pokemon_forms}
    form_name_ko = {
        r["pokemon_form_id"]: (r.get("pokemon_name") or r.get("form_name") or "").strip()
        for r in form_names
        if r["local_language_id"] == ko_lang_id
    }

    pokemon_rows: list[tuple[int, str, str, str, int, int, int]] = []
    form_meta_rows: list[tuple[int, int, int, int, int, int, int]] = []
    for p in pokemon:
        pid = int(p["id"])
        pid_key = str(pid)
        sid = pokemon_to_species[pid_key]
        base_name = species_to_ko.get(sid, species_id_to_identifier.get(sid, p["identifier"]))
        form = forms_by_pokemon.get(pid_key)

        introduced_gen = species_generation.get(sid, 0)
        is_default = 1
        is_mega = 0
        is_gmax = 0
        form_order = 9999

        if form:
            vg = form.get("introduced_in_version_group_id")
            if vg:
                introduced_gen = version_group_to_gen.get(vg, introduced_gen)
            is_default = safe_int(form.get("is_default"), 0)
            is_mega = safe_int(form.get("is_mega"), 0)
            form_order = safe_int(form.get("form_order"), 9999)
            form_identifier = (form.get("form_identifier") or "")
            is_gmax = 1 if "gmax" in p["identifier"] or "gmax" in form_identifier else 0

        is_post_oras = 1 if introduced_gen > 6 else 0
        display_name = base_name
        if form and form_name_ko.get(form["id"]):
            display_name = form_name_ko[form["id"]]
        elif p["identifier"] != species_id_to_identifier.get(sid, p["identifier"]):
            display_name = f"{base_name} ({p['identifier']})"

        chain_id = species_chain.get(sid, 0)
        pokemon_rows.append((pid, p["identifier"], base_name, display_name, int(sid), chain_id, is_post_oras))
        form_meta_rows.append((pid, is_default, is_mega, is_gmax, introduced_gen, form_order, pokemon_order[pid_key]))

    cur.executemany("INSERT INTO pokemon VALUES(?,?,?,?,?,?,?)", pokemon_rows)
    cur.executemany("INSERT INTO pokemon_form_meta VALUES(?,?,?,?,?,?,?)", form_meta_rows)

    stat_id_to_identifier = {r["id"]: r["identifier"] for r in stats}
    cur.executemany(
        "INSERT INTO pokemon_stat VALUES(?,?,?)",
        [(safe_int(r["pokemon_id"]), stat_id_to_identifier[r["stat_id"]], safe_int(r["base_stat"])) for r in pokemon_stats],
    )

    ability_name_ko = {r["ability_id"]: r["name"] for r in ability_names if r["local_language_id"] == ko_lang_id}
    ability_effect_ko = {r["ability_id"]: (r["short_effect"] or r["effect"]) for r in ability_prose if r["local_language_id"] == ko_lang_id}
    ability_effect_en = {r["ability_id"]: (r["short_effect"] or r["effect"]) for r in ability_prose if r["local_language_id"] == en_lang_id}
    ability_flavor_ko = latest_localized_text(ability_flavor_text, "ability_id", "flavor_text", ko_lang_id, "version_group_id")
    ability_flavor_en = latest_localized_text(ability_flavor_text, "ability_id", "flavor_text", en_lang_id, "version_group_id")
    ability_identifier = {r["id"]: r["identifier"] for r in abilities}
    ability_generation = {r["id"]: safe_int(r.get("generation_id"), 0) for r in abilities}

    ability_rows = []
    for r in pokemon_abilities:
        aid = r["ability_id"]
        ability_rows.append(
            (
                safe_int(r["pokemon_id"]),
                safe_int(aid),
                ability_name_ko.get(aid, ability_identifier.get(aid, "unknown")),
                ability_flavor_ko.get(aid) or ability_effect_ko.get(aid) or ability_flavor_en.get(aid) or ability_effect_en.get(aid) or "효과 정보가 없습니다.",
                1 if ability_generation.get(aid, 0) > 6 else 0,
                safe_int(r.get("is_hidden"), 0),
            )
        )
    cur.executemany("INSERT INTO pokemon_ability VALUES(?,?,?,?,?,?)", ability_rows)

    type_name_ko = {r["type_id"]: r["name"] for r in type_names if r["local_language_id"] == ko_lang_id}
    cur.executemany(
        "INSERT INTO pokemon_type VALUES(?,?,?,?)",
        [(safe_int(r["pokemon_id"]), safe_int(r["type_id"]), safe_int(r["slot"]), type_name_ko.get(r["type_id"], r["type_id"])) for r in pokemon_types],
    )

    cur.executemany(
        "INSERT INTO type_efficacy VALUES(?,?,?)",
        [(safe_int(r["damage_type_id"]), safe_int(r["target_type_id"]), safe_int(r["damage_factor"])) for r in type_efficacy],
    )

    egg_method_ids = {r["id"] for r in move_methods if r["identifier"] == "egg"}
    level_method_ids = {r["id"] for r in move_methods if r["identifier"] == "level-up"}

    move_name_ko = {r["move_id"]: r["name"] for r in move_names if r["local_language_id"] == ko_lang_id}
    move_identifier = {r["id"]: r["identifier"] for r in moves}
    move_detail = {r["id"]: r for r in moves}
    move_effect_ko = {r["move_effect_id"]: (r["short_effect"] or r["effect"]) for r in move_effect_prose if r["local_language_id"] == ko_lang_id}
    move_effect_en = {r["move_effect_id"]: (r["short_effect"] or r["effect"]) for r in move_effect_prose if r["local_language_id"] == en_lang_id}
    move_flavor_ko = latest_localized_text(move_flavor_text, "move_id", "flavor_text", ko_lang_id, "version_group_id")
    move_flavor_en = latest_localized_text(move_flavor_text, "move_id", "flavor_text", en_lang_id, "version_group_id")
    damage_class_identifier = {r["id"]: r["identifier"] for r in damage_classes}
    damage_class_ko = {r["move_damage_class_id"]: r["name"] for r in damage_class_names if r["local_language_id"] == ko_lang_id}

    seen_egg: set[tuple[int, str]] = set()
    egg_rows = []
    seen_level: set[tuple[int, str, int]] = set()
    level_rows = []

    for r in pokemon_moves:
        m = move_detail.get(r["move_id"])
        if not m:
            continue

        effect_tpl = move_flavor_ko.get(r["move_id"]) or move_effect_ko.get(m["effect_id"]) or move_flavor_en.get(r["move_id"]) or move_effect_en.get(m["effect_id"]) or "효과 정보가 없습니다."
        effect_text = effect_tpl.replace("$effect_chance", m["effect_chance"] or "-")
        dmg_cls = damage_class_ko.get(m["damage_class_id"], damage_class_identifier.get(m["damage_class_id"], "미상"))
        is_post_oras = 1 if safe_int(m.get("generation_id"), 0) > 6 else 0

        if r["pokemon_move_method_id"] in egg_method_ids:
            key = (safe_int(r["pokemon_id"]), r["move_id"])
            if key not in seen_egg:
                seen_egg.add(key)
                egg_rows.append(
                    (
                        safe_int(r["pokemon_id"]),
                        move_name_ko.get(r["move_id"], move_identifier.get(r["move_id"], "unknown")),
                        move_identifier.get(r["move_id"], "unknown"),
                        type_name_ko.get(m["type_id"], m["type_id"]),
                        dmg_cls,
                        safe_int(m.get("power"), 0),
                        safe_int(m.get("accuracy"), 0),
                        safe_int(m.get("pp"), 0),
                        effect_text,
                        is_post_oras,
                    )
                )

        if r["pokemon_move_method_id"] in level_method_ids:
            lvl = safe_int(r.get("level"), 0)
            key = (safe_int(r["pokemon_id"]), r["move_id"], lvl)
            if key not in seen_level:
                seen_level.add(key)
                level_rows.append(
                    (
                        safe_int(r["pokemon_id"]),
                        move_name_ko.get(r["move_id"], move_identifier.get(r["move_id"], "unknown")),
                        type_name_ko.get(m["type_id"], m["type_id"]),
                        dmg_cls,
                        safe_int(m.get("power"), 0),
                        safe_int(m.get("accuracy"), 0),
                        safe_int(m.get("pp"), 0),
                        effect_text,
                        lvl,
                        is_post_oras,
                    )
                )

    cur.executemany("INSERT INTO pokemon_egg_move VALUES(?,?,?,?,?,?,?,?,?,?)", egg_rows)
    cur.executemany("INSERT INTO pokemon_level_move VALUES(?,?,?,?,?,?,?,?,?,?)", level_rows)

    # evolution tree members (includes forms; mega/gmax included explicitly)
    chain_species: dict[int, list[str]] = defaultdict(list)
    for s in species:
        cid = safe_int(s.get("evolution_chain_id"), 0)
        if cid > 0:
            chain_species[cid].append(s["id"])

    species_depth_cache: dict[tuple[str, int], int] = {}

    def species_depth(species_id: str, chain_id: int) -> int:
        key = (species_id, chain_id)
        if key in species_depth_cache:
            return species_depth_cache[key]
        parent = species_parent.get(species_id, "")
        if not parent or safe_int(species_chain.get(parent, 0), 0) != chain_id:
            species_depth_cache[key] = 0
            return 0
        d = species_depth(parent, chain_id) + 1
        species_depth_cache[key] = d
        return d

    pokemon_rows_by_species: dict[int, list[sqlite3.Row]] = defaultdict(list)
    con.row_factory = sqlite3.Row
    for row in con.execute("SELECT p.*, f.is_default, f.is_mega, f.is_gmax, f.form_order, f.sort_order FROM pokemon p LEFT JOIN pokemon_form_meta f ON p.id=f.pokemon_id").fetchall():
        pokemon_rows_by_species[row["species_id"]].append(row)

    evo_rows = []
    for chain_id, species_ids in chain_species.items():
        for sid_text in species_ids:
            sid = safe_int(sid_text, 0)
            depth = species_depth(sid_text, chain_id)
            for prow in pokemon_rows_by_species.get(sid, []):
                is_special = 0
                if (prow["is_mega"] or 0) == 1 or (prow["is_gmax"] or 0) == 1:
                    is_special = 1
                elif (prow["is_default"] or 0) == 0 and prow["identifier"] != species_id_to_identifier.get(str(sid), prow["identifier"]):
                    is_special = 1
                evo_rows.append((chain_id, depth, prow["id"], prow["display_name_ko"], is_special, prow["sort_order"] or 99999))

    cur.executemany("INSERT INTO evolution_member VALUES(?,?,?,?,?,?)", evo_rows)

    con.commit()
    con.close()


def get_connection() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def type_matchups(con: sqlite3.Connection, type_ids: list[int]) -> dict[str, list[dict[str, str | float]]]:
    rows = con.execute("SELECT DISTINCT type_id, type_name_ko FROM pokemon_type").fetchall()
    names = {int(r["type_id"]): r["type_name_ko"] for r in rows}
    multipliers: dict[int, float] = {tid: 1.0 for tid in names}

    for atk in names:
        m = 1.0
        for defending in type_ids:
            row = con.execute(
                "SELECT damage_factor FROM type_efficacy WHERE attack_type_id=? AND target_type_id=?",
                (atk, defending),
            ).fetchone()
            if row:
                m *= row["damage_factor"] / 100
        multipliers[atk] = m

    def collect(pred: callable) -> list[dict[str, str | float]]:
        out = [{"type": names[t], "multiplier": m} for t, m in multipliers.items() if pred(m)]
        return sorted(out, key=lambda x: (x["multiplier"], x["type"]))

    return {
        "weakness": collect(lambda m: m > 1),
        "resistance": collect(lambda m: 0 < m < 1),
        "immune": collect(lambda m: m == 0),
    }


def ordered_stats(rows: list[sqlite3.Row]) -> tuple[list[dict[str, int | str]], int]:
    raw = {r["stat_identifier"]: int(r["base_stat"]) for r in rows}
    ordered = [{"key": key, "name": STAT_LABELS[key], "value": raw.get(key, 0)} for key in STAT_ORDER]
    total = sum(item["value"] for item in ordered)
    return ordered, total


class Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, code: int = 200, ctype: str = "text/html; charset=utf-8") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._send(INDEX_HTML.encode("utf-8"))
            return
        if path == "/static/style.css":
            self._send(STYLE_CSS.encode("utf-8"), ctype="text/css; charset=utf-8")
            return
        if path == "/static/app.js":
            self._send(APP_JS.encode("utf-8"), ctype="application/javascript; charset=utf-8")
            return
        if path == "/api/search":
            q = urllib.parse.parse_qs(parsed.query).get("q", [""])[0].strip()
            if not q:
                self._send(b"[]", ctype="application/json; charset=utf-8")
                return
            con = get_connection()
            rows = con.execute(
                """
                SELECT id, korean_name, display_name_ko, identifier, is_post_oras
                FROM pokemon
                WHERE korean_name LIKE ? OR display_name_ko LIKE ?
                ORDER BY korean_name, id
                LIMIT 40
                """,
                (f"{q}%", f"{q}%"),
            ).fetchall()
            con.close()
            out = [
                {
                    "id": r["id"],
                    "korean_name": r["korean_name"],
                    "display_name": r["display_name_ko"],
                    "identifier": r["identifier"],
                    "oras_available": not bool(r["is_post_oras"]),
                }
                for r in rows
            ]
            self._send(json.dumps(out, ensure_ascii=False).encode("utf-8"), ctype="application/json; charset=utf-8")
            return

        if path.startswith("/api/pokemon/"):
            pid_text = path.replace("/api/pokemon/", "")
            if not pid_text.isdigit():
                self._send(b'{"error":"invalid id"}', code=400, ctype="application/json; charset=utf-8")
                return
            pid = int(pid_text)

            con = get_connection()
            p = con.execute("SELECT * FROM pokemon WHERE id=?", (pid,)).fetchone()
            if p is None:
                con.close()
                self._send(b'{"error":"not found"}', code=404, ctype="application/json; charset=utf-8")
                return

            stat_rows = con.execute("SELECT stat_identifier, base_stat FROM pokemon_stat WHERE pokemon_id=?", (pid,)).fetchall()
            stats, stat_total = ordered_stats(stat_rows)
            abilities = con.execute(
                """
                SELECT ability_name_ko, ability_effect_ko, is_post_oras, is_hidden
                FROM pokemon_ability
                WHERE pokemon_id=?
                ORDER BY is_hidden, ability_id
                """,
                (pid,),
            ).fetchall()
            types = con.execute("SELECT type_id, type_name_ko FROM pokemon_type WHERE pokemon_id=? ORDER BY slot", (pid,)).fetchall()
            egg_moves = con.execute(
                """
                SELECT move_name_ko, type_name_ko, damage_class_ko, power, accuracy, pp, effect_text_ko, is_post_oras
                FROM pokemon_egg_move
                WHERE pokemon_id=?
                ORDER BY move_name_ko
                """,
                (pid,),
            ).fetchall()
            level_moves = con.execute(
                """
                SELECT move_name_ko, type_name_ko, damage_class_ko, power, accuracy, pp, effect_text_ko, learn_level, is_post_oras
                FROM pokemon_level_move
                WHERE pokemon_id=?
                ORDER BY learn_level, move_name_ko
                """,
                (pid,),
            ).fetchall()
            evolution_rows = con.execute(
                """
                SELECT depth, pokemon_id, display_name_ko, is_special
                FROM evolution_member
                WHERE chain_id=?
                ORDER BY depth, is_special, sort_order, pokemon_id
                """,
                (p["evolution_chain_id"],),
            ).fetchall()

            payload = {
                "id": p["id"],
                "korean_name": p["korean_name"],
                "display_name": p["display_name_ko"],
                "identifier": p["identifier"],
                "oras_available": not bool(p["is_post_oras"]),
                "image": f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{p['id']}.png",
                "types": [r["type_name_ko"] for r in types],
                "stats": stats,
                "stat_total": stat_total,
                "abilities": [
                    {
                        "name": r["ability_name_ko"],
                        "description": r["ability_effect_ko"],
                        "hidden": bool(r["is_hidden"]),
                        "post_oras": bool(r["is_post_oras"]),
                    }
                    for r in abilities
                ],
                "type_matchups": type_matchups(con, [int(r["type_id"]) for r in types]),
                "level_moves": [
                    {
                        "name": r["move_name_ko"],
                        "type": r["type_name_ko"],
                        "damage_class": r["damage_class_ko"],
                        "power": r["power"],
                        "accuracy": r["accuracy"],
                        "pp": r["pp"],
                        "effect": r["effect_text_ko"],
                        "level": r["learn_level"],
                        "post_oras": bool(r["is_post_oras"]),
                    }
                    for r in level_moves
                ],
                "egg_moves": [
                    {
                        "name": r["move_name_ko"],
                        "type": r["type_name_ko"],
                        "damage_class": r["damage_class_ko"],
                        "power": r["power"],
                        "accuracy": r["accuracy"],
                        "pp": r["pp"],
                        "effect": r["effect_text_ko"],
                        "post_oras": bool(r["is_post_oras"]),
                    }
                    for r in egg_moves
                ],
                "evolution_tree": [
                    {
                        "depth": r["depth"],
                        "pokemon_id": r["pokemon_id"],
                        "name": r["display_name_ko"],
                        "image": f"https://raw.githubusercontent.com/PokeAPI/sprites/master/sprites/pokemon/other/official-artwork/{r['pokemon_id']}.png",
                        "is_special": bool(r["is_special"]),
                    }
                    for r in evolution_rows
                ],
            }
            con.close()
            self._send(json.dumps(payload, ensure_ascii=False).encode("utf-8"), ctype="application/json; charset=utf-8")
            return

        self._send(b"Not found", 404, "text/plain; charset=utf-8")


def launch_edge(url: str) -> None:
    if not sys.platform.startswith("win"):
        return
    try:
        subprocess.Popen(["cmd", "/c", "start", "msedge", url], shell=False)
    except Exception:
        pass


def run() -> None:
    ensure_db()
    url = f"http://127.0.0.1:{PORT}"
    threading.Timer(1.0, lambda: launch_edge(url)).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Pokemon Wiki running at {url}")
    server.serve_forever()


if __name__ == "__main__":
    run()
