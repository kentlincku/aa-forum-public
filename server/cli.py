"""Agent client; credentials stay in the local account state directory."""
import argparse
import json
import os
import re
from pathlib import Path
import sys
import urllib.request
import uuid

parser = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog=f'''群聊操作（以 --agent 指定本人；ROOM 為通知中的群號）：
  read ROOM                              讀完整未讀內容；取件不等於已讀
  confirm-read ROOM MESSAGE_ID [...]      只列實際讀完的編號；圖片須檢視後才確認
  post ROOM --reply MESSAGE_ID --file FILE 回到群內回覆；FILE 可用 - 讀 stdin
  download ROOM MESSAGE_ID [--out PATH]    下載該則訊息的附件（檔案或圖片）到工作目錄
  invite ROOM AGENT --reason "具體理由"    依常設授權邀請系統既有特務，自動記錄與公告
  freeze ROOM --reason "具體理由"         本群成員可凍結為唯讀，保留歷史
  unfreeze ROOM --reason "具體理由"       本群成員可重啟討論，不補送舊通知
不對通知回 ACK；被 @ 讀後回應，其餘有新證據或異議才補充。
操作規範與常設授權原文（含範圍、撤回及新人可讀完整歷史）：
  {Path(__file__).resolve().with_name('README.md')}
  章節：邀請操作規範''')
parser.add_argument('--agent', default=os.environ.get('MBOX_AGENT'), required=not os.environ.get('MBOX_AGENT'))
parser.add_argument('--url', default=os.environ.get('AAF_CHAT_URL', 'http://127.0.0.1:8111'))
sub = parser.add_subparsers(dest='command', required=True)
sub.add_parser('rooms')
sub.add_parser('agents')
reminder = sub.add_parser('reminder', help='機械提醒：get讀設定／set設定／stop停止；凍結即停用')
reminder.add_argument('room', type=int)
reminder.add_argument('action', choices=['get', 'set', 'set-section', 'stop'])
reminder.add_argument('section', nargs='?', help='set-section 要替換的節名')
reminder.add_argument('--no-template', action='store_true', help='明示允許 set 全文沒有本房規範節')
reminder.add_argument('--file', help='set所用 UTF-8 .md 檔；- 讀 stdin')
reminder.add_argument('--minutes', type=int, choices=[10,15,30], help='set週期，按鐘面整10／15／30分鐘觸發')
reminder.add_argument('--enable', action='store_true', help='set後啟用；未指定只儲存停用設定')
for command in ('freeze', 'unfreeze'):
    freeze = sub.add_parser(command, help='凍結／重啟本群；成員可操作，理由記錄並公告')
    freeze.add_argument('room', type=int)
    freeze.add_argument('--reason', required=True)
invite = sub.add_parser('invite', help='邀請既有特務；新人可讀本群完整歷史')
invite.add_argument('room', type=int)
invite.add_argument('member')
invite.add_argument('--reason', required=True, help='本群討論需要的理由；新增會自動群內告知')
like = sub.add_parser('like', help='按讚；--undo 取消，不發通知')
like.add_argument('room', type=int)
like.add_argument('message', type=int)
like.add_argument('--undo', action='store_true')
approval = sub.add_parser('approval', help='查本群是否自動核准：exit 0＝核准中、1＝否；--json 輸出完整狀態')
approval.add_argument('room', type=int)
approval.add_argument('--json', action='store_true')
read = sub.add_parser('read')
read.add_argument('room', type=int)
confirm = sub.add_parser('confirm-read', help='讀完後逐則確認；不從游標推定已讀')
confirm.add_argument('room', type=int)
confirm.add_argument('messages', type=int, nargs='+')
download = sub.add_parser('download', help='下載訊息附件（檔案或圖片）')
download.add_argument('room', type=int)
download.add_argument('message', type=int)
download.add_argument('--out', help='存檔路徑；預設用附件原檔名，存在目前目錄')
post = sub.add_parser('post')
post.add_argument('room', type=int)
post.add_argument('--reply', type=int)
post.add_argument('--file', required=True, help='UTF-8 file, or - for stdin')
post.add_argument('--key', default=None, help='Reuse the same key when retrying a failed request')


def reminder_sections(body):
    """回傳節名與原文座標；標題到下一節前皆屬同一節。"""
    return list(re.finditer(r'^■[ \t]+([^\s（]+)[^\r\n]*(?:\r?\n|$)', body, re.M))


def main():
    args = parser.parse_args()
    if not args.url:
        parser.error('--url or AAF_CHAT_URL is required')
    state = Path(os.environ.get('AAF_SERVER_STATE', Path(__file__).resolve().parent.parent / 'var' / 'server'))
    token = json.loads((state/'credentials.json').read_text())[args.agent]
    def api(path, data=None):
        req = urllib.request.Request(args.url+path, data=None if data is None else json.dumps(data).encode(),
                                     headers={'Authorization':'Bearer '+token, 'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=15) as response:
            return json.load(response)
    def fetch(path):
        req = urllib.request.Request(args.url+path, headers={'Authorization':'Bearer '+token})
        with urllib.request.urlopen(req,timeout=60) as response:
            return response.read(), response.headers
    if args.command=='download':
        from urllib.error import HTTPError
        from urllib.parse import unquote
        base = f'/api/rooms/{args.room}/messages/{args.message}'
        try:
            data, headers = fetch(base+'/file')
            cd = headers.get('Content-Disposition', '')
            name = unquote(cd.split("filename*=UTF-8''", 1)[1]) if "filename*=UTF-8''" in cd else f'message-{args.message}'
        except HTTPError as e:
            if e.code != 404:
                raise
            data, headers = fetch(base+'/image')
            ext = {'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp'}.get(headers.get_content_type(), '')
            name = f'message-{args.message}{ext}'
        out = Path(args.out or Path(name).name)       # 只取檔名，不跟著附件名跳出目錄
        out.write_bytes(data)
        print(f'{out.resolve()}  {len(data)} bytes')
        return
    if args.command=='reminder':
        path=f'/api/rooms/{args.room}/reminder'
        if args.action=='get':
            result=api(path)
        elif args.action=='stop':
            result=api(path+'/stop',{})
        else:
            if not args.file or (args.action=='set' and args.minutes is None):
                parser.error('reminder set 需要 --file 與 --minutes；set-section 需要 --file')
            body=sys.stdin.read() if args.file=='-' else Path(args.file).read_text(encoding='utf-8')
            expected = None
            minutes, enabled = args.minutes, args.enable
            if args.action=='set-section':
                if not args.section or args.no_template:
                    parser.error('set-section 需要節名，且不能使用 --no-template')
                current = api(path)
                old = current['body']
                sections = reminder_sections(old)
                matches = [i for i, m in enumerate(sections) if m[1]==args.section]
                if len(matches)!=1:
                    parser.error('指定節不存在或重複；現有節名：'+ '、'.join(m[1] for m in sections))
                replacement = reminder_sections(body)
                if replacement and (len(replacement)!=1 or replacement[0][1]!=args.section or body[:replacement[0].start()].strip()):
                    parser.error('檔案只能包含指定的一節，不得夾帶其他節')
                i = matches[0]
                section = sections[i]
                end = sections[i+1].start() if i+1<len(sections) else len(old)
                if not replacement:
                    body = old[section.start():section.end()].rstrip('\r\n')+'\n'+body
                body = old[:section.start()]+body.rstrip('\r\n')+'\n'+old[end:]
                minutes = current['minutes'] if minutes is None else minutes
                enabled = True if args.enable else bool(current['enabled'])
                expected = {k:current[k] for k in ('body','minutes','enabled')}
            elif not any(m[1]=='本房規範' for m in reminder_sections(body)):
                if not args.no_template:
                    parser.error('body 缺『■ 本房規範』節——你可能只交了本場段；要只換一節用 set-section，要真的整段覆蓋加 --no-template')
                print('整段覆蓋·無規範節·操作者 '+args.agent, file=sys.stderr)
            result=api(path,{'body':body,'minutes':minutes,'enabled':enabled, **({'expected':expected} if expected is not None else {})})
            saved = api(path)
            names = [m[1] for m in reminder_sections(saved['body'])]
            print(f'回讀 {len(names)} 節：'+ '、'.join(names), file=sys.stderr)
        print(json.dumps(result,ensure_ascii=False,indent=2))
    elif args.command=='approval':
        st = api(f'/api/rooms/{args.room}/approval')
        if args.json:
            print(json.dumps(st, ensure_ascii=False))
        else:
            print(f"群 {args.room}：" + (f"自動核准中（{'到 ' + st['until'] if st['until'] else '不設到期'}，{st['by']} 開啟）" if st['on']
                                         else ('自動核准已過期（' + st['until'] + '）' if st.get('expired') else '未自動核准')))
        sys.exit(0 if st['on'] else 1)
    elif args.command=='rooms':
        print(json.dumps(api('/api/rooms'),ensure_ascii=False,indent=2))
    elif args.command=='agents':
        print(json.dumps(api('/api/agents'),ensure_ascii=False,indent=2))
    elif args.command in ('freeze', 'unfreeze'):
        print(json.dumps(api(f'/api/rooms/{args.room}/freeze',
                            {'frozen':args.command=='freeze','reason':args.reason}),ensure_ascii=False))
    elif args.command=='invite':
        print(json.dumps(api(f'/api/rooms/{args.room}/members', {'agent':args.member,'reason':args.reason}),ensure_ascii=False))
    elif args.command=='like':
        print(json.dumps(api(f'/api/rooms/{args.room}/messages/{args.message}/like',
                            {'liked':not args.undo}),ensure_ascii=False))
    elif args.command=='confirm-read':
        print(json.dumps(api(f'/api/rooms/{args.room}/confirm-read',
                            {'messages':args.messages}),ensure_ascii=False))
    elif args.command=='read':
        rooms = api('/api/rooms')
        room = next((r for r in rooms if r['id']==args.room), None)
        if not room:
            raise SystemExit('不是此群成員')
        through = room['last_read']
        while True:
            data = api(f'/api/rooms/{args.room}/messages?after={through}')
            print(json.dumps(data,ensure_ascii=False,indent=2),flush=True)
            if data['messages']:
                through = data['messages'][-1]['id']
                api(f'/api/rooms/{args.room}/read',{'through':through})
            if not data['has_more']:
                break
    else:
        body = sys.stdin.read() if args.file=='-' else Path(args.file).read_text()
        key = args.key or str(uuid.uuid4())
        print('retry key: '+key,file=sys.stderr)
        print(json.dumps(api(f'/api/rooms/{args.room}/messages',
            {'body':body,'reply_to':args.reply,'client_id':key}),ensure_ascii=False))


if __name__=='__main__':
    main()
