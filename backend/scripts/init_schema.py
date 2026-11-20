"""
Create the SwasthyaSync schema on a fresh Postgres / AlloyDB database — idempotent.

Takes the CREATE TABLE statements from migrate_to_supabase.py (without its DROP
statements), makes them IF NOT EXISTS, then applies the startup ALTER TABLE patches
(including staff_sessions.created_at). Never drops or migrates data.

Run from backend/ with DATABASE_URL set:   python scripts/init_schema.py
Then seed synthetic demo data:             python seed_judge_demo_data.py
"""
import asyncio
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)


def load_schema_sql() -> str:
    src = open(os.path.join(HERE, "migrate_to_supabase.py"), encoding="utf-8").read()
    m = re.search(r'SCHEMA_SQL\s*=\s*"""(.*?)"""', src, re.S)
    if not m:
        raise SystemExit("SCHEMA_SQL not found in migrate_to_supabase.py")
    sql = m.group(1)
    sql = "\n".join(l for l in sql.splitlines() if not l.strip().upper().startswith("DROP "))
    return re.sub(r"CREATE TABLE (?!IF NOT EXISTS)", "CREATE TABLE IF NOT EXISTS ", sql, flags=re.I)


PATCHES = [
    "ALTER TABLE patients ADD COLUMN IF NOT EXISTS vitals TEXT",
    "ALTER TABLE staff_sessions DROP CONSTRAINT IF EXISTS staff_sessions_staff_id_fkey",
    "ALTER TABLE staff_sessions ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now()",
]


async def main() -> None:
    import database
    await database.init_db_pool()
    if not database._pool:
        raise SystemExit("Could not connect — set DATABASE_URL (or ALLOYDB_INSTANCE_URI).")
    async with database._pool.acquire() as conn:
        await conn.execute(load_schema_sql())
        for p in PATCHES:
            await conn.execute(p)
        tables = await conn.fetch(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='public' ORDER BY 1")
    print("Schema ready. Tables:", ", ".join(r["table_name"] for r in tables))
    await database._pool.close()


if __name__ == "__main__":
    asyncio.run(main())
