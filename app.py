from __future__ import annotations

import csv
import io
import json
import re
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
DB_SCHEMA_VERSION = 3

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




def clean_effect_text(text: str) -> str:
    t = text.replace("\n", " ").strip()
    t = re.sub(r"\[([^\]]+)\]\{[^}]+\}", r"\1", t)
    t = t.replace("[", "").replace("]", "")
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def translate_en_to_ko(text: str) -> str:
    t = clean_effect_text(text)
    low = t.lower()

    # Common full-sentence patterns first (avoid mixed ko/en output)
    m = re.search(r"(\d+)% chance to make the target flinch\.?", low)
    if m:
        return f"{m.group(1)}% 확률로 상대를 풀죽게 한다."

    if "inflicts regular damage with no additional effect" in low:
        return "추가 효과 없이 일반적인 데미지를 준다."

    if "causes one-hit ko" in low:
        return "일격에 상대를 쓰러뜨릴 수 있다."

    if "confuses the target" in low:
        return "상대를 혼란 상태로 만든다."

    if "heals the user by half its max hp" in low:
        return "사용자의 최대 HP 절반만큼 회복한다."

    if "equal to the user's level" in low or "equal to the user’s level" in low:
        return "사용자의 레벨과 같은 데미지를 준다."

    if "protecting the user from further damage or status changes until it breaks" in low and "1/4" in low:
        return "사용자의 최대 HP의 1/4을 소비해 분신인 인형을 만들고, 인형이 사라질 때까지 데미지와 상태이상을 막는다."

    # stage up/down patterns
    stat_map = {
        "special defense": "특수방어",
        "special attack": "특수공격",
        "attack": "공격",
        "defense": "방어",
        "speed": "스피드",
        "accuracy": "명중",
        "evasion": "회피",
    }
    stage_map = {"one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6"}

    m = re.search(r"lowers the target'?s? ([a-z ]+) by (one|two|three|four|five|six) stages?", low)
    if m:
        stat = stat_map.get(m.group(1).strip(), m.group(1).strip())
        stage = stage_map.get(m.group(2), m.group(2))
        return f"상대의 {stat}(을/를) {stage}랭크 떨어뜨린다."

    m = re.search(r"raises the user'?s? ([a-z ]+) by (one|two|three|four|five|six) stages?", low)
    if m:
        stat = stat_map.get(m.group(1).strip(), m.group(1).strip())
        stage = stage_map.get(m.group(2), m.group(2))
        return f"사용자의 {stat}(을/를) {stage}랭크 올린다."

    # Status immunity / weather snippets
    if "prevents paralysis" in low:
        return "마비 상태가 되지 않는다."
    if "protects against sandstorm damage" in low:
        return "모래바람 데미지를 받지 않는다."
    if "increases evasion" in low and "sandstorm" in low:
        return "모래바람일 때 회피율이 상승한다."

    # Generic fallback (avoid half-translated mixed sentence)
    return f"(영문 설명) {t}"


def choose_localized_text(ko_primary: str | None, ko_secondary: str | None, en_primary: str | None, en_secondary: str | None) -> str:
    for cand in [ko_primary, ko_secondary]:
        if cand and cand.strip():
            return clean_effect_text(cand)
    for cand in [en_primary, en_secondary]:
        if cand and cand.strip():
            return translate_en_to_ko(cand)
    return "효과 정보가 없습니다."

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
            user_version = con.execute("PRAGMA user_version").fetchone()[0]
            pokemon_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon)").fetchall()}
            form_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_form_meta)").fetchall()}
            ability_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_ability)").fetchall()}
            level_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_level_move)").fetchall()}
            evo_cols = {r[1] for r in con.execute("PRAGMA table_info(evolution_member)").fetchall()}
            edge_cols = {r[1] for r in con.execute("PRAGMA table_info(evolution_edge)").fetchall()}
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
            }.issubset(evo_cols) and {"from_pokemon_id", "to_pokemon_id", "condition_text"}.issubset(edge_cols)
            if count > 0 and has_new_schema and user_version == DB_SCHEMA_VERSION:
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

        CREATE TABLE evolution_edge (
            chain_id INTEGER,
            from_pokemon_id INTEGER,
            to_pokemon_id INTEGER,
            condition_text TEXT,
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
    pokemon_evolution = fetch_csv("pokemon_evolution")
    evolution_triggers = fetch_csv("evolution_triggers")
    evolution_trigger_prose = fetch_csv("evolution_trigger_prose")
    items = fetch_csv("items")
    item_names = fetch_csv("item_names")

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
                choose_localized_text(ability_flavor_ko.get(aid), ability_effect_ko.get(aid), ability_flavor_en.get(aid), ability_effect_en.get(aid)),
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

        effect_text = choose_localized_text(move_flavor_ko.get(r["move_id"]), move_effect_ko.get(m["effect_id"]), move_flavor_en.get(r["move_id"]), move_effect_en.get(m["effect_id"])).replace("$effect_chance", m["effect_chance"] or "-")
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

    # evolution tree members + edges
    chain_species: dict[int, list[str]] = defaultdict(list)
    for sp in species:
        cid = safe_int(sp.get("evolution_chain_id"), 0)
        if cid > 0:
            chain_species[cid].append(sp["id"])

    trigger_id_to_name = {r["id"]: r["identifier"] for r in evolution_triggers}
    trigger_id_to_ko = {
        r["evolution_trigger_id"]: (r.get("name") or r.get("evolution_trigger_id"))
        for r in evolution_trigger_prose
        if r.get("local_language_id") == ko_lang_id
    }
    item_id_to_name = {r["id"]: r["identifier"] for r in items}
    item_id_to_ko = {
        r["item_id"]: r.get("name") or item_id_to_name.get(r["item_id"], r["item_id"])
        for r in item_names
        if r.get("local_language_id") == ko_lang_id
    }
    evo_by_species = {r["evolved_species_id"]: r for r in pokemon_evolution}

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
    default_pokemon_by_species: dict[int, int] = {}
    con.row_factory = sqlite3.Row
    all_p_rows = con.execute("SELECT p.*, f.is_default, f.is_mega, f.is_gmax, f.form_order, f.sort_order FROM pokemon p LEFT JOIN pokemon_form_meta f ON p.id=f.pokemon_id").fetchall()
    for row in all_p_rows:
        pokemon_rows_by_species[row["species_id"]].append(row)
        if (row["is_default"] or 0) == 1:
            default_pokemon_by_species[row["species_id"]] = row["id"]

    def evo_condition_text(evo: dict[str, str] | None) -> str:
        if not evo:
            return ""
        min_level = safe_int(evo.get("minimum_level"), 0)
        item_id = evo.get("trigger_item_id") or ""
        held_item_id = evo.get("held_item_id") or ""
        trigger_id = evo.get("evolution_trigger_id") or ""
        known_move_type = evo.get("known_move_type_id") or ""
        known_move = evo.get("known_move_id") or ""
        location_id = evo.get("location_id") or ""
        min_happiness = evo.get("minimum_happiness") or ""
        time_of_day = evo.get("time_of_day") or ""

        if min_level > 0:
            return f"Lv.{min_level}"
        if item_id:
            return f"{item_id_to_ko.get(item_id, item_id_to_name.get(item_id, '진화아이템'))} 사용"
        if held_item_id:
            return f"{item_id_to_ko.get(held_item_id, item_id_to_name.get(held_item_id, '아이템'))} 지니고"
        if min_happiness:
            return f"친밀도 {min_happiness}+"
        if known_move:
            return "특정 기술 습득"
        if known_move_type:
            return "특정 타입 기술 습득"
        if location_id:
            return "특정 장소"
        if time_of_day:
            return f"{time_of_day}"
        if trigger_id:
            trig = trigger_id_to_ko.get(trigger_id, trigger_id_to_name.get(trigger_id, "진화"))
            if trigger_id_to_name.get(trigger_id) == "trade":
                return "교환"
            return trig
        return "진화"

    evo_rows = []
    edge_rows = []
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

        # base species evolution edges
        for sid_text in species_ids:
            sid = safe_int(sid_text, 0)
            parent_sid_text = species_parent.get(sid_text, "")
            if not parent_sid_text:
                continue
            parent_sid = safe_int(parent_sid_text, 0)
            from_pid = default_pokemon_by_species.get(parent_sid)
            to_pid = default_pokemon_by_species.get(sid)
            if not from_pid or not to_pid:
                continue
            evo = evo_by_species.get(sid_text)
            cond = evo_condition_text(evo)
            sort_order = (species_depth(sid_text, chain_id) * 10000) + to_pid
            edge_rows.append((chain_id, from_pid, to_pid, cond, sort_order))

        # special-form edges (mega/gmax/etc)
        for sid_text in species_ids:
            sid = safe_int(sid_text, 0)
            base_pid = default_pokemon_by_species.get(sid)
            if not base_pid:
                continue
            for prow in pokemon_rows_by_species.get(sid, []):
                if prow["id"] == base_pid:
                    continue
                cond = "폼변화"
                if (prow["is_mega"] or 0) == 1:
                    cond = "메가진화"
                elif (prow["is_gmax"] or 0) == 1:
                    cond = "거다이맥스"
                edge_rows.append((chain_id, base_pid, prow["id"], cond, (species_depth(sid_text, chain_id) * 10000) + (prow["sort_order"] or 99999)))

    cur.executemany("INSERT INTO evolution_member VALUES(?,?,?,?,?,?)", evo_rows)
    cur.executemany("INSERT INTO evolution_edge VALUES(?,?,?,?,?)", edge_rows)

    cur.execute(f"PRAGMA user_version={DB_SCHEMA_VERSION}")
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




def localize_runtime_text(text: str | None) -> str:
    if not text:
        return "효과 정보가 없습니다."
    t = clean_effect_text(text)
    if re.search(r"[A-Za-z]", t):
        return translate_en_to_ko(t)
    return t

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
            evolution_edges = con.execute(
                """
                SELECT from_pokemon_id, to_pokemon_id, condition_text
                FROM evolution_edge
                WHERE chain_id=?
                ORDER BY sort_order, from_pokemon_id, to_pokemon_id
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
                        "description": localize_runtime_text(r["ability_effect_ko"]),
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
                        "effect": localize_runtime_text(r["effect_text_ko"]),
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
                        "effect": localize_runtime_text(r["effect_text_ko"]),
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
                "evolution_edges": [
                    {
                        "from": r["from_pokemon_id"],
                        "to": r["to_pokemon_id"],
                        "condition": r["condition_text"] or "진화",
                    }
                    for r in evolution_edges
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
