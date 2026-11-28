"""W346 second oracle for the Product variable: run the REAL allValue from home.json inside the
Factory tile's `in (...)` against throwaway sqlite tables (v_factory has rows, cap_cards is empty,
the live case) and print the boards the tile would show, one per line."""
import json
import sqlite3
import sys

d = json.load(open(sys.argv[1]))
all_value = next(v for v in d["templating"]["list"] if v["name"] == "product")["allValue"]
sql = next(t["rawSql"] for p in d["panels"] if p.get("title") == "Factory" for t in p["targets"])
db = sqlite3.connect(":memory:")
db.execute("create table v_factory (board text, state_words text)")
db.execute("create table cap_cards (board text)")
live = [("sample-app", "running"), ("capsule-corp", "running")]
db.executemany("insert into v_factory values (?, ?)", live)
rows = db.execute(sql.replace("$product", all_value)).fetchall()
print("\n".join(r[0] for r in rows))
