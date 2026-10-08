"""每個資料根逐層 no-follow 開啟；增量索引只存變動的檔案，不每次搜尋重掃。"""
import contextlib
import os
from pathlib import Path, PurePosixPath
import sqlite3
import stat
import threading
import time

# macOS 沒有 O_PATH：改用 O_RDONLY 開目錄（單一使用者只走自己可讀的路徑）；O_NOFOLLOW 防護不變。
O_PATH = getattr(os, 'O_PATH', os.O_RDONLY)

# 內容比對每分鐘、目錄發現每十分鐘，沿用神殿兩層刷新設計。
CONTENT_SECONDS = 60
DISCOVERY_SECONDS = 600
TEXT_BYTES = 1024 * 1024  # 每份最多讀1MiB；其餘仍可依檔名搜尋。
TEXT_SUFFIXES = {'.md', '.txt', '.html', '.htm', '.csv', '.json', '.log', '.py', '.js', '.c', '.h', '.yaml', '.yml'}


@contextlib.contextmanager
def open_member(root, relative='', *, directory=False):
    """含 root 在內每層皆拒絕 symlink；以 dir_fd 綁住已開啟的目錄，避免檢查後換路徑。"""
    base = Path(root)
    rel = PurePosixPath(relative)
    if not base.is_absolute() or rel.is_absolute() or '..' in rel.parts or '\\' in relative or '\x00' in relative:
        raise OSError('invalid path')
    parts = base.parts[1:] + rel.parts
    fd = os.open('/', O_PATH | os.O_DIRECTORY)
    try:
        for i, part in enumerate(parts):
            if i < len(parts)-1:
                # 中間層只要「經過」：O_PATH 不需要讀權限（例如別人家目錄只給 --x），
                # O_DIRECTORY＋O_NOFOLLOW 讓符號連結照樣被拒（2026-10-01 AA Forum 內建傳檔站）。
                flags = O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW
            else:
                flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                if directory:
                    flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        expected = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected:
            raise OSError('not a regular file/directory')
        yield fd
    finally:
        os.close(fd)


class SearchIndex:
    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.last_scan = {}
        self.last_refresh = {}
        self.db.execute('''CREATE TABLE IF NOT EXISTS files(
            owner TEXT, space TEXT, rel TEXT, stamp TEXT, size INTEGER, modified REAL,
            content TEXT, partial INTEGER, PRIMARY KEY(owner,space,rel))''')
        self.db.commit()

    def refresh(self, owner, space, root, *, force=False):
        key = (owner, space)
        now = time.monotonic()
        with self.lock:
            if not force and now - self.last_refresh.get(key, -1e9) < CONTENT_SECONDS:
                return 0
            old = {r['rel']: dict(r) for r in self.db.execute('SELECT * FROM files WHERE owner=? AND space=?', key)}
            names = set(old)
            discover = force or now - self.last_scan.get(key, -1e9) >= DISCOVERY_SECONDS
            if discover:
                # os.walk 不跟隨子目錄連結；下面 open_member 再拒絕每層連結。
                names = set()
                for folder, dirs, files in os.walk(root, followlinks=False):
                    dirs[:] = [d for d in dirs if not d.startswith('.') and not Path(folder, d).is_symlink()]
                    for name in files:
                        if not name.startswith('.'):
                            names.add(str(Path(folder, name).relative_to(root)))
                self.last_scan[key] = now
            changed = 0
            with self.db:
                for rel in names:
                    try:
                        with open_member(root, rel) as fd:
                            info = os.fstat(fd)
                            stamp = f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{info.st_ctime_ns}'
                            if rel in old and old[rel]['stamp'] == stamp:
                                continue
                            text = ''
                            partial = 1
                            if Path(rel).suffix.lower() in TEXT_SUFFIXES:
                                with os.fdopen(os.dup(fd), 'rb') as src:
                                    raw = src.read(TEXT_BYTES + 1)
                                if b'\x00' not in raw:
                                    text = raw[:TEXT_BYTES].decode('utf-8', errors='replace').casefold()
                                    partial = int(len(raw) > TEXT_BYTES)
                            self.db.execute('INSERT OR REPLACE INTO files VALUES(?,?,?,?,?,?,?,?)',
                                            (*key, rel, stamp, info.st_size, info.st_mtime, text, partial))
                            changed += 1
                    except OSError:
                        self.db.execute('DELETE FROM files WHERE owner=? AND space=? AND rel=?', (*key, rel))
                for rel in set(old) - names:
                    self.db.execute('DELETE FROM files WHERE owner=? AND space=? AND rel=?', (*key, rel))
            self.last_refresh[key] = now
            return changed

    def search(self, owner, space, root, query='', offset=0, limit=100):
        # 搜尋與下載再檢查存取，舊索引不能繞過刪檔或 symlink 改動。
        with self.lock:
            rows = self.db.execute('SELECT * FROM files WHERE owner=? AND space=? ORDER BY modified DESC,rel', (owner,space)).fetchall()
        matches = []
        needle = query.casefold()
        for row in rows:
            try:
                with open_member(root, row['rel']):
                    pass
            except OSError:
                continue
            if needle and needle not in row['rel'].casefold() and needle not in row['content']:
                continue
            result = dict(row)
            result.pop('content')
            result.pop('stamp')
            matches.append(result)
        return matches[offset:offset+limit], len(matches), self.last_refresh.get((owner,space)) is not None
