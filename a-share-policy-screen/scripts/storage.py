"""Immutable SQLite bar revisions and lightweight daily snapshot manifests."""
import gzip
import hashlib
import json
import os
import sqlite3
from pathlib import Path

KEYS = ('date', 'open', 'high', 'low', 'close', 'volume')


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_bytes(encode(value))
    temp.replace(path)


class Store:
    def __init__(self, path):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS series(
                id TEXT PRIMARY KEY, symbol TEXT NOT NULL, source TEXT NOT NULL,
                parent TEXT, depth INTEGER NOT NULL, payload BLOB NOT NULL);
            CREATE TABLE IF NOT EXISTS snapshots(
                id TEXT PRIMARY KEY, day TEXT NOT NULL, scope TEXT NOT NULL,
                header TEXT NOT NULL, entries TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS snapshot_days ON snapshots(day);
            CREATE TABLE IF NOT EXISTS checkpoints(
                run_key TEXT NOT NULL, symbol TEXT NOT NULL, item TEXT NOT NULL,
                revision TEXT NOT NULL, PRIMARY KEY(run_key,symbol));
        ''')
        self.db.commit()

    def close(self):
        self.db.close()

    def bars(self, revision):
        layers = []
        while revision:
            row = self.db.execute('SELECT parent,payload FROM series WHERE id=?', (revision,)).fetchone()
            if not row:
                raise ValueError('missing_history_revision:' + revision)
            layers.append(json.loads(gzip.decompress(row[1])))
            revision = row[0]
        by_day = {}
        for layer in reversed(layers):
            by_day.update((r[0], dict(zip(KEYS, r))) for r in layer)
        return [by_day[d] for d in sorted(by_day)]

    def put_series(self, stock, previous=None, old_bars=None):
        rows = stock.get('bars') or []
        source = json.dumps([stock.get('bar_source'), stock.get('adjust'), stock.get('volume_unit')])
        parent = None
        depth = 0
        payload = rows
        if previous:
            old = self.db.execute('SELECT symbol,source,depth FROM series WHERE id=?', (previous,)).fetchone()
            if old and old[:2] == (stock['symbol'], source):
                old_bars = self.bars(previous) if old_bars is None else old_bars
                if rows == old_bars:
                    return previous
                # Only exact preserved prefixes are appendable. Corrections get a new base.
                if old[2] < 20 and rows[:len(old_bars)] == old_bars:
                    parent, depth, payload = previous, old[2]+1, rows[len(old_bars):]
        raw = encode([[r.get(k) for k in KEYS] for r in payload])
        identity = encode([stock['symbol'], source, parent]) + raw
        revision = hashlib.sha256(identity).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO series VALUES(?,?,?,?,?,?)',
                        (revision, stock['symbol'], source, parent, depth, gzip.compress(raw, 3, mtime=0)))
        return revision

    def checkpoint(self, run_key, stock, previous=None, old_bars=None):
        revision = self.put_series(stock, previous, old_bars)
        metadata = {k:v for k,v in stock.items() if k not in ('bars', '_weeks')}
        self.db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?)',
                        (run_key, stock['symbol'], encode(metadata).decode(), revision))
        self.db.commit()
        return {'stock': metadata, 'revision': revision}

    def checkpoints(self, run_key):
        return {sid: {'stock': json.loads(item), 'revision': revision}
                for sid,item,revision in self.db.execute(
                    'SELECT symbol,item,revision FROM checkpoints WHERE run_key=? ORDER BY symbol', (run_key,))}

    def save_snapshot(self, header, entries):
        header = {k:v for k,v in header.items() if k != 'stocks'}
        raw_header, raw_entries = encode(header), encode(entries)
        identity = hashlib.sha256(raw_header + b'\n' + raw_entries).hexdigest()
        self.db.execute('INSERT OR IGNORE INTO snapshots VALUES(?,?,?,?,?)',
                        (identity, header['target_date'], header['scope'], raw_header.decode(), raw_entries.decode()))
        self.db.commit()
        return identity

    def snapshot(self, identity):
        row = self.db.execute('SELECT header,entries FROM snapshots WHERE id=?', (identity,)).fetchone()
        if not row:
            raise ValueError('missing_snapshot:' + identity)
        header, entries = json.loads(row[0]), json.loads(row[1])
        if hashlib.sha256(encode(header) + b'\n' + encode(entries)).hexdigest() != identity:
            raise ValueError('snapshot_digest_mismatch')
        return header, entries

    def latest(self, before=None):
        sql = "SELECT id FROM snapshots WHERE scope='current_active_sh_sz'"
        args = ()
        if before:
            sql += ' AND day<?'
            args = (before,)
        row = self.db.execute(sql + ' ORDER BY day DESC,rowid DESC LIMIT 1', args).fetchone()
        return row[0] if row else None

    def manifest(self, snapshot_id, destination):
        header, entries = self.snapshot(snapshot_id)
        destination = Path(destination).resolve()
        value = {'schema_version':'2.0', 'target_date':header['target_date'],
                 'scope':header['scope'], 'stock_count':len(entries), 'snapshot_id':snapshot_id,
                 'cache_ref':os.path.relpath(self.path, destination.parent)}
        if destination.exists() and json.loads(destination.read_text()) != value:
            raise FileExistsError('immutable_manifest_already_exists:' + str(destination))
        if not destination.exists():
            atomic_json(destination, value)
        return value

    def import_bundle(self, bundle):
        entries = []
        seen = set()
        for stock in bundle['stocks']:
            sid = stock['symbol']
            if sid in seen:
                raise ValueError('duplicate_stock:' + sid)
            seen.add(sid)
            revision = self.put_series(stock)
            entries.append({'stock': {k:v for k,v in stock.items() if k != 'bars'}, 'revision':revision})
        return self.save_snapshot(bundle, entries)


class StockStream:
    def __init__(self, path, snapshot_id, symbols=None):
        self.path, self.snapshot_id, self.symbols = path, snapshot_id, symbols

    def __iter__(self):
        store = Store(self.path)
        try:
            _, entries = store.snapshot(self.snapshot_id)
            for entry in entries:
                stock = entry['stock']
                if self.symbols is None or stock['symbol'] in self.symbols:
                    yield {**stock, 'bars':store.bars(entry['revision'])}
        finally:
            store.close()


def load_input(path, symbols=None):
    path = Path(path).resolve()
    raw = path.read_bytes()
    value = json.loads(raw)
    digest = hashlib.sha256(raw).hexdigest()
    if value.get('schema_version') == '2.0':
        cache = path.parent / value['cache_ref']
        if not cache.is_file():
            raise ValueError('snapshot_cache_unavailable')
        store = Store(cache)
        try:
            header, entries = store.snapshot(value['snapshot_id'])
            if (header['target_date'] != value['target_date'] or header['scope'] != value['scope']
                    or len(entries) != value['stock_count']):
                raise ValueError('manifest_metadata_mismatch')
            header['stocks'] = StockStream(store.path, value['snapshot_id'], symbols)
            return header, digest
        finally:
            store.close()
    if value.get('schema_version') != '1.0':
        raise ValueError('unsupported_input_schema')
    if symbols is not None:
        value['stocks'] = [s for s in value['stocks'] if s['symbol'] in symbols]
    return value, digest
