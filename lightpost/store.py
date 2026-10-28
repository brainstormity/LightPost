import json
import os
from pathlib import Path

import aiosqlite


class Store:
    """SQLite storage with a dedicated I/O thread and a single-process lock."""

    def __init__(self, path):
        self.path = path
        self.lock_file = None

    async def open(self):
        if self.path != ":memory:":
            import fcntl

            path = Path(self.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            self.lock_file = open(str(path) + ".lock", "a")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                self.lock_file.close()
                self.lock_file = None
                raise ValueError("Another LightPost process already owns this database.") from None
        self.db = await aiosqlite.connect(self.path)
        await self.db.execute("PRAGMA journal_mode=WAL")
        await self.db.execute("PRAGMA busy_timeout=5000")
        await self.db.execute(
            "CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, value TEXT, PRIMARY KEY(kind,id))"
        )
        await self.db.execute(
            "CREATE INDEX IF NOT EXISTS posts_due ON records(kind, json_extract(value, '$.status'), json_extract(value, '$.scheduledAt'))"
        )
        await self.db.commit()
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)
        return self

    async def get(self, kind, id):
        async with self.db.execute(
            "SELECT value FROM records WHERE kind=? AND id=?", (kind, id)
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def all(self, kind):
        async with self.db.execute("SELECT value FROM records WHERE kind=?", (kind,)) as cursor:
            return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def posts(self, status="", offset=0, limit=50):
        where = "kind='posts' AND COALESCE(json_extract(value, '$.demo'),0)=0 AND json_extract(value, '$.status')!='simulated'"
        args = []
        if status == "attention":
            where += " AND json_extract(value, '$.status') IN ('failed','partial','needs_review','blocked')"
        elif status:
            where += " AND json_extract(value, '$.status')=?"
            args.append(status)
        async with self.db.execute("SELECT count(*) FROM records WHERE " + where, args) as cursor:
            total = (await cursor.fetchone())[0]
        order = (
            "json_extract(value, '$.scheduledAt') ASC"
            if status == "scheduled"
            else "json_extract(value, '$.createdAt') DESC"
        )
        async with self.db.execute(
            "SELECT value FROM records WHERE "
            + where
            + " ORDER BY "
            + order
            + ", id DESC LIMIT ? OFFSET ?",
            [*args, limit, offset],
        ) as cursor:
            posts = [json.loads(row[0]) for row in await cursor.fetchall()]
        return posts, total

    async def post_with_key(self, key):
        async with self.db.execute(
            "SELECT value FROM records WHERE kind='posts' AND json_extract(value, '$.idempotencyKey')=? LIMIT 1",
            (key,),
        ) as cursor:
            row = await cursor.fetchone()
        return json.loads(row[0]) if row else None

    async def due(self, now, limit=2):
        async with self.db.execute(
            "SELECT value FROM records WHERE kind='posts' AND (json_extract(value, '$.status')='queued' OR (json_extract(value, '$.status')='scheduled' AND json_extract(value, '$.scheduledAt')<=?) OR (json_extract(value, '$.status')='processing' AND COALESCE(json_extract(value, '$.nextCheckAt'),'')<=?)) ORDER BY json_extract(value, '$.scheduledAt') LIMIT ?",
            (now, now, limit),
        ) as cursor:
            return [json.loads(row[0]) for row in await cursor.fetchall()]

    async def put(self, kind, value):
        await self.db.execute(
            "INSERT OR REPLACE INTO records VALUES (?,?,?)", (kind, value["id"], json.dumps(value))
        )
        await self.db.commit()
        return value

    async def remove(self, kind, id):
        await self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, id))
        await self.db.commit()

    async def close(self):
        await self.db.close()
        if self.lock_file:
            self.lock_file.close()
