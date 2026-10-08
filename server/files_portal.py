"""AA Forum 內建傳檔站（2026-10-01 由獨立的 8091 傳檔站併入·哥令「一個登入同時用 AA Forum 和傳檔站」）。

每支 AA Forum 本來就以本人的系統帳號執行（server@<帳號>，User=%i），所以這裡只服務本人：
- 資料區 outputs：`<實例>/outputs`（可用 AAF_FILES_ROOT 覆寫），可上傳。
- 資料區 legacy：AAF_FILES_LEGACY_ROOT 有設才出現，只讀（舊 8091 傳檔站留下的檔案）。
登入沿用 AA Forum 本身：前端從 sessionStorage 拿 AA Forum 的 token、每次呼叫帶 Authorization；
只有擁有者（Owner）能用，特務的 CLI 憑證不行。

檔案操作沿用舊站的 files_storage：每層 O_NOFOLLOW＋dir_fd，拒絕符號連結與路徑逃逸；
上傳先寫暫存檔再以 link 排他發布，同名另存、不覆寫。
上傳改成一檔一個請求、送原始位元組（不用 multipart），所以不需要多裝套件，也不必把整批讀進記憶體。
"""
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import secrets
import threading
import time
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse

from files_storage import SearchIndex, open_member

def _upload_limit():
    """檔案頁上傳單檔上限（MB）：AAF_UPLOAD_MB，預設 1024；0＝不限（回 None）。"""
    import sys as _s
    from pathlib import Path as _P
    _s.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from mbox import paths as _p
    try:
        mb = int(_p.setting('AAF_UPLOAD_MB', '1024'))
    except (TypeError, ValueError):
        mb = 1024
    return None if mb <= 0 else mb * 1024 * 1024


UPLOAD_BYTES = _upload_limit()   # 單檔上限；None＝不限
CHUNK = 1024 * 1024
TAIPEI = timezone(timedelta(hours=8))
REFRESH_SECONDS = 30
NAME_BYTES = 255  # 單一檔名上限（ext4／btrfs／APFS 皆為 255 bytes）


def spaces_from_env(instance):
    # Mac 移植：預設 <實例>/outputs
    outputs = os.environ.get('AAF_FILES_ROOT') or str(Path(os.path.expanduser(os.environ.get('AAF_HOME') or str(Path(__file__).resolve().parent.parent))) / 'outputs')
    spaces = {'outputs': {'root': outputs, 'label': 'outputs', 'writable': True}}
    legacy = os.environ.get('AAF_FILES_LEGACY_ROOT')
    if legacy:
        spaces['legacy'] = {'root': legacy, 'label': '舊傳檔資料', 'writable': False}
    for space in spaces.values():
        if not Path(space['root']).is_absolute():
            raise ValueError('files root must be absolute')
    return spaces


def clean_name(raw):
    """只取最後一段檔名；拒絕空白、隱藏檔與控制字元。"""
    name = Path((raw or '').replace('\\', '/')).name.strip()
    if not name or name.startswith('.') or any(ord(c) < 32 for c in name) or len(name.encode()) > NAME_BYTES:
        return None
    return name


def numbered(original, number):
    """同名另存用的名字：`<stem>-<n><suffix>`。加上 `-n` 後若超過 255 bytes，
    先截短 stem（不切斷多位元組字），副檔名保留；副檔名本身太長就整個名字當 stem 截。"""
    if number == 0:
        return original
    stem, suffix = Path(original).stem, Path(original).suffix
    tag = f'-{number}'
    if len((tag + suffix).encode()) >= NAME_BYTES:
        stem, suffix = original, ''
    budget = NAME_BYTES - len((tag + suffix).encode())
    stem = stem.encode()[:budget].decode('utf-8', errors='ignore')
    return f'{stem}{tag}{suffix}'


def make_router(identity, instance, state_dir, spaces=None):
    spaces = spaces or spaces_from_env(instance)
    index = SearchIndex(Path(state_dir) / 'files_search.sqlite3')
    router = APIRouter(prefix='/files')

    def owner_only(request):
        if identity(request) != 'Owner':
            raise HTTPException(403, '傳檔站只給擁有者使用')

    def pick(space):
        if space not in spaces:
            raise HTTPException(400, '無效資料區')
        return spaces[space]

    def refresh_all():
        for name, space in spaces.items():
            try:
                index.refresh(instance, name, space['root'])
            except Exception:
                # 不印路徑或內容；搜尋回應會顯示索引尚未就緒。
                print('files index refresh failed', flush=True)

    def loop():
        while True:
            refresh_all()
            time.sleep(REFRESH_SECONDS)

    def start_indexer():
        # 只在服務真的起來時才開背景索引（app.py 的 __main__），單元測試與 set-password 不開。
        threading.Thread(target=loop, daemon=True, name='files-index').start()

    router.refresh_all = refresh_all
    router.start_indexer = start_indexer
    router.index = index

    @router.get('', include_in_schema=False)
    @router.get('/', include_in_schema=False)
    def files_page():
        # 舊網址導到新介面的檔案頁；資料都在要登入的 /files/api/*。
        return RedirectResponse('/app/files', status_code=308)

    @router.get('/api/me')
    def me(request: Request):
        owner_only(request)
        return {'owner': instance,
                'spaces': [{'id': k, 'label': v['label'], 'writable': v['writable'],
                            'path': v['root'], 'exists': os.path.isdir(v['root'])}
                           for k, v in spaces.items()]}

    @router.get('/api/files')
    def files(request: Request, space: str = 'outputs', q: str = '', offset: int = 0):
        owner_only(request)
        config = pick(space)
        needle = q.strip()
        if not 0 <= offset <= 1000000 or len(needle) > 200:
            raise HTTPException(400, '無效搜尋條件')
        rows, total, ready = index.search(instance, space, config['root'], needle, offset)
        out = []
        for row in rows:
            modified = datetime.fromtimestamp(row['modified'], TAIPEI)
            out.append({'name': row['rel'], 'rel': row['rel'], 'size': row['size'],
                        'day': modified.strftime('%Y%m%d'),
                        'modified': modified.strftime('%Y-%m-%d %H:%M'),
                        'path': str(Path(config['root']) / row['rel']), 'partial': row['partial']})
        return {'owner': instance, 'space': space, 'files': out, 'total': total, 'offset': offset,
                'limit': 100, 'truncated': offset + len(out) < total, 'ready': ready}

    @router.get('/api/download')
    def download(request: Request, rel: str, space: str = 'outputs'):
        owner_only(request)
        config = pick(space)
        try:
            with open_member(config['root'], rel) as fd:
                size = os.fstat(fd).st_size
                handle = os.fdopen(os.dup(fd), 'rb')
        except OSError:
            raise HTTPException(404, '找不到可下載的檔案')

        def stream():
            try:
                while True:
                    chunk = handle.read(CHUNK)
                    if not chunk:
                        break
                    yield chunk
            finally:
                handle.close()

        return StreamingResponse(stream(), media_type='application/octet-stream', headers={
            'Content-Length': str(size),
            'Content-Disposition': "attachment; filename*=UTF-8''" + quote(Path(rel).name, safe='')})

    @router.post('/api/upload', status_code=201)
    async def upload(request: Request, name: str, space: str = 'outputs'):
        owner_only(request)
        if request.headers.get('x-portal-request') != '1':
            raise HTTPException(403, '請由傳檔站頁面操作')
        config = pick(space)
        if not config['writable']:
            raise HTTPException(403, '這個資料區只能讀')
        original = clean_name(name)
        if original is None:
            raise HTTPException(400, '檔名不合法')
        declared = request.headers.get('content-length')
        if declared is not None and (not declared.isdigit() or (UPLOAD_BYTES and int(declared) > UPLOAD_BYTES)):
            raise HTTPException(413, f'單檔上限 {UPLOAD_BYTES // 1048576} MB')
        now = datetime.now(TAIPEI)
        day = now.strftime('%Y%m%d')
        temporary = '.uploading-' + secrets.token_hex(16)
        try:
            with open_member(config['root'], directory=True) as rootfd:
                try:
                    os.mkdir(day, mode=0o770, dir_fd=rootfd)
                except FileExistsError:
                    pass
                dayfd = os.open(day, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        except OSError:
            raise HTTPException(500, '無法寫入本人的資料夾')
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o660, dir_fd=dayfd)
            written = 0
            try:
                with os.fdopen(fd, 'wb') as output:
                    async for chunk in request.stream():
                        written += len(chunk)
                        if UPLOAD_BYTES and written > UPLOAD_BYTES:
                            raise HTTPException(413, f'單檔上限 {UPLOAD_BYTES // 1048576} MB')
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                for number in range(10000):
                    stored = numbered(original, number)
                    try:
                        # link 是排他發布：同名時換下一個名字，不覆寫既有檔案。
                        os.link(temporary, stored, src_dir_fd=dayfd, dst_dir_fd=dayfd, follow_symlinks=False)
                        break
                    except FileExistsError:
                        continue
                else:
                    raise HTTPException(409, '同名檔案太多')
                size = os.stat(stored, dir_fd=dayfd, follow_symlinks=False).st_size
            finally:
                try:
                    os.unlink(temporary, dir_fd=dayfd)
                except FileNotFoundError:
                    pass
        except OSError:
            raise HTTPException(500, '無法寫入本人的資料夾')
        finally:
            os.close(dayfd)
        # 另起一條執行緒立刻重掃這個資料區，新檔一兩秒內就查得到；請求本身不等整棵樹重讀。
        threading.Thread(target=lambda: index.refresh(instance, space, config['root'], force=True),
                         daemon=True, name='files-index-upload').start()
        return JSONResponse({'owner': instance, 'uploaded_at': now.strftime('%Y-%m-%d %H:%M:%S'),
                             'file': {'original_name': original, 'stored_name': stored,
                                      'renamed': stored != original, 'size': size,
                                      'path': str(Path(config['root']) / day / stored)}},
                            status_code=201)

    return router
