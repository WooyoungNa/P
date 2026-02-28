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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

DB_PATH = Path("data/pokewiki.db")
CSV_BASE = "https://raw.githubusercontent.com/PokeAPI/pokeapi/master/data/v2/csv"
KO_LANG_ID = "3"
EN_LANG_ID = "9"
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
            ability_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_ability)").fetchall()}
            egg_cols = {r[1] for r in con.execute("PRAGMA table_info(pokemon_egg_move)").fetchall()}
            con.close()
            has_new_schema = {"ability_effect_ko", "is_post_oras"}.issubset(ability_cols) and {
                "effect_text_ko",
                "is_post_oras",
            }.issubset(egg_cols)
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
        CREATE TABLE pokemon (id INTEGER PRIMARY KEY, identifier TEXT NOT NULL, korean_name TEXT NOT NULL);
        CREATE INDEX idx_pokemon_korean_name ON pokemon(korean_name);

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
        """
    )

    pokemon = fetch_csv("pokemon")
    species = fetch_csv("pokemon_species")
    species_names = fetch_csv("pokemon_species_names")
    pokemon_stats = fetch_csv("pokemon_stats")
    stats = fetch_csv("stats")
    pokemon_abilities = fetch_csv("pokemon_abilities")
    abilities = fetch_csv("abilities")
    ability_names = fetch_csv("ability_names")
    ability_prose = fetch_csv("ability_prose")
    pokemon_types = fetch_csv("pokemon_types")
    type_names = fetch_csv("type_names")
    type_efficacy = fetch_csv("type_efficacy")
    pokemon_moves = fetch_csv("pokemon_moves")
    move_methods = fetch_csv("pokemon_move_methods")
    moves = fetch_csv("moves")
    move_names = fetch_csv("move_names")
    move_effect_prose = fetch_csv("move_effect_prose")
    damage_classes = fetch_csv("move_damage_classes")
    damage_class_names = fetch_csv("move_damage_class_prose")

    species_to_ko = {r["pokemon_species_id"]: r["name"] for r in species_names if r["local_language_id"] == KO_LANG_ID}
    species_id_to_identifier = {r["id"]: r["identifier"] for r in species}
    pokemon_to_species = {r["id"]: r["species_id"] for r in pokemon}

    pokemon_rows = []
    for p in pokemon:
        pid = int(p["id"])
        sid = pokemon_to_species[str(pid)]
        korean_name = species_to_ko.get(sid, species_id_to_identifier.get(sid, p["identifier"]))
        pokemon_rows.append((pid, p["identifier"], korean_name))
    cur.executemany("INSERT INTO pokemon VALUES(?,?,?)", pokemon_rows)

    stat_id_to_identifier = {r["id"]: r["identifier"] for r in stats}
    cur.executemany(
        "INSERT INTO pokemon_stat VALUES(?,?,?)",
        [(int(r["pokemon_id"]), stat_id_to_identifier[r["stat_id"]], int(r["base_stat"])) for r in pokemon_stats],
    )

    ability_name_ko = {r["ability_id"]: r["name"] for r in ability_names if r["local_language_id"] == KO_LANG_ID}
    ability_effect_ko = {r["ability_id"]: (r["short_effect"] or r["effect"]) for r in ability_prose if r["local_language_id"] == KO_LANG_ID}
    ability_effect_en = {r["ability_id"]: (r["short_effect"] or r["effect"]) for r in ability_prose if r["local_language_id"] == EN_LANG_ID}
    ability_identifier = {r["id"]: r["identifier"] for r in abilities}
    ability_generation = {r["id"]: int(r["generation_id"]) for r in abilities}

    ability_rows = []
    for r in pokemon_abilities:
        aid = r["ability_id"]
        is_post_oras = 1 if ability_generation.get(aid, 0) > 6 else 0
        ability_rows.append(
            (
                int(r["pokemon_id"]),
                int(aid),
                ability_name_ko.get(aid, ability_identifier.get(aid, "unknown")),
                ability_effect_ko.get(aid) or ability_effect_en.get(aid) or "효과 정보가 없습니다.",
                is_post_oras,
                int(r["is_hidden"]),
            )
        )
    cur.executemany("INSERT INTO pokemon_ability VALUES(?,?,?,?,?,?)", ability_rows)

    type_name_ko = {r["type_id"]: r["name"] for r in type_names if r["local_language_id"] == KO_LANG_ID}
    cur.executemany(
        "INSERT INTO pokemon_type VALUES(?,?,?,?)",
        [(int(r["pokemon_id"]), int(r["type_id"]), int(r["slot"]), type_name_ko.get(r["type_id"], r["type_id"])) for r in pokemon_types],
    )

    cur.executemany(
        "INSERT INTO type_efficacy VALUES(?,?,?)",
        [(int(r["damage_type_id"]), int(r["target_type_id"]), int(r["damage_factor"])) for r in type_efficacy],
    )

    egg_method_ids = {r["id"] for r in move_methods if r["identifier"] == "egg"}
    move_name_ko = {r["move_id"]: r["name"] for r in move_names if r["local_language_id"] == KO_LANG_ID}
    move_identifier = {r["id"]: r["identifier"] for r in moves}
    move_detail = {r["id"]: r for r in moves}
    move_effect_ko = {r["move_effect_id"]: (r["short_effect"] or r["effect"]) for r in move_effect_prose if r["local_language_id"] == KO_LANG_ID}
    move_effect_en = {r["move_effect_id"]: (r["short_effect"] or r["effect"]) for r in move_effect_prose if r["local_language_id"] == EN_LANG_ID}
    damage_class_identifier = {r["id"]: r["identifier"] for r in damage_classes}
    damage_class_ko = {r["move_damage_class_id"]: r["name"] for r in damage_class_names if r["local_language_id"] == KO_LANG_ID}

    seen: set[tuple[int, str]] = set()
    egg_rows = []
    for r in pokemon_moves:
        if r["pokemon_move_method_id"] not in egg_method_ids:
            continue
        key = (int(r["pokemon_id"]), r["move_id"])
        if key in seen:
            continue
        seen.add(key)

        m = move_detail.get(r["move_id"])
        if not m:
            continue
        effect_tpl = move_effect_ko.get(m["effect_id"]) or move_effect_en.get(m["effect_id"]) or "효과 정보가 없습니다."
        effect_text = effect_tpl.replace("$effect_chance", m["effect_chance"] or "-")
        dmg_cls = damage_class_ko.get(m["damage_class_id"], damage_class_identifier.get(m["damage_class_id"], "미상"))
        is_post_oras = 1 if int(m["generation_id"] or 0) > 6 else 0

        egg_rows.append(
            (
                int(r["pokemon_id"]),
                move_name_ko.get(r["move_id"], move_identifier.get(r["move_id"], "unknown")),
                move_identifier.get(r["move_id"], "unknown"),
                type_name_ko.get(m["type_id"], m["type_id"]),
                dmg_cls,
                int(m["power"] or 0),
                int(m["accuracy"] or 0),
                int(m["pp"] or 0),
                effect_text,
                is_post_oras,
            )
        )
    cur.executemany("INSERT INTO pokemon_egg_move VALUES(?,?,?,?,?,?,?,?,?,?)", egg_rows)

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
                "SELECT id, korean_name, identifier FROM pokemon WHERE korean_name LIKE ? ORDER BY id LIMIT 30",
                (f"{q}%",),
            ).fetchall()
            con.close()
            self._send(json.dumps([dict(r) for r in rows], ensure_ascii=False).encode("utf-8"), ctype="application/json; charset=utf-8")
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

            payload = {
                "id": p["id"],
                "korean_name": p["korean_name"],
                "identifier": p["identifier"],
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
